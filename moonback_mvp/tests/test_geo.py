import tempfile
import unittest
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import server
from test_v03 import FakeSocket


PUBLIC_ORIGIN = 'https://moonback-jbz0.onrender.com'
SITEMAP_PATHS = ('/', '/archive', '/journal')


class Metadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.descriptions = []
        self.titles = []
        self.headings = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta' and attrs.get('name') == 'description':
            self.descriptions.append(attrs.get('content', ''))
        if tag in ('title', 'h1'):
            self.current = tag

    def handle_endtag(self, tag):
        if tag == self.current:
            self.current = None

    def handle_data(self, data):
        if self.current == 'title':
            self.titles.append(data)
        elif self.current == 'h1':
            self.headings.append(data)


class SearchFoundationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.start_patch('server.LOG', Path(tmp.name) / 'requests.jsonl')
        store = Mock()
        store.get_status.return_value = {'pending': False, 'cursor': None}
        self.start_patch('server.configured_cloudflare_response_store', return_value=store)
        self.start_patch('server.RESPONSE_STATUS_CACHE', server.ResponseStatusCache())
        self.start_patch('socket.create_connection', side_effect=AssertionError('Network prohibited'))

    def start_patch(self, target, *args, **kwargs):
        active = patch(target, *args, **kwargs)
        self.addCleanup(active.stop)
        return active.start()

    def request(self, path, method='GET', user_agent='Mozilla/5.0'):
        sock = FakeSocket((method + ' ' + path + ' HTTP/1.0\r\nUser-Agent: '
                           + user_agent + '\r\n\r\n').encode())
        with patch.object(server.Handler, 'log_message'):
            server.Handler(sock, ('127.0.0.1', 12345), None)
        header, body = sock.output.getvalue().split(b'\r\n\r\n', 1)
        lines = header.decode('latin-1').split('\r\n')
        headers = {key.lower(): value for key, value in
                   (line.split(': ', 1) for line in lines[1:])}
        return int(lines[0].split()[1]), headers, body

    def test_sitemap_xml_has_only_selected_absolute_public_urls(self):
        status, headers, body = self.request('/sitemap.xml')
        self.assertEqual(status, 200)
        self.assertIn('xml', headers['content-type'])
        root = ET.fromstring(body)
        namespace = '{http://www.sitemaps.org/schemas/sitemap/0.9}'
        self.assertEqual(root.tag, namespace + 'urlset')
        urls = [item.find(namespace + 'loc').text for item in root]
        self.assertEqual(urls, [PUBLIC_ORIGIN + path for path in SITEMAP_PATHS])
        self.assertEqual(len(urls), len(set(urls)))
        self.assertNotIn(b'example.com', body)
        for url in urls:
            parsed = urlsplit(url)
            self.assertEqual(parsed.scheme, 'https')
            self.assertFalse(parsed.query or parsed.fragment)
            status, headers, _ = self.request(parsed.path)
            self.assertEqual(status, 200)
            self.assertIn('text/html', headers['content-type'])

    def test_robots_preserves_allow_policy_and_announces_absolute_sitemap(self):
        status, headers, body = self.request('/robots.txt')
        self.assertEqual(status, 200)
        self.assertIn('text/plain', headers['content-type'])
        rules = RobotFileParser()
        rules.parse(body.decode().splitlines())
        self.assertEqual(rules.site_maps(), [PUBLIC_ORIGIN + '/sitemap.xml'])
        for agent in ('Googlebot', 'GPTBot', '*'):
            for path in SITEMAP_PATHS + ('/observe', '/signal/001'):
                self.assertTrue(rules.can_fetch(agent, PUBLIC_ORIGIN + path))

    def test_all_public_routes_support_get_and_head(self):
        paths = tuple(server.Handler.PUBLIC_FILES) + (
            '/archive', '/journal', '/stats', '/api/responses/status')
        for path in paths:
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                head_status, head_headers, head_body = self.request(path, 'HEAD')
                self.assertEqual((status, head_status), (200, 200))
                self.assertEqual(head_body, b'')
                self.assertEqual(headers['content-type'], head_headers['content-type'])
                # /stats includes this test's observation requests and can change.
                if path != '/stats':
                    self.assertEqual(int(head_headers['content-length']), len(body))

    def test_omitted_dashboard_aliases_and_machine_files_remain_accessible(self):
        for path in ('/observe', '/observe.html', '/index.html', '/agent.json',
                     '/llms.txt', '/signal/001', '/stats', '/api/responses/status'):
            with self.subTest(path=path):
                self.assertNotIn(path, SITEMAP_PATHS)
                self.assertEqual(self.request(path)[0], 200)

    def test_html_titles_descriptions_and_visible_headings(self):
        descriptions = []
        for path, title, heading in (
            ('/', 'Moonback', None),
            ('/observe', 'Moonback / Observe', 'Moonback / Observe'),
            ('/archive', 'MoonBack / AI/Web Archive', 'AI/Web Archive'),
            ('/journal', 'MoonBack / Luna Journal', 'Luna Journal'),
        ):
            with self.subTest(path=path):
                page = Metadata()
                page.feed(self.request(path)[2].decode())
                self.assertEqual(page.titles, [title])
                self.assertEqual(page.headings, [] if heading is None else [heading])
                self.assertEqual(len(page.descriptions), 1)
                self.assertTrue(page.descriptions[0].strip())
                descriptions.extend(page.descriptions)
        self.assertEqual(len(set(descriptions)), 4)
        homepage = self.request('/')[2]
        self.assertEqual(homepage, (server.ROOT / 'index.html').read_bytes())
        self.assertIn(b'<a href="/observe">.</a>', homepage)

    def test_crawlers_and_humans_receive_same_content(self):
        for path in SITEMAP_PATHS + ('/observe', '/robots.txt', '/sitemap.xml'):
            with self.subTest(path=path):
                human = self.request(path)[2]
                for agent in ('Googlebot', 'GPTBot', 'ChatGPT-User'):
                    self.assertEqual(self.request(path, user_agent=agent)[2], human)

    def test_private_files_stay_inaccessible_for_get_and_head(self):
        for path in ('/drafts/', '/drafts/archive/private.json',
                     '/published/journal/private.json', '/data/requests.jsonl',
                     '/.env', '/.git/config', '/README.md', '/tests/test_geo.py',
                     '/server.py', '/status/response-status.json',
                     '/cloudflare/response-intake/wrangler.jsonc'):
            for method in ('GET', 'HEAD'):
                with self.subTest(path=path, method=method):
                    status, _, body = self.request(path, method)
                    self.assertEqual(status, 404)
                    if method == 'HEAD':
                        self.assertEqual(body, b'')


if __name__ == '__main__':
    unittest.main()
