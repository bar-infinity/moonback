import json
import socket
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import server
from response_store import CloudflareStoreError
from test_v03 import FakeSocket


class ObservationAccuracyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.log = self.root / 'requests.jsonl'
        active = patch('server.LOG', self.log)
        active.start()
        self.addCleanup(active.stop)
        network = patch('socket.create_connection', side_effect=AssertionError('Network prohibited'))
        network.start()
        self.addCleanup(network.stop)
        logging = patch.object(server.Handler, 'log_message')
        logging.start()
        self.addCleanup(logging.stop)

    def request(self, path='/', method='GET', headers='', socket_type=FakeSocket):
        sock = socket_type((method + ' ' + path + ' HTTP/1.0\r\n'
                            + headers + '\r\n').encode())
        server.Handler(sock, ('127.0.0.1', 12345), None)
        return sock.output.getvalue()

    def rows(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_get_head_and_error_status_are_recorded_once(self):
        for path, method, status in (('/', 'GET', 200), ('/', 'HEAD', 200),
                                     ('/missing', 'GET', 404), ('/missing', 'HEAD', 404)):
            response = self.request(path, method)
            self.assertEqual(int(response.split()[1]), status)
        self.assertEqual([(r['method'], r['status']) for r in self.rows()],
                         [('GET', 200), ('HEAD', 200), ('GET', 404), ('HEAD', 404)])
        self.assertTrue(all(r['response_completed'] for r in self.rows()))

    def test_unavailable_data_records_503_for_get_and_head(self):
        with patch('server.render_page', side_effect=ValueError('private diagnostic')):
            for method in ('GET', 'HEAD'):
                response = self.request('/journal', method)
                self.assertIn(b'503', response)
                self.assertNotIn(b'private diagnostic', response)
        self.assertEqual([r['status'] for r in self.rows()], [503, 503])
        self.assertNotIn('private diagnostic', self.log.read_text())

    def test_status_api_conditions_remain_excluded(self):
        for configured, failure, expected in ((False, None, 404), (True, None, 200),
                                             (True, 'unavailable', 503), (True, 'bad_gateway', 502)):
            store = Mock()
            store.get_status.return_value = {'pending': False, 'cursor': None}
            if failure:
                store.get_status.side_effect = CloudflareStoreError(failure)
            with patch('server.configured_cloudflare_response_store', return_value=store if configured else None), \
                    patch('server.RESPONSE_STATUS_CACHE', server.ResponseStatusCache(ttl=0)):
                for method in ('GET', 'HEAD'):
                    self.assertEqual(int(self.request('/api/responses/status', method).split()[1]), expected)
        self.assertEqual(self.rows(), [])

    def test_post_options_and_stats_remain_excluded(self):
        with patch('server.response_store_for_request', return_value=(None, None)):
            self.assertIn(b'404', self.request('/api/journal/example/responses', 'POST'))
        self.assertIn(b'405', self.request('/', 'OPTIONS'))
        self.assertIn(b'200', self.request('/stats'))
        self.assertEqual(self.rows(), [])

    def test_no_new_private_fields_or_query_values_and_ua_limit(self):
        ua = 'ExampleClient-' + 'x' * 200
        self.request('/tools/?utm_source=private-query-marker', headers=(
            'User-Agent: ' + ua + '\r\nCookie: private-cookie-marker\r\n'
            'Authorization: private-auth-marker\r\n'))
        row = self.rows()[0]
        self.assertEqual(set(row), {'time', 'path', 'method', 'status',
                                    'response_completed', 'ua', 'agent_like'})
        self.assertEqual(row['path'], '/tools/')
        self.assertEqual(row['ua'], ua[:180])
        for marker in ('private-query-marker', 'private-cookie-marker', 'private-auth-marker', '127.0.0.1'):
            self.assertNotIn(marker, self.log.read_text())

    def test_old_and_new_rows_keep_counts_without_inventing_fields(self):
        old = dict(time='2026-11-04T00:00:00+00:00', path='/signal/005', ua='oldbot', agent_like=True)
        self.log.write_text(json.dumps(old) + '\n')
        self.request('/', headers='User-Agent: ExampleClient\r\n')
        stats = server.read_stats()
        self.assertEqual(stats['total_requests'], 2)
        self.assertEqual(stats['agent_like_requests'], 1)
        self.assertEqual(stats['deepest_signal'], '005')
        self.assertEqual(stats['observation_started'], old['time'])
        self.assertEqual(stats['recent'][1], old)
        self.assertEqual(stats['recent'][0]['status'], 200)

    def test_log_write_failure_does_not_change_response(self):
        for method in ('GET', 'HEAD'):
            baseline = self.request('/', method)
            with patch.object(Path, 'open', side_effect=PermissionError('private diagnostic')):
                # Static serving uses builtins.open; only the observation write fails.
                response = self.request('/', method)
            self.assertEqual(response.split(b'\r\n', 1)[0], baseline.split(b'\r\n', 1)[0])
            self.assertEqual(response.split(b'\r\n\r\n', 1)[1], baseline.split(b'\r\n\r\n', 1)[1])
        self.assertEqual(len(self.rows()), 2)

    def test_header_send_failure_has_unknown_status(self):
        class BrokenHeaders(FakeSocket):
            def sendall(self, data):
                raise BrokenPipeError('private diagnostic')
        with self.assertRaises(BrokenPipeError):
            self.request(socket_type=BrokenHeaders)
        self.assertIsNone(self.rows()[0]['status'])
        self.assertFalse(self.rows()[0]['response_completed'])
        self.assertEqual(len(self.rows()), 1)

    def test_body_send_failure_retains_status_but_is_not_complete(self):
        class BrokenBody(FakeSocket):
            def sendall(self, data):
                if self.output.getvalue():
                    raise BrokenPipeError('private diagnostic')
                super().sendall(data)
        with self.assertRaises(BrokenPipeError):
            self.request(socket_type=BrokenBody)
        self.assertEqual(self.rows()[0]['status'], 200)
        self.assertFalse(self.rows()[0]['response_completed'])
        self.assertEqual(len(self.rows()), 1)

    def test_unhandled_failure_before_response_does_not_invent_500(self):
        with patch.object(server.Handler, 'serve', side_effect=RuntimeError('private diagnostic')):
            with self.assertRaises(RuntimeError):
                self.request()
        self.assertIsNone(self.rows()[0]['status'])
        self.assertFalse(self.rows()[0]['response_completed'])
        self.assertNotIn('private diagnostic', self.log.read_text())

    def test_timeout_during_flush_marks_incomplete(self):
        with patch('socketserver._SocketWriter.flush', side_effect=[socket.timeout('private diagnostic'), None, None]):
            self.request()
        self.assertEqual(self.rows()[0]['status'], 200)
        self.assertFalse(self.rows()[0]['response_completed'])
        self.assertEqual(len(self.rows()), 1)

    def test_concurrent_requests_keep_individual_json_records(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: self.request('/llms.txt'), range(16)))
        self.assertTrue(all(b'200 OK' in reply for reply in replies))
        self.assertEqual(len(self.rows()), 16)
        self.assertTrue(all(r['status'] == 200 and r['method'] == 'GET' for r in self.rows()))

    def test_conditional_response_records_actual_304(self):
        response = self.request('/llms.txt', headers='If-Modified-Since: Wed, 01 Jan 2099 00:00:00 GMT\r\n')
        self.assertIn(b'304', response)
        self.assertEqual(self.rows()[0]['status'], 304)

    def test_keep_alive_resets_observation_for_each_request(self):
        sock = FakeSocket(b'GET /llms.txt HTTP/1.1\r\nHost: localhost\r\n\r\n'
                          b'GET /stats HTTP/1.1\r\nHost: localhost\r\n\r\n'
                          b'HEAD / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
        with patch.object(server.Handler, 'protocol_version', 'HTTP/1.1'):
            server.Handler(sock, ('127.0.0.1', 12345), None)
        self.assertEqual([(r['method'], r['path'], r['status']) for r in self.rows()],
                         [('GET', '/llms.txt', 200), ('HEAD', '/', 200)])

    def test_no_observation_is_written_before_content_send(self):
        owner = self

        class InspectWrite(FakeSocket):
            def sendall(self, data):
                owner.assertFalse(owner.log.exists())
                super().sendall(data)

        self.request(socket_type=InspectWrite)
        self.assertEqual(len(self.rows()), 1)
        self.assertTrue(self.rows()[0]['response_completed'])
