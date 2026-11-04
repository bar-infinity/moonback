from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import server
import tools_web
from test_v03 import FakeSocket


LINKS = [
    ('/', 'MoonBack'),
    ('/archive', 'Archive'),
    ('/journal', 'Journal'),
    ('/tools/', 'LUNA Tools'),
]


class SiteNavigation(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.links = []
        self.landmarks = 0
        self.in_nav = False
        self.anchor = None
        self.feed(body.decode('utf-8'))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'nav' and attrs.get('aria-label') == 'Site':
            self.landmarks += 1
            self.in_nav = True
        if self.in_nav and tag == 'a':
            self.anchor = (attrs['href'], [], attrs.get('aria-current'))

    def handle_data(self, data):
        if self.anchor is not None:
            self.anchor[1].append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.anchor is not None:
            path, label, current = self.anchor
            self.links.append((path, ''.join(label), current))
            self.anchor = None
        if tag == 'nav':
            self.in_nav = False


class PublicNavigationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        log = patch('server.LOG', Path(temp.name) / 'requests.jsonl')
        log.start()
        self.addCleanup(log.stop)

    def request(self, path, method='GET'):
        sock = FakeSocket(f'{method} {path} HTTP/1.0\r\n\r\n'.encode('ascii'))
        with patch.object(server.Handler, 'log_message'):
            server.Handler(sock, ('127.0.0.1', 12345), None)
        header, body = sock.output.getvalue().split(b'\r\n\r\n', 1)
        return int(header.split(b' ', 2)[1]), body

    def test_every_public_content_page_links_to_all_four_destinations(self):
        pages = [(path, path, 'page') for path, _ in LINKS if path != '/']
        pages.append((tools_web.DATASET_PATH, '/tools/', 'location'))
        for kind in ('archive', 'journal'):
            for item in server.FileStore().read('published', kind):
                pages.append((f'/{kind}/{item["id"]}', f'/{kind}', 'location'))
        for path, current_path, current_type in pages:
            with self.subTest(path=path):
                status, body = self.request(path)
                self.assertEqual(status, 200)
                nav = SiteNavigation(body)
                self.assertEqual(nav.landmarks, 1)
                self.assertEqual(nav.links, [
                    (href, label, current_type if href == current_path else None)
                    for href, label in LINKS
                ])
                for href, _, _ in nav.links:
                    self.assertEqual(self.request(href)[0], 200)
                tracked = path + '?utm_source=chatgpt.com'
                self.assertEqual(self.request(tracked), (200, body))
                self.assertEqual(self.request(tracked, 'HEAD'), (200, b''))

    def test_navigation_not_added_to_home_or_non_html_downloads(self):
        self.assertEqual(self.request('/')[1], (server.ROOT / 'index.html').read_bytes())
        for path in (tools_web.MANIFEST_PATH, *tools_web.SAMPLE_ROUTES):
            status, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertNotIn(b'<nav', body)
