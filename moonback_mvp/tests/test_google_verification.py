import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from test_v03 import FakeSocket


VERIFICATION_PATH = '/google1c6ec0bf65f9cfd3.html'
# Fingerprint of the supervisor's original download, including its final newline.
ORIGINAL_SHA256 = '2a8ced13bf0831913041b061ba5fd4b22281f56c0a6fbb9db83bed51eec88917'


class GoogleVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        active = patch('server.LOG', Path(self.tmp.name) / 'requests.jsonl')
        active.start()
        self.addCleanup(active.stop)

    def request(self, path, method='GET'):
        sock = FakeSocket((method + ' ' + path + ' HTTP/1.0\r\n\r\n').encode())
        with patch.object(server.Handler, 'log_message'):
            server.Handler(sock, ('127.0.0.1', 12345), None)
        header, body = sock.output.getvalue().split(b'\r\n\r\n', 1)
        lines = header.decode().split('\r\n')
        headers = {key.lower(): value for key, value in
                   (line.split(': ', 1) for line in lines[1:])}
        return int(lines[0].split()[1]), headers, body

    def test_get_preserves_original_file_bytes(self):
        status, headers, body = self.request(VERIFICATION_PATH)
        self.assertEqual(status, 200)
        self.assertIn('text/html', headers['content-type'])
        self.assertEqual(int(headers['content-length']), len(body))
        self.assertEqual(body, (server.ROOT / VERIFICATION_PATH[1:]).read_bytes())
        self.assertEqual(len(body), 53)
        self.assertEqual(hashlib.sha256(body).hexdigest(), ORIGINAL_SHA256)

    def test_head_preserves_headers_without_body(self):
        _, get_headers, body = self.request(VERIFICATION_PATH)
        status, headers, head_body = self.request(VERIFICATION_PATH, 'HEAD')
        self.assertEqual(status, 200)
        self.assertEqual(head_body, b'')
        self.assertEqual(int(headers['content-length']), len(body))
        self.assertEqual(headers['content-type'], get_headers['content-type'])

    def test_similar_names_and_nonliteral_paths_are_not_allowed(self):
        for path in (
            '/google.html', '/google-other.html',
            '/google1c6ec0bf65f9cfd4.html', '/Google1c6ec0bf65f9cfd3.html',
            VERIFICATION_PATH + '.backup', VERIFICATION_PATH + '/',
            VERIFICATION_PATH + '/extra', VERIFICATION_PATH + ';parameter',
            VERIFICATION_PATH + '?check=1', VERIFICATION_PATH + '?',
            VERIFICATION_PATH + '#fragment', '/' + VERIFICATION_PATH,
            '/%67oogle1c6ec0bf65f9cfd3.html',
            '/x/../google1c6ec0bf65f9cfd3.html',
            '/%2e%2e/google1c6ec0bf65f9cfd3.html',
        ):
            for method in ('GET', 'HEAD'):
                with self.subTest(path=path, method=method):
                    status, _, body = self.request(path, method)
                    self.assertEqual(status, 404)
                    if method == 'HEAD':
                        self.assertEqual(body, b'')

    def test_other_files_remain_private_even_when_present(self):
        private_paths = ('google-other.html', 'private.html', '.env',
                         'drafts/archive/private.json', 'data/private.html')
        root = Path(self.tmp.name)
        for path in private_paths:
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('PRIVATE_TEST_MARKER')
        with patch('server.ROOT', root):
            for path in private_paths:
                for method in ('GET', 'HEAD'):
                    with self.subTest(path=path, method=method):
                        status, _, body = self.request('/' + path, method)
                        self.assertEqual(status, 404)
                        self.assertNotIn(b'PRIVATE_TEST_MARKER', body)


if __name__ == '__main__':
    unittest.main()
