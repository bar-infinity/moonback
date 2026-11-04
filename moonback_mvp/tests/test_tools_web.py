import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

import server
import tools_web
from test_v03 import FakeSocket


class PageMetadata(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.links = set()
        self.canonical = []
        self.descriptions = []
        self.titles = 0
        self.headings = 0
        self.scripts = 0
        self.feed(body.decode('utf-8'))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a':
            self.links.add(attrs['href'])
        elif tag == 'link' and attrs.get('rel') == 'canonical':
            self.canonical.append(attrs['href'])
        elif tag == 'meta' and attrs.get('name') == 'description':
            self.descriptions.append(attrs['content'])
        elif tag == 'title':
            self.titles += 1
        elif tag == 'h1':
            self.headings += 1
        elif tag == 'script':
            self.scripts += 1


class ToolsWebTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        log = patch('server.LOG', self.root / 'requests.jsonl')
        log.start()
        self.addCleanup(log.stop)

    def request(self, path, method='GET'):
        sock = FakeSocket((method + ' ' + path +
                           ' HTTP/1.0\r\nHost: untrusted.example\r\n\r\n').encode())
        with patch.object(server.Handler, 'log_message'):
            server.Handler(sock, ('127.0.0.1', 12345), None)
        header, body = sock.output.getvalue().split(b'\r\n\r\n', 1)
        lines = header.decode().split('\r\n')
        headers = {key.lower(): value for key, value in
                   (line.split(': ', 1) for line in lines[1:])}
        return int(lines[0].split()[1]), headers, body

    def test_all_thirteen_routes_get_head_and_headers(self):
        self.assertEqual(len(tools_web.ROUTES), 13)
        for path in tools_web.ROUTES:
            with self.subTest(path=path):
                code, headers, body = self.request(path)
                head_code, head_headers, head_body = self.request(path, 'HEAD')
                self.assertEqual((code, head_code), (200, 200))
                self.assertEqual(head_body, b'')
                for key in ('content-type', 'content-length', 'x-content-type-options',
                            'cache-control', 'content-disposition'):
                    self.assertEqual(head_headers.get(key), headers.get(key))
                self.assertEqual(int(headers['content-length']), len(body))
                self.assertEqual(headers['x-content-type-options'], 'nosniff')
                if path in tools_web.TOOL_PAGES:
                    self.assertEqual(headers['content-type'], 'text/html; charset=utf-8')
                    self.assertNotIn('content-disposition', headers)

    def test_manifest_exact_utf8_bytes_and_deployment_neutral_status(self):
        code, headers, body = self.request(tools_web.MANIFEST_PATH)
        self.assertEqual(code, 200)
        self.assertEqual(headers['content-type'], 'application/json; charset=utf-8')
        self.assertNotIn('content-disposition', headers)
        self.assertEqual(body, (tools_web.DATASET / 'cases.json').read_bytes())
        manifest = json.loads(body.decode('utf-8', errors='strict'))
        self.assertEqual(manifest['publication_status']['moonback_web_route'],
                         'available_when_deployed')
        self.assertEqual(manifest['license']['spdx_id'], 'CC0-1.0')

    def test_each_csv_matches_manifest_and_original_bytes(self):
        manifest = json.loads((tools_web.DATASET / 'cases.json').read_bytes())
        for case in manifest['cases']:
            with self.subTest(case=case['id']):
                code, headers, body = self.request(tools_web.DATASET_PATH + case['file'])
                self.assertEqual(code, 200)
                self.assertEqual(body, (tools_web.DATASET / case['file']).read_bytes())
                self.assertEqual(hashlib.sha256(body).hexdigest(), case['sha256'])
                self.assertEqual(headers['content-type'], 'text/csv; charset=utf-8')
                self.assertEqual(headers['content-disposition'],
                                 'attachment; filename="' + Path(case['file']).name + '"')

    def test_pages_have_canonical_metadata_and_working_discovery_links(self):
        for path in tools_web.TOOL_PAGES:
            meta = PageMetadata(self.request(path)[2])
            self.assertEqual(meta.canonical, [server.PUBLIC_ORIGIN + path])
            self.assertEqual((meta.titles, meta.headings, meta.scripts), (1, 1, 0))
            self.assertEqual(len(meta.descriptions), 1)
            self.assertTrue(meta.descriptions[0])
            self.assertIn(tools_web.DATASET_PATH if path == tools_web.TOOLS_PATH
                          else tools_web.MANIFEST_PATH, meta.links)
        self.assertTrue(set(tools_web.SAMPLE_ROUTES) <= meta.links)
        self.assertIn(tools_web.GITHUB, meta.links)
        self.assertIn(tools_web.LICENSE, meta.links)
        for link in meta.links:
            if link.startswith('/'):
                self.assertEqual(self.request(link)[0], 200)

    def test_unknown_paths_aliases_and_private_files_are_not_served(self):
        base = tools_web.DATASET_PATH
        sample = next(iter(tools_web.SAMPLE_ROUTES))
        paths = (
            '/tools', '//tools/', '/tools/?x', '/tools/#fragment',
            base.rstrip('/'), base + 'cases.json;params', base + 'cases.json?',
            base + 'samples/unknown.csv', sample + '/', sample.replace('.csv', '%2ecsv'),
            sample.replace('.csv', '%252ecsv'), base + 'samples/%2fserver.py',
            base + 'samples/../cases.json', base + 'samples/%2e%2e/cases.json',
            base + 'samples/%252e%252e/cases.json', base + 'samples/..\\cases.json',
            base + 'README.md', base + 'LICENSE.md', base + 'tests/test_cases.py',
            '/luna_tools/csv_test_cases/v0.1.0/cases.json',
            '/drafts/archive/private.json', '/data/requests.jsonl', '/.env', '/tools_web.py',
        )
        for path in paths:
            for method in ('GET', 'HEAD'):
                with self.subTest(path=path, method=method):
                    self.assertEqual(self.request(path, method)[0], 404)
        self.assertNotEqual(self.request(tools_web.MANIFEST_PATH, 'POST')[0], 200)

    def test_manifest_display_text_is_escaped(self):
        dataset = self.root / 'dataset'
        shutil.copytree(tools_web.DATASET, dataset)
        path = dataset / 'cases.json'
        manifest = json.loads(path.read_bytes())
        manifest['cases'][0]['name'] = '<script>alert(1)</script>'
        path.write_text(json.dumps(manifest), encoding='utf-8')
        with patch('tools_web.DATASET', dataset):
            code, _, body = self.request(tools_web.DATASET_PATH)
        self.assertEqual(code, 200)
        self.assertIn(b'&lt;script&gt;alert(1)&lt;/script&gt;', body)
        self.assertNotIn(b'<script>', body)

    def test_missing_dataset_does_not_advertise_unavailable_pages(self):
        with patch('tools_web.DATASET', self.root / 'missing'):
            self.assertEqual(self.request(tools_web.TOOLS_PATH)[0], 503)
            code, _, body = self.request('/sitemap.xml')
            self.assertEqual(code, 200)
            self.assertNotIn(b'/tools/', body)

    def test_unlisted_files_corruption_and_symlinks_fail_closed(self):
        dataset = self.root / 'dataset'
        shutil.copytree(tools_web.DATASET, dataset)
        name = tools_web.SAMPLE_FILES[0]
        sample = dataset / 'samples' / name
        original = sample.read_bytes()
        hidden = dataset / 'samples' / 'private.csv'
        hidden.write_bytes(b'PRIVATE_MARKER')
        with patch('tools_web.DATASET', dataset):
            self.assertEqual(self.request(tools_web.MANIFEST_PATH)[0], 200)
            self.assertEqual(self.request(tools_web.DATASET_PATH + 'samples/private.csv')[0], 404)
            sample.write_bytes(b'CORRUPT_MARKER')
            code, _, body = self.request(tools_web.DATASET_PATH + 'samples/' + name)
            self.assertEqual(code, 503)
            self.assertNotIn(b'CORRUPT_MARKER', body)
            sample.unlink()
            hidden.write_bytes(original)
            sample.symlink_to(hidden)
            self.assertEqual(self.request(tools_web.MANIFEST_PATH)[0], 503)

    def test_sitemap_preserves_published_urls_and_lists_only_two_tools_pages(self):
        store = server.FileStore()
        public_paths = ['/', '/archive', '/journal']
        for kind in ('archive', 'journal'):
            public_paths.extend(f'/{kind}/{row["id"]}'
                                for row in store.read('published', kind))
        public_urls = [server.PUBLIC_ORIGIN + path for path in public_paths]
        tools_urls = [server.PUBLIC_ORIGIN + path for path in
                      ('/tools/', '/tools/csv-test-cases/v0.1.0/')]
        code, _, body = self.request('/sitemap.xml')
        self.assertEqual(code, 200)
        urls = [node.text for node in ET.fromstring(body).iter(
            '{http://www.sitemaps.org/schemas/sitemap/0.9}loc')]
        self.assertCountEqual(urls, public_urls + tools_urls)
        with patch('tools_web.DATASET', self.root / 'missing'):
            code, _, previous = self.request('/sitemap.xml')
        self.assertEqual(code, 200)
        self.assertCountEqual([node.text for node in ET.fromstring(previous).iter(
            '{http://www.sitemaps.org/schemas/sitemap/0.9}loc')], public_urls)


if __name__ == '__main__':
    unittest.main()
