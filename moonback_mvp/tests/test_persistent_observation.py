import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import server
from test_v03 import FakeSocket


class PersistentObservationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.path = self.root / 'mount' / 'moonback' / 'requests.jsonl'
        for active in (patch('server.LOG', self.path),
                       patch.object(server.Handler, 'log_message'),
                       patch('socket.create_connection', side_effect=AssertionError('Network prohibited'))):
            active.start()
            self.addCleanup(active.stop)

    def request(self, path='/', method='GET', handler=None):
        cls = handler or server.Handler
        sock = FakeSocket((method + ' ' + path + ' HTTP/1.0\r\nUser-Agent: PersistenceTest/1.0\r\n\r\n').encode())
        cls(sock, ('127.0.0.1', 12345), None)
        header, body = sock.output.getvalue().split(b'\r\n\r\n', 1)
        return int(header.split()[1]), body

    def restart(self):
        namespace = {'__name__': 'restart_test', '__file__': server.__file__}
        with patch.dict(os.environ, {'MOONBACK_OBSERVATION_LOG_PATH': str(self.path)}, clear=True):
            exec(compile(Path(server.__file__).read_text(), server.__file__, 'exec'), namespace)
        namespace['Handler'].log_message = lambda *args: None
        return namespace

    def test_unset_environment_preserves_default_path(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(server.observation_log_path(), server.ROOT / 'data' / 'requests.jsonl')

    def test_custom_startup_path_is_fixed_and_creates_no_file_on_import(self):
        instance = self.restart()
        self.assertEqual(instance['LOG'], self.path)
        self.assertFalse(self.path.exists())
        # A writable ordinary directory also works: path configuration is not
        # evidence that a persistent disk was mounted by the hosting platform.
        self.assertFalse(os.path.ismount(self.root))
        with patch.dict(os.environ, {'MOONBACK_OBSERVATION_LOG_PATH': str(self.root / 'other.jsonl')}):
            self.request(handler=instance['Handler'])
        self.assertTrue(self.path.exists())
        self.assertFalse((self.root / 'other.jsonl').exists())

    def test_invalid_paths_fail_with_fixed_message(self):
        for value in ('', ' ', 'relative.jsonl', '/tmp/../requests.jsonl', '/tmp/log.txt',
                      '/tmp/bad\x00.jsonl', '/tmp/bad\x7f.jsonl', '/tmp/bad\u202e.jsonl',
                      str(server.ROOT / 'published' / 'requests.jsonl'),
                      str(server.ROOT.parent / '.git' / 'requests.jsonl')):
            with self.subTest(value=value), patch('server.os.environ', {'MOONBACK_OBSERVATION_LOG_PATH': value}):
                with self.assertRaises(ValueError) as caught:
                    server.observation_log_path()
                self.assertEqual(str(caught.exception), 'Invalid MOONBACK_OBSERVATION_LOG_PATH configuration')

    def test_directory_and_symlink_destinations_are_rejected(self):
        directory = self.root / 'directory.jsonl'
        directory.mkdir()
        link = self.root / 'link.jsonl'
        link.symlink_to(self.root / 'target.jsonl')
        for path in (directory, link):
            with patch.dict(os.environ, {'MOONBACK_OBSERVATION_LOG_PATH': str(path)}):
                with self.assertRaises(ValueError):
                    server.observation_log_path()

    def test_restart_preserves_old_bytes_and_reads_same_custom_file(self):
        self.path.parent.mkdir(parents=True)
        old = {'time': '2026-11-04T00:00:00+00:00', 'path': '/signal/004', 'ua': 'old', 'agent_like': True}
        original = (json.dumps(old) + '\n').encode()
        self.path.write_bytes(original)
        first = self.restart()
        self.request('/llms.txt', handler=first['Handler'])
        after_first = self.path.read_bytes()
        second = self.restart()
        self.assertEqual(self.path.read_bytes(), after_first)
        self.assertTrue(after_first.startswith(original))
        self.assertEqual(second['read_stats']()['total_requests'], 2)
        self.request('/missing', 'HEAD', second['Handler'])
        stats = second['read_stats']()
        self.assertEqual(stats['total_requests'], 3)
        self.assertEqual(stats['deepest_signal'], '004')
        self.assertEqual(stats['agent_like_requests'], 1)
        self.assertEqual(stats['recent'][-1], old)
        self.assertEqual((stats['recent'][0]['method'], stats['recent'][0]['status']), ('HEAD', 404))
        self.assertTrue(self.path.read_bytes().startswith(after_first))

    def test_existing_last_record_without_newline_is_not_overwritten(self):
        self.path.parent.mkdir(parents=True)
        original = json.dumps({'path': '/old', 'agent_like': False}).encode()
        self.path.write_bytes(original)
        self.request()
        self.assertTrue(self.path.read_bytes().startswith(original + b'\n'))
        self.assertEqual(server.read_stats()['total_requests'], 2)

    def test_missing_file_is_empty_without_creating_or_resetting(self):
        self.assertEqual(server.read_stats()['total_requests'], 0)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists())

    def test_read_failure_returns_fixed_503_and_does_not_leak_path(self):
        with patch.object(Path, 'open', side_effect=PermissionError('private diagnostic')):
            for method in ('GET', 'HEAD'):
                code, body = self.request('/stats', method)
                self.assertEqual(code, 503)
                if method == 'GET':
                    self.assertEqual(json.loads(body), {'error': 'observation_unavailable'})
                else:
                    self.assertEqual(body, b'')
                self.assertNotIn(str(self.path).encode(), body)
                self.assertNotIn(b'private diagnostic', body)
        self.assertFalse(self.path.exists())

    def test_write_failure_keeps_content_and_does_not_fall_back(self):
        with patch.object(Path, 'open', side_effect=PermissionError('private diagnostic')):
            self.assertEqual(self.request()[0], 200)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.root.rglob('*.jsonl')), [])

    def test_parent_creation_failure_keeps_response(self):
        with patch.object(Path, 'mkdir', side_effect=OSError('private diagnostic')):
            self.assertEqual(self.request()[0], 200)
        self.assertFalse(self.path.exists())
        # A structurally valid but mistaken mount path can be blocked by a file.
        blocker = self.root / 'mount'
        blocker.write_text('Synthetic path blocker')
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.request('/stats')[0], 503)
        self.assertEqual(blocker.read_text(), 'Synthetic path blocker')

    def test_unreadable_encoding_is_unavailable_not_empty(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b'\xff\xfe\n')
        self.assertEqual(self.request('/stats')[0], 503)
        self.assertEqual(self.path.read_bytes(), b'\xff\xfe\n')

    def test_large_log_is_streamed_with_bounded_recent_results(self):
        self.path.parent.mkdir(parents=True)
        with self.path.open('w') as stream:
            stream.write('not json\n[]\n')
            for i in range(10000):
                stream.write(json.dumps({'path': '/', 'time': str(i), 'agent_like': False}) + '\n')
        before = self.path.stat().st_size
        with patch.object(Path, 'read_text', side_effect=AssertionError('Whole-file read prohibited')):
            stats = server.read_stats()
        self.assertEqual(stats['total_requests'], 10000)
        self.assertEqual(len(stats['recent']), 20)
        self.assertEqual(stats['recent'][0]['time'], '9999')
        self.assertEqual(stats['observation_started'], '0')
        self.assertEqual(self.path.stat().st_size, before)

    def test_concurrent_appends_and_methods_survive_reload(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda i: self.request('/llms.txt', 'HEAD' if i % 2 else 'GET'), range(20)))
        self.assertTrue(all(code == 200 for code, _ in replies))
        stats = self.restart()['read_stats']()
        self.assertEqual(stats['total_requests'], 20)
        self.assertEqual(sum(row['method'] == 'HEAD' for row in stats['recent']), 10)
        self.assertTrue(all(row['status'] == 200 and row['response_completed'] for row in stats['recent']))

    def test_custom_file_never_becomes_a_public_route(self):
        self.request()
        for path in ('/requests.jsonl', '/data/requests.jsonl', str(self.path)):
            for method in ('GET', 'HEAD'):
                self.assertEqual(self.request(path, method)[0], 404)

    def test_separate_python_process_reads_previous_process_log(self):
        code = '''import json, sys
import server
from test_v03 import FakeSocket
server.Handler.log_message = lambda *args: None
if sys.argv[1] == 'write':
    sock = FakeSocket(b'HEAD /llms.txt HTTP/1.0\\r\\n\\r\\n')
    server.Handler(sock, ('127.0.0.1', 12345), None)
stats = server.read_stats()
print(json.dumps({'total': stats['total_requests'], 'recent': stats['recent']}))
'''
        environment = {'MOONBACK_OBSERVATION_LOG_PATH': str(self.path),
                       'PYTHONPATH': os.pathsep.join((str(server.ROOT), str(server.ROOT / 'tests'))),
                       'PATH': os.defpath}
        first = subprocess.run([sys.executable, '-c', code, 'write'], env=environment,
                               check=True, capture_output=True, text=True)
        original = self.path.read_bytes()
        second = subprocess.run([sys.executable, '-c', code, 'read'], env=environment,
                                check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(first.stdout), json.loads(second.stdout))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(json.loads(second.stdout)['total'], 1)
        self.assertEqual(json.loads(second.stdout)['recent'][0]['method'], 'HEAD')
