import copy
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from urllib import error

import observation_archive as archive
import server
from test_v03 import FakeSocket


def event():
    return {**archive.new_event_fields(), 'time': '2026-11-07T12:00:00+00:00',
            'path': '/journal', 'method': 'GET', 'status': 200,
            'response_completed': True, 'ua': 'ExampleClient/1.0', 'agent_like': False}


def wait_for(check):
    deadline = time.monotonic() + 2
    while not check():
        if time.monotonic() >= deadline:
            raise AssertionError('sender did not finish')
        time.sleep(.005)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('socket.create_connection', side_effect=AssertionError('network prohibited'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def sender(self, transport=None, **kwargs):
        sender = archive.ObservationArchive(transport or Mock(), **kwargs)
        self.addCleanup(sender.close)
        return sender

    def test_disabled_and_incomplete_configuration_never_start(self):
        with patch.object(archive, 'ObservationTransport') as transport:
            self.assertIsNone(archive.configured_archive({}))
            self.assertIsNone(archive.configured_archive({'MOONBACK_OBSERVATION_ARCHIVE_SECRET': 'test-only'}))
            transport.assert_not_called()
        self.assertIsNone(archive.configured_archive({'MOONBACK_OBSERVATION_ARCHIVE_ENABLED': '1'}))
        configured = archive.configured_archive({
            'MOONBACK_OBSERVATION_ARCHIVE_ENABLED': '1',
            'MOONBACK_OBSERVATION_ARCHIVE_URL': 'https://observation.example/v1/observations',
            'MOONBACK_OBSERVATION_ARCHIVE_SECRET': 'test-only',
        })
        self.assertIsNotNone(configured)
        self.assertIsNone(configured._thread)

    def test_event_schema_and_fixed_metadata(self):
        item = event()
        self.assertEqual(json.loads(archive.encode_event(item)), item)
        self.assertNotEqual(event()['event_id'], item['event_id'])
        for key, value in [('schema_version', True), ('status', True), ('method', 'POST'),
                           ('time', '2026-02-30T00:00:00Z'), ('path', '/x?q=private'),
                           ('path', '/x#fragment'), ('ua', '\x1b[2J'), ('ua', 'x' * 181),
                           ('agent_like', 1), ('extra', 'untrusted')]:
            bad = {**item, key: value}
            with self.subTest(key=key, value=value), self.assertRaises(archive.ArchiveError):
                archive.encode_event(bad)

    def test_secret_blocked_before_queue_no_contact_false_positive(self):
        sender = self.sender()
        secret = 'Authorization: Bearer synthetic-credential-123456'
        self.assertFalse(sender.submit({**event(), 'ua': secret}))
        self.assertEqual(sender.health()['last_error'], 'secret_blocked')
        self.assertNotIn(secret, str(sender.health()))
        self.assertEqual(sender.health()['pending'], 0)
        self.assertTrue(sender.submit({**event(), 'ua': 'Public contact hello@example.com'}))

    def test_queue_count_and_memory_limits(self):
        for kwargs in ({'max_events': 1}, {'max_bytes': len(archive.encode_event(event())) + 3}):
            sender = self.sender(**kwargs)
            self.assertTrue(sender.submit(event()))
            self.assertFalse(sender.submit(event()))
            self.assertEqual(sender.health()['last_error'], 'queue_full')
            self.assertEqual(sender.health()['dropped'], 1)

    def test_success_in_background_and_event_snapshot(self):
        transport = Mock()
        sender = self.sender(transport)
        item = event()
        expected = copy.deepcopy(item)
        sender.submit(item)
        item['ua'] = 'changed'
        sender.start()
        wait_for(lambda: sender.health().get('stored') == 1)
        self.assertEqual(json.loads(transport.send.call_args.args[0]), expected)
        transport.send.assert_called_once()
        self.assertEqual(sender.health()['pending_bytes'], 0)

    def test_transient_retry_stable_bytes_id_and_retry_exhaustion(self):
        transport = Mock()
        transport.send.side_effect = archive.ArchiveError('connection', True)
        sender = self.sender(transport, max_attempts=3, retry_seconds=.01)
        sender.submit(event()); sender.start()
        wait_for(lambda: sender.health().get('failed') == 1)
        self.assertEqual(transport.send.call_count, 3)
        self.assertEqual(len({call.args[0] for call in transport.send.call_args_list}), 1)
        self.assertEqual(sender.health()['retries'], 2)

    def test_transient_retry_can_succeed(self):
        transport = Mock()
        transport.send.side_effect = [archive.ArchiveError('connection', True), None]
        sender = self.sender(transport, retry_seconds=0)
        sender.submit(event()); sender.start()
        wait_for(lambda: sender.health().get('stored') == 1)
        self.assertEqual(transport.send.call_count, 2)
        self.assertEqual(sender.health().get('failed', 0), 0)

    def test_non_retryable_auth_invalid_conflict_capacity(self):
        for category in ('authentication', 'invalid_request', 'conflict', 'capacity'):
            transport = Mock()
            transport.send.side_effect = archive.ArchiveError(category)
            sender = self.sender(transport)
            sender.submit(event()); sender.start()
            wait_for(lambda: sender.health().get('failed') == 1)
            transport.send.assert_called_once()

    def test_stop_and_fresh_process_model_loses_only_unsent_memory(self):
        sender = self.sender()
        sender.submit(event())
        sender.close()
        self.assertEqual(sender.health()['abandoned'], 1)
        self.assertEqual(sender.health()['pending'], 0)
        self.assertFalse(sender.submit(event()))
        fresh = self.sender()
        self.assertEqual(fresh.health()['pending'], 0)
        self.assertEqual(fresh.health().get('stored', 0), 0)

    def test_inflight_included_in_budget_and_stop_is_bounded(self):
        entered, release = threading.Event(), threading.Event()
        transport = Mock()
        transport.send.side_effect = lambda _: (entered.set(), release.wait(2))
        sender = self.sender(transport, max_events=1)
        self.addCleanup(release.set)
        sender.submit(event()); sender.start()
        self.assertTrue(entered.wait(1))
        self.assertFalse(sender.submit(event()))
        start = time.monotonic(); sender.close()
        self.assertLess(time.monotonic() - start, .5)
        release.set()
        wait_for(lambda: sender.health()['pending'] == 0)

    def test_unexpected_transport_exception_never_leaks(self):
        transport = Mock()
        transport.send.side_effect = RuntimeError('private provider diagnostic')
        sender = self.sender(transport)
        sender.submit(event()); sender.start()
        wait_for(lambda: sender.health().get('failed') == 1)
        self.assertEqual(sender.health()['last_error'], 'sender_failure')
        self.assertNotIn('private', str(sender.health()))

    def test_transport_exact_post_auth_ack_and_no_retry(self):
        transport = archive.ObservationTransport('https://observation.example/v1/observations', 'test-only')
        item = event(); response = Mock()
        response.status = 201
        response.read.return_value = json.dumps({'status': 'stored', 'event_id': item['event_id']}).encode()
        transport._opener = MagicMock()
        transport._opener.open.return_value.__enter__.return_value = response
        transport.send(archive.encode_event(item))
        transport._opener.open.assert_called_once()
        req = transport._opener.open.call_args.args[0]
        self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(req.get_header('Authorization'), 'Bearer test-only')
        self.assertEqual(req.get_header('User-agent'), archive.USER_AGENT)
        self.assertEqual(json.loads(req.data), item)
        self.assertEqual(transport._opener.open.call_args.kwargs['timeout'], 8)
        self.assertIsNone(archive.NoRedirect().redirect_request(req, None, 302, '', {}, 'https://evil.example'))

    def test_transport_errors_safe_and_no_internal_retry(self):
        for code, category, retry in [(401,'authentication',False), (404,'authentication',False),
                                      (409,'conflict',False), (400,'invalid_request',False),
                                      (503,'upstream_unavailable',True), (507,'capacity',False),
                                      (429,'upstream_unavailable',True), (302,'invalid_request',False)]:
            transport = archive.ObservationTransport('https://observation.example/v1/observations','test-only')
            transport._opener = MagicMock()
            transport._opener.open.side_effect = error.HTTPError('https://observation.example',code,'private diagnostic',{},io.BytesIO(b'private body'))
            with self.subTest(code=code), self.assertRaises(archive.ArchiveError) as found:
                transport.send(archive.encode_event(event()))
            self.assertEqual((found.exception.category,found.exception.retryable),(category,retry))
            self.assertEqual(str(found.exception), category)
            transport._opener.open.assert_called_once()

    def test_transport_timeout_and_malformed_ack(self):
        transport = archive.ObservationTransport('https://observation.example/v1/observations','test-only')
        transport._opener = MagicMock()
        transport._opener.open.side_effect = TimeoutError('private')
        with self.assertRaises(archive.ArchiveError) as found:
            transport.send(archive.encode_event(event()))
        self.assertTrue(found.exception.retryable)
        transport._opener.open.side_effect = None
        for raw in (b'not json', b'{}', b'x' * 1025, b'{"status":"stored","event_id":"wrong"}'):
            response = Mock(status=201)
            response.read.return_value = raw
            transport._opener.open.return_value.__enter__.return_value = response
            with self.assertRaises(archive.ArchiveError) as found:
                transport.send(archive.encode_event(event()))
            self.assertEqual(found.exception.category,'malformed_response')

    def test_transport_rejects_invalid_targets_and_headers(self):
        for url in ('http://example.com/v1/observations','https://example.com/other',
                    'https://user@example.com/v1/observations','https://example.com/v1/observations?q=1'):
            with self.assertRaises(ValueError):
                archive.ObservationTransport(url, 'test-only')
        with self.assertRaises(ValueError):
            archive.ObservationTransport('https://example.com/v1/observations', 'invalid\r\nheader')

    def test_server_queue_outside_log_lock_preserves_get_head_and_public_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'requests.jsonl'
            submitted = []
            def submit(item):
                self.assertFalse(server.OBSERVATION_LOG_LOCK.locked())
                submitted.append(item)
            with patch.object(server, 'LOG', log), patch.object(server, 'OBSERVATION_ARCHIVE', Mock(submit=submit)), \
                    patch.object(server.Handler, 'log_message'):
                for method in ('GET', 'HEAD'):
                    sock = FakeSocket((method + ' /journal?utm_source=test HTTP/1.0\r\n\r\n').encode())
                    server.Handler(sock, ('127.0.0.1',1), None)
                    self.assertEqual(int(sock.output.getvalue().split()[1]), 200)
                self.assertEqual(len(submitted), 2)
                local = [json.loads(row) for row in log.read_text().splitlines()]
                self.assertEqual(local, submitted)
                self.assertTrue(all(row['path'] == '/journal' for row in local))
                self.assertNotIn('event_id',server.read_stats()['recent'][0])
                self.assertNotIn('schema_version',server.read_stats()['recent'][0])

    def test_failed_local_append_or_archive_cannot_break_public_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            for sink in (None, Mock(submit=Mock(side_effect=RuntimeError('private')))):
                with patch.object(server,'LOG',Path(tmp)), patch.object(server,'OBSERVATION_ARCHIVE',sink), \
                        patch.object(server.Handler,'log_message'):
                    sock = FakeSocket(b'GET / HTTP/1.0\r\n\r\n')
                    server.Handler(sock, ('127.0.0.1',1), None)
                    self.assertEqual(int(sock.output.getvalue().split()[1]),200)

    def test_concurrent_submissions_respect_capacity_without_duplicates(self):
        from concurrent.futures import ThreadPoolExecutor
        sender = self.sender(max_events=64)
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(lambda _: sender.submit(event()), range(100)))
        self.assertEqual(sum(outcomes), 64)
        self.assertEqual(sender.health()['pending'], 64)
        self.assertEqual(sender.health()['dropped'], 36)
        ids = [json.loads(item[0])['event_id'] for item in sender._queue]
        self.assertEqual(len(set(ids)), 64)

    def test_backoff_prevents_immediate_retry(self):
        transport = Mock()
        transport.send.side_effect = [archive.ArchiveError('connection', True), None]
        sender = self.sender(transport, retry_seconds=.15)
        sender.submit(event()); sender.start()
        wait_for(lambda: sender.health().get('retries') == 1)
        self.assertEqual(transport.send.call_count, 1)
        wait_for(lambda: sender.health().get('stored') == 1)
        self.assertEqual(transport.send.call_count, 2)

    def test_secret_event_remains_only_in_existing_local_log(self):
        transport = Mock()
        sender = self.sender(transport)
        sensitive = 'Authorization: Bearer synthetic-credential-123456'
        with tempfile.TemporaryDirectory() as tmp, patch.object(server,'LOG',Path(tmp)/'requests.jsonl'), \
                patch.object(server,'OBSERVATION_ARCHIVE',sender), patch.object(server.Handler,'log_message'):
            sock = FakeSocket(('GET / HTTP/1.0\r\nUser-Agent: '+sensitive+'\r\n\r\n').encode())
            server.Handler(sock,('127.0.0.1',1),None)
            self.assertEqual(int(sock.output.getvalue().split()[1]),200)
            self.assertEqual(json.loads(server.LOG.read_text())['ua'],sensitive)
            self.assertEqual(sender.health()['rejected'],1)
            self.assertEqual(sender.health()['pending'],0)
            self.assertNotIn(sensitive,str(sender.health()))
            transport.send.assert_not_called()

    def test_excluded_routes_never_submit_or_call_generation_or_crawler(self):
        sink=Mock()
        with tempfile.TemporaryDirectory() as tmp, patch.object(server,'LOG',Path(tmp)/'requests.jsonl'), \
                patch.object(server,'OBSERVATION_ARCHIVE',sink), patch.object(server.Handler,'log_message'), \
                patch('journal_openai.OpenAIJournalAdapter') as adapter, \
                patch('crawler.crawl') as crawler:
            with patch.object(server,'configured_cloudflare_response_store',return_value=None), \
                    patch.object(server,'response_store_for_request',return_value=(None,None)):
                for method,path in [('GET','/stats'),('HEAD','/api/responses/status'),
                                    ('POST','/api/journal/example/responses'),('OPTIONS','/')]:
                    sock=FakeSocket((method+' '+path+' HTTP/1.0\r\n\r\n').encode())
                    server.Handler(sock,('127.0.0.1',1),None)
            sink.submit.assert_not_called()
            adapter.assert_not_called(); crawler.assert_not_called()

    def test_duplicate_ack_is_not_rescued_as_success(self):
        item=event()
        transport=archive.ObservationTransport('https://observation.example/v1/observations','test-only')
        transport._opener=MagicMock()
        response=Mock(status=201)
        response.read.return_value=('{"status":"ignored","status":"stored","event_id":"'+item['event_id']+'"}').encode()
        transport._opener.open.return_value.__enter__.return_value=response
        with self.assertRaises(archive.ArchiveError) as found:
            transport.send(archive.encode_event(item))
        self.assertEqual(found.exception.category,'malformed_response')

    @unittest.skipUnless(__import__('os').name == 'posix', 'SIGKILL model requires POSIX')
    def test_force_killed_process_cannot_recover_memory_queue(self):
        import subprocess
        import sys
        program = ('import json,sys\n'
                   'from observation_archive import ObservationArchive\n'
                   'a=ObservationArchive(None)\n'
                   'a.submit(json.loads(sys.stdin.readline()))\n'
                   'print(a.health()["pending"],flush=True)\n'
                   'sys.stdin.readline()\n')
        child = subprocess.Popen([sys.executable, '-u', '-c', program],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            child.stdin.write(json.dumps(event()) + '\n'); child.stdin.flush()
            self.assertEqual(child.stdout.readline().strip(), '1')
            child.kill(); child.wait(timeout=2)
            self.assertLess(child.returncode, 0)
            fresh = self.sender()
            self.assertEqual(fresh.health()['pending'], 0)
            self.assertEqual(fresh.health().get('stored', 0), 0)
        finally:
            if child.poll() is None:
                child.kill(); child.wait(timeout=2)
            child.stdin.close(); child.stdout.close(); child.stderr.close()
