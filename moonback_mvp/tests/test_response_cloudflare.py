import io
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import server
from content import FileStore
from response import build_draft_response
from response_store import CloudflareResponseStore, CloudflareStoreError


JOURNAL_ID = 'journal-cloudflare-test'
WORKER_URL = 'https://moonback-response-intake.example.workers.dev/v1/responses'
SECRET = 'test-only-worker-secret'


def journal_row():
    return {
        'id': JOURNAL_ID,
        'date': '2026-09-30',
        'text': 'A published Journal observation.',
        'source_urls': ['https://example.org/journal'],
    }


def submitted():
    return {
        'agent_name': 'Example Agent',
        'response': 'A short response.',
        'source_url': 'https://example.org/agent',
    }


def draft():
    return build_draft_response(
        submitted(), JOURNAL_ID,
        response_id='response-cloudflare-test-001',
    )


class FakeSocket:
    def __init__(self, request):
        self.input = io.BytesIO(request)
        self.output = io.BytesIO()

    def makefile(self, *args, **kwargs):
        return self.input

    def sendall(self, data):
        self.output.write(data)


class CloudflareResponseStoreTests(unittest.TestCase):
    def test_valid_request_is_sent_once_with_authorization(self):
        calls = []

        def transport(payload, headers):
            calls.append((payload, headers))
            ident = json.loads(payload)['id']
            return 201, json.dumps({
                'status': 'stored', 'receipt_id': ident,
            }).encode()

        store = CloudflareResponseStore(
            WORKER_URL, SECRET, timeout=3, transport=transport)
        self.assertEqual(store.save_draft(draft()), 'response-cloudflare-test-001')
        self.assertEqual(len(calls), 1)
        payload, headers = calls[0]
        self.assertEqual(headers['Authorization'], 'Bearer ' + SECRET)
        self.assertEqual(headers['Content-Type'], 'application/json')
        self.assertEqual(
            headers['User-Agent'], CloudflareResponseStore.USER_AGENT)
        self.assertTrue(headers['User-Agent'].startswith('Mozilla/5.0 '))
        self.assertNotIn('Python-urllib', headers['User-Agent'])
        self.assertNotIn(SECRET, headers['User-Agent'])
        for value in json.loads(payload).values():
            if value:
                self.assertNotIn(str(value), headers['User-Agent'])
        self.assertEqual(set(json.loads(payload)), {
            'id', 'journal_id', 'received_at', 'agent_name', 'response',
            'source_url'})

    def test_timeout_connection_and_provider_failure_do_not_retry(self):
        for failure in (TimeoutError('private timeout'),
                        ConnectionError('private connection')):
            calls = []

            def transport(payload, headers):
                calls.append(1)
                raise failure

            store = CloudflareResponseStore(WORKER_URL, SECRET, transport=transport)
            with self.assertRaisesRegex(CloudflareStoreError, '^unavailable$'):
                store.save_draft(draft())
            self.assertEqual(calls, [1])

        calls = []

        def failed(payload, headers):
            calls.append(1)
            return 503, b'provider-private-response'

        store = CloudflareResponseStore(WORKER_URL, SECRET, transport=failed)
        with self.assertRaisesRegex(CloudflareStoreError, '^unavailable$'):
            store.save_draft(draft())
        self.assertEqual(calls, [1])

    def test_malformed_success_response_is_rejected_safely(self):
        for body in (b'not-json', b'{"status":"stored"}',
                     b'{"status":"stored","receipt_id":"wrong"}'):
            store = CloudflareResponseStore(
                WORKER_URL, SECRET, transport=lambda payload, headers: (201, body))
            with self.assertRaisesRegex(CloudflareStoreError, '^bad_gateway$'):
                store.save_draft(draft())

    def test_worker_url_and_secret_are_strict(self):
        for url in ('http://worker.example/v1/responses',
                    'https://worker.example/other',
                    'https://name:secret@worker.example/v1/responses',
                    'https://worker.example/v1/responses?x=1'):
            with self.assertRaises(ValueError):
                CloudflareResponseStore(url, SECRET)
        with self.assertRaises(ValueError):
            CloudflareResponseStore(WORKER_URL, '')

    def test_secret_and_provider_body_never_enter_exception(self):
        marker = 'provider-private-marker'
        store = CloudflareResponseStore(
            WORKER_URL, SECRET,
            transport=lambda payload, headers: (500, marker.encode()),
        )
        with self.assertRaises(CloudflareStoreError) as caught:
            store.save_draft(draft())
        message = str(caught.exception)
        self.assertNotIn(SECRET, message)
        self.assertNotIn(marker, message)

    def test_status_url_authentication_and_schema_are_fixed(self):
        calls = []
        cursor = 'sha256:' + ('a' * 64)

        def status_transport(url, headers):
            calls.append((url, headers))
            return 200, json.dumps({
                'pending': True, 'cursor': cursor,
            }).encode()

        store = CloudflareResponseStore(
            WORKER_URL, SECRET, status_transport=status_transport)
        self.assertEqual(store.get_status(), {
            'pending': True, 'cursor': cursor,
        })
        self.assertEqual(len(calls), 1)
        url, headers = calls[0]
        self.assertEqual(
            url,
            'https://moonback-response-intake.example.workers.dev'
            '/v1/responses/status',
        )
        self.assertEqual(headers['Authorization'], 'Bearer ' + SECRET)
        self.assertEqual(headers['User-Agent'], CloudflareResponseStore.USER_AGENT)
        self.assertNotIn(SECRET, url)

    def test_status_timeout_and_malformed_response_are_safe(self):
        calls = []

        def timeout(url, headers):
            calls.append(1)
            raise TimeoutError('private timeout')

        store = CloudflareResponseStore(
            WORKER_URL, SECRET, status_transport=timeout)
        with self.assertRaisesRegex(CloudflareStoreError, '^unavailable$'):
            store.get_status()
        self.assertEqual(calls, [1])

        malformed = (
            (200, b'not-json'),
            (200, b'{"pending":false}'),
            (200, b'{"pending":1,"cursor":null}'),
            (200, b'{"pending":true,"cursor":null}'),
            (200, b'{"pending":true,"cursor":"response-private"}'),
            (503, b'{"error":"storage_unavailable"}'),
        )
        for status, body in malformed:
            store = CloudflareResponseStore(
                WORKER_URL, SECRET,
                status_transport=lambda url, headers, pair=(status, body): pair,
            )
            category = 'unavailable' if status == 503 else 'bad_gateway'
            with self.assertRaisesRegex(CloudflareStoreError, '^' + category + '$'):
                store.get_status()


class CloudflareServerSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journals = FileStore(Path(self.tmp.name))
        self.journals.save('published', 'journal', journal_row())
        self.limiter = server.LocalResponseRateLimiter(
            source_limit=100, journal_limit=100, window=60)

    def request(self, environment, cloud_store=None, client='203.0.113.10'):
        body = json.dumps(submitted()).encode()
        raw = ('\r\n'.join([
            f'POST /api/journal/{JOURNAL_ID}/responses HTTP/1.0',
            'Host: localhost',
            'Content-Type: application/json',
            'Content-Length: ' + str(len(body)),
            '', '',
        ])).encode() + body
        sock = FakeSocket(raw)
        patches = [
            patch.dict(os.environ, environment, clear=True),
            patch('server.FileStore', return_value=self.journals),
            patch('server.RESPONSE_RATE_LIMITER', self.limiter),
            patch.object(server.Handler, 'log_message'),
        ]
        if cloud_store is not None:
            patches.append(patch('server.CloudflareResponseStore',
                                 return_value=cloud_store))
        for active in patches:
            active.start()
            self.addCleanup(active.stop)
        server.Handler(sock, (client, 12345), None)
        for active in reversed(patches):
            active.stop()
        return sock.output.getvalue()

    def status_request(self, environment, cloud_store=None, *, method='GET',
                       path='/api/responses/status', cache=None):
        raw = ('\r\n'.join([
            f'{method} {path} HTTP/1.0',
            'Host: localhost',
            '', '',
        ])).encode()
        sock = FakeSocket(raw)
        patches = [
            patch.dict(os.environ, environment, clear=True),
            patch('server.RESPONSE_STATUS_CACHE',
                  cache or server.ResponseStatusCache(ttl=30)),
            patch.object(server.Handler, 'log_message'),
        ]
        if cloud_store is not None:
            patches.append(patch('server.CloudflareResponseStore',
                                 return_value=cloud_store))
        for active in patches:
            active.start()
            self.addCleanup(active.stop)
        server.Handler(sock, ('203.0.113.10', 12345), None)
        for active in reversed(patches):
            active.stop()
        return sock.output.getvalue()

    def cloud_environment(self, **changes):
        environment = {
            'MOONBACK_RESPONSE_BACKEND': 'cloudflare',
            'MOONBACK_RESPONSE_WORKER_URL': WORKER_URL,
            'MOONBACK_RESPONSE_WORKER_SECRET': SECRET,
        }
        environment.update(changes)
        return environment

    def test_backend_and_all_three_settings_are_required(self):
        cases = [
            {},
            {'MOONBACK_RESPONSE_WORKER_URL': WORKER_URL,
             'MOONBACK_RESPONSE_WORKER_SECRET': SECRET},
            {'MOONBACK_RESPONSE_BACKEND': 'cloudflare'},
            {'MOONBACK_RESPONSE_BACKEND': 'cloudflare',
             'MOONBACK_RESPONSE_WORKER_URL': WORKER_URL},
            {'MOONBACK_RESPONSE_BACKEND': 'cloudflare',
             'MOONBACK_RESPONSE_WORKER_SECRET': SECRET},
        ]
        for environment in cases:
            with self.subTest(environment=tuple(environment)):
                response = self.request(environment)
                self.assertIn(b'404 Not Found', response)

    def test_worker_success_returns_202(self):
        store = Mock()
        response = self.request(self.cloud_environment(), store)
        self.assertIn(b'202 Accepted', response)
        store.save_draft.assert_called_once()
        payload = json.loads(response.split(b'\r\n\r\n', 1)[1])
        self.assertEqual(payload['status'], 'accepted_for_review')
        self.assertNotIn(b'Example Agent', response)

    def test_worker_error_categories_return_5xx_or_429(self):
        for category, expected in (
                ('unavailable', b'503'), ('bad_gateway', b'502'),
                ('rate_limited', b'429')):
            store = Mock()
            store.save_draft.side_effect = CloudflareStoreError(category)
            response = self.request(self.cloud_environment(), store)
            self.assertIn(expected, response)
            store.save_draft.assert_called_once()
            self.assertNotIn(SECRET.encode(), response)

    def test_cloudflare_failure_never_falls_back_to_filesystem(self):
        store = Mock()
        store.save_draft.side_effect = CloudflareStoreError('unavailable')
        with patch('server.FileResponseStore',
                   side_effect=AssertionError('filesystem fallback')):
            response = self.request(self.cloud_environment(), store)
        self.assertIn(b'503', response)

    def test_cloudflare_path_never_calls_openai_or_crawler(self):
        store = Mock()
        with patch('journal.generate_journal_entry') as generate, \
             patch('journal_openai.OpenAIJournalAdapter') as adapter, \
             patch('crawler.crawl') as crawl, \
             patch('socket.create_connection',
                   side_effect=AssertionError('network forbidden')):
            response = self.request(self.cloud_environment(), store)
        self.assertIn(b'202', response)
        generate.assert_not_called()
        adapter.assert_not_called()
        crawl.assert_not_called()

    def test_public_status_is_minimal_cached_and_supports_head(self):
        cursor = 'sha256:' + ('b' * 64)
        store = Mock()
        store.get_status.return_value = {'pending': True, 'cursor': cursor}
        cache = server.ResponseStatusCache(ttl=30)
        first = self.status_request(self.cloud_environment(), store, cache=cache)
        second = self.status_request(self.cloud_environment(), store, cache=cache)
        head = self.status_request(
            self.cloud_environment(), store, method='HEAD', cache=cache)
        for response in (first, second):
            self.assertIn(b'200 OK', response)
            self.assertIn(b'application/json; charset=utf-8', response)
            self.assertIn(b'public, max-age=30, must-revalidate', response)
            self.assertEqual(
                json.loads(response.split(b'\r\n\r\n', 1)[1]),
                {'pending': True, 'cursor': cursor},
            )
        self.assertIn(b'200 OK', head)
        self.assertEqual(head.split(b'\r\n\r\n', 1)[1], b'')
        store.get_status.assert_called_once()
        serialized = first.lower()
        for forbidden in (
                b'agent_name', b'response text', b'source_url', b'journal_id',
                b'received_at', b'response-cloudflare-test-001', SECRET.encode()):
            self.assertNotIn(forbidden, serialized)

    def test_public_status_is_off_without_complete_cloudflare_settings(self):
        cases = [
            {},
            {'MOONBACK_RESPONSE_BACKEND': 'cloudflare'},
            {'MOONBACK_RESPONSE_BACKEND': 'cloudflare',
             'MOONBACK_RESPONSE_WORKER_URL': WORKER_URL},
            {'MOONBACK_RESPONSE_WORKER_URL': WORKER_URL,
             'MOONBACK_RESPONSE_WORKER_SECRET': SECRET},
        ]
        for environment in cases:
            response = self.status_request(environment)
            self.assertIn(b'404 Not Found', response)

    def test_public_status_upstream_failures_do_not_become_no_pending(self):
        for category, expected in (
                ('unavailable', b'503'), ('bad_gateway', b'502')):
            store = Mock()
            store.get_status.side_effect = CloudflareStoreError(category)
            response = self.status_request(self.cloud_environment(), store)
            self.assertIn(expected, response)
            self.assertNotIn(b'"pending":false', response)
            store.get_status.assert_called_once()

    def test_public_status_never_calls_generation_or_crawler(self):
        store = Mock()
        store.get_status.return_value = {'pending': False, 'cursor': None}
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / 'requests.jsonl'
            with patch('journal.generate_journal_entry') as generate, \
                 patch('journal_openai.OpenAIJournalAdapter') as adapter, \
                 patch('crawler.crawl') as crawl, \
                 patch('server.LOG', log_path), \
                 patch('socket.create_connection',
                       side_effect=AssertionError('network forbidden')):
                response = self.status_request(self.cloud_environment(), store)
            self.assertFalse(log_path.exists())
        self.assertIn(b'200 OK', response)
        generate.assert_not_called()
        adapter.assert_not_called()
        crawl.assert_not_called()

    def test_expired_cache_does_not_hide_an_upstream_failure(self):
        cache = server.ResponseStatusCache(ttl=0)
        self.assertEqual(
            cache.get(lambda: {'pending': False, 'cursor': None}),
            {'pending': False, 'cursor': None},
        )

        def unavailable():
            raise CloudflareStoreError('unavailable')

        with self.assertRaisesRegex(CloudflareStoreError, '^unavailable$'):
            cache.get(unavailable)


if __name__ == '__main__':
    unittest.main()
