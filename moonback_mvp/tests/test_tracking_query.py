import hashlib
import io
import json
from contextlib import redirect_stderr
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import server
import tools_web
from test_v03 import FakeSocket


ARCHIVE = '/archive/llms-txt'
JOURNAL = '/journal/journal-2026-09-28-a68378fd20d74f04b29265498b8cedec'
TRACKING = '?utm_source=chatgpt.com'
PAGES = ('/archive', '/journal', ARCHIVE, JOURNAL, *tools_web.TOOL_PAGES)


class TrackingQueryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log = Path(tmp.name) / 'requests.jsonl'
        active = patch('server.LOG', self.log)
        active.start()
        self.addCleanup(active.stop)
        self.access_log = io.StringIO()

    def request(self, path, method='GET'):
        request = (f'{method} {path} HTTP/1.0\r\n'
                   'User-Agent: testbot\r\n\r\n').encode('ascii')
        sock = FakeSocket(request)
        with redirect_stderr(self.access_log):
            server.Handler(sock, ('127.0.0.1', 12345), None)
        head, body = sock.output.getvalue().split(b'\r\n\r\n', 1)
        status, *lines = head.decode('ascii').split('\r\n')
        headers = dict(line.split(': ', 1) for line in lines)
        headers.pop('Date', None)
        return int(status.split()[1]), headers, body

    def test_get_and_head_are_identical_with_tracking_on_all_target_routes(self):
        for path in (*PAGES, tools_web.MANIFEST_PATH, *tools_web.SAMPLE_ROUTES):
            with self.subTest(path=path):
                plain = self.request(path)
                self.assertEqual(plain[0], 200)
                self.assertEqual(self.request(path + TRACKING), plain)
                head = self.request(path + TRACKING, 'HEAD')
                self.assertEqual(head, (200, plain[1], b''))
                self.assertEqual(self.request(path, 'HEAD'), head)

    def test_all_allowed_keys_and_encoded_values(self):
        query = ('?utm_source=chatgpt.com&utm_medium=referral&utm_campaign=spring'
                 '&utm_content=a%20b%26c&utm_term=one+two&gclid=Abc_123&fbclid=xyz-456')
        for path in (*PAGES, tools_web.MANIFEST_PATH, *tools_web.SAMPLE_ROUTES):
            for method in ('GET', 'HEAD'):
                with self.subTest(path=path, method=method):
                    plain = self.request(path, method)
                    self.assertEqual(plain[0], 200)
                    self.assertEqual(self.request(path + query, method), plain)
        self.assertEqual(self.request('/tools/?utm_content=%E6%9C%88')[0], 200)

    def test_download_bytes_and_hashes_are_unchanged(self):
        raw = (tools_web.DATASET / 'cases.json').read_bytes()
        self.assertEqual(self.request(tools_web.MANIFEST_PATH + TRACKING)[2], raw)
        for case in json.loads(raw)['cases']:
            with self.subTest(case=case['id']):
                b = self.request(tools_web.DATASET_PATH + case['file'] + TRACKING)[2]
                self.assertEqual(b, (tools_web.DATASET / case['file']).read_bytes())
                self.assertEqual(hashlib.sha256(b).hexdigest(), case['sha256'])

    def test_rejects_unknown_duplicate_or_malformed_queries(self):
        queries = (
            '?x=1', '?=a', '?utm_source=a&=b', '?utm_source=a&x=1',
            '?UTM_source=a', '?%75tm_source=a',
            '?utm_source=a&utm_source=b', '?utm_source', '?utm_source=',
            '?', '?utm_source=a&', '?utm_source=a&&utm_term=b',
            '?utm_source=a;utm_medium=b', '?utm_source=a=b', '?utm_source=a?b',
            '?utm_source=a#b', '?utm_source=%', '?utm_source=%GG',
            '?utm_source=%0a', '?utm_source=%00', '?utm_source=%7F',
            '?utm_source=%FF', '?utm_source=%C0%AF', '?utm_source=%E6%9C',
            '?utm_source=%ED%A0%80',
            '?utm_source=' + 'x' * 513,
            '?' + '&'.join(key + '=' + 'x' * 512 for key in
                           ('utm_source', 'utm_medium', 'utm_campaign', 'utm_content')),
            '?' + '&'.join(['utm_source=a'] * 8),
        )
        for path in ('/tools/', '/archive', JOURNAL, tools_web.MANIFEST_PATH):
            for query in queries:
                for method in ('GET', 'HEAD'):
                    with self.subTest(path=path, query=query[:60], method=method):
                        self.assertEqual(self.request(path + query, method)[0], 404)

    def test_accepts_value_at_limit(self):
        self.assertEqual(self.request(ARCHIVE + '?utm_source=' + 'x' * 512)[0], 200)

    def test_tracking_does_not_relax_path_or_other_route_rules(self):
        paths = (
            '/%74ools/', '/%2574ools/', '//tools/', '/tools', '/tools/../archive',
            '/tools/%2e%2e/archive', '/tools/%252e%252e/archive',
            '/tools/;params', '/archive;params', '/archive/llms-txt;params',
            '/archive/%6clms-txt', '/archive/unknown', '/journal/unknown',
            tools_web.DATASET_PATH + 'samples/unknown.csv',
            '/drafts/archive/llms-txt.json', '/data/requests.jsonl',
            '/published/archive/llms-txt.json', '/.env',
            '/v1/admin/responses', '/api/responses/status',
            '/google1c6ec0bf65f9cfd3.html',
        )
        for path in paths:
            for method in ('GET', 'HEAD'):
                with self.subTest(path=path, method=method):
                    self.assertEqual(self.request(path + TRACKING, method)[0], 404)
        self.assertEqual(self.request('/google1c6ec0bf65f9cfd3.html')[0], 200)
        with patch('server.response_store_for_request', return_value=('cloudflare', object())), \
                patch('server.FileStore') as store:
            self.assertEqual(self.request('/api' + JOURNAL + '/responses' + TRACKING, 'POST')[0], 404)
            store.assert_not_called()

    def test_canonical_and_sitemap_contain_only_clean_urls(self):
        for path in PAGES:
            body = self.request(path + TRACKING)[2]
            self.assertIn(f'rel="canonical" href="{server.PUBLIC_ORIGIN}{path}"'.encode(), body)
            self.assertNotIn(b'utm_source', body)
        xml = ET.fromstring(self.request('/sitemap.xml')[2])
        locs = {node.text for node in xml.iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc')}
        self.assertTrue({server.PUBLIC_ORIGIN + path for path in PAGES} <= locs)
        self.assertTrue(all('?' not in loc and '#' not in loc for loc in locs))

    def test_single_observation_per_request_and_no_query_values_in_logs(self):
        for target, method, status in (
            (ARCHIVE, 'GET', 200),
            (ARCHIVE + '?utm_source=private-marker', 'GET', 200),
            (ARCHIVE + '?gclid=private-marker', 'HEAD', 200),
            (ARCHIVE + '?unknown=private-marker', 'GET', 404),
        ):
            self.assertEqual(self.request(target, method)[0], status)
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual([row['path'] for row in rows], [ARCHIVE] * 4)
        self.assertTrue(all(row['agent_like'] for row in rows))
        stats = json.loads(self.request('/stats')[2])
        self.assertEqual(stats['total_requests'], 4)
        self.assertNotIn('private-marker', self.log.read_text())
        self.assertNotIn('private-marker', self.access_log.getvalue())
        self.assertNotIn('utm_source', self.access_log.getvalue())
        self.assertEqual(self.access_log.getvalue().count(f'GET {ARCHIVE} HTTP/1.0'), 3)
        self.assertEqual(self.access_log.getvalue().count(f'HEAD {ARCHIVE} HTTP/1.0'), 1)


if __name__ == '__main__':
    unittest.main()
