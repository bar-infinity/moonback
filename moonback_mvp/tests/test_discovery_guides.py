import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import content
import server
import tools_web
from test_tools_web import PageMetadata
from test_v03 import FakeSocket


class DiscoveryGuideTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for target, kwargs in (
            ('server.LOG', {'new': self.root / 'requests.jsonl'}),
            ('socket.create_connection', {'side_effect': AssertionError('Network prohibited')}),
            ('os.environ', {'new': {}}),
        ):
            active = patch(target, **kwargs)
            active.start()
            self.addCleanup(active.stop)
        self.spec = json.loads((server.ROOT / 'agent.json').read_text())

    def request(self, path, method='GET'):
        sock = FakeSocket((method + ' ' + path + ' HTTP/1.0\r\n\r\n').encode())
        with patch.object(server.Handler, 'log_message'):
            server.Handler(sock, ('127.0.0.1', 12345), None)
        headers, body = sock.output.getvalue().split(b'\r\n\r\n', 1)
        lines = headers.decode().split('\r\n')
        return int(lines[0].split()[1]), dict(
            line.lower().split(': ', 1) for line in lines[1:]), body

    def test_guided_literal_paths_get_head_mime_and_canonical(self):
        llms = (server.ROOT / 'llms.txt').read_text()
        paths = set(re.findall(r'^- (/[^\s]+)', llms, re.MULTILINE))
        paths.update(self.spec[key] for key in
                     ('human_ui', 'observer_ui', 'entrypoint', 'archive_ui', 'journal_ui'))
        dataset = self.spec['tools']['csv_test_cases']
        paths.update((self.spec['tools']['index'], dataset['page'], dataset['manifest'], '/robots.txt'))
        for kind in content.KINDS:
            paths.update('/' + kind + '/' + row['id']
                         for row in content.FileStore().read('published', kind))
        paths.update(tools_web.SAMPLE_ROUTES)
        for path in sorted(paths):
            with self.subTest(path=path):
                code, headers, body = self.request(path)
                head_code, head_headers, head_body = self.request(path, 'HEAD')
                self.assertEqual((code, head_code), (200, 200))
                self.assertEqual(headers['content-type'], head_headers['content-type'])
                self.assertEqual(int(head_headers['content-length']), len(body))
                self.assertEqual(head_body, b'')
                if path in tools_web.TOOL_PAGES or path.startswith(('/archive', '/journal')):
                    self.assertEqual(PageMetadata(body).canonical, [content.PUBLIC_ORIGIN + path])
                if path.endswith('.json') or path.startswith('/signal/'):
                    self.assertIn('application/json', headers['content-type'])
                if path.endswith('.csv'):
                    self.assertEqual(headers['content-type'], 'text/csv; charset=utf-8')

    def test_machine_tools_inventory_matches_exact_original_downloads(self):
        dataset = self.spec['tools']['csv_test_cases']
        code, headers, raw = self.request(dataset['manifest'])
        self.assertEqual(code, 200)
        self.assertEqual(raw, (tools_web.DATASET / 'cases.json').read_bytes())
        manifest = json.loads(raw)
        paths = set()
        for case in manifest['cases']:
            path = dataset['sample_path_template'].format(file=case['file'])
            paths.add(path)
            code, _, body = self.request(path)
            self.assertEqual(code, 200)
            self.assertEqual(body, (tools_web.DATASET / case['file']).read_bytes())
            self.assertEqual(hashlib.sha256(body).hexdigest(), case['sha256'])
        self.assertEqual(paths, set(tools_web.SAMPLE_ROUTES))

    def test_intake_and_status_document_configuration_and_review_boundaries(self):
        intake = self.spec['journal_response_intake']
        self.assertIn('Configuration-dependent', intake['production_intake'])
        self.assertIn('Cloudflare', intake['production_intake'])
        self.assertIn('loopback', intake['phase1_network_scope'])
        self.assertEqual(intake['max_request_bytes'], server.MAX_RESPONSE_BODY)
        self.assertTrue(intake['human_review_required'])
        self.assertFalse(intake['automatic_publication'])
        self.assertEqual(intake['success']['http_status'], 202)
        self.assertEqual(server.response_store_for_request('127.0.0.1'), (None, None))
        path = self.spec['journal_response_status']['endpoint']
        self.assertEqual(self.request(path)[0], 404)
        store = Mock()
        store.get_status.return_value = {'pending': False, 'cursor': None}
        with patch('server.configured_cloudflare_response_store', return_value=store), \
                patch('server.RESPONSE_STATUS_CACHE', server.ResponseStatusCache()):
            for method in self.spec['journal_response_status']['methods']:
                code, headers, body = self.request(path, method)
                self.assertEqual(code, 200)
                self.assertEqual(headers['content-type'], 'application/json; charset=utf-8')
                if method == 'GET':
                    self.assertEqual(set(json.loads(body)), set(self.spec['journal_response_status']['response_fields']))
                else:
                    self.assertEqual(body, b'')
        store.get_status.assert_called_once_with()

    def test_guide_semantics_and_observation_uncertainty(self):
        self.assertIn('required source', self.spec['archive_description'])
        self.assertIn('independent', self.spec['archive_description'])
        self.assertIn("Luna's diary", self.spec['journal_description'])
        self.assertIn('when needed', self.spec['journal_description'])
        self.assertIn('heuristic', self.spec['observation_limits'])
        self.assertEqual(self.spec['observer_ui'], '/observe')
        self.assertNotIn('/observe', (server.ROOT / 'llms.txt').read_text())
        self.assertIn("Luna's diary", PageMetadata(self.request('/journal')[2]).descriptions[0])

    def test_templates_and_private_routes_remain_unavailable(self):
        for path in (self.spec['archive_item_template'], self.spec['journal_item_template'],
                     '/drafts/journal/private.json', '/published/journal/private.json',
                     '/response_admin.py', '/.env', '/data/requests.jsonl',
                     '/v1/admin/responses', '/v1/responses/status',
                     '/tools/csv-test-cases/v0.1.0/samples/../cases.json'):
            for method in ('GET', 'HEAD'):
                with self.subTest(path=path, method=method):
                    self.assertEqual(self.request(path, method)[0], 404)
