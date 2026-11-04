import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from html import escape
from pathlib import Path
from unittest.mock import patch

import content
import server
from response import build_draft_response
from response_store import FileResponseStore
from test_geo import Metadata
from test_v03 import FakeSocket, archive


class IndividualPageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = content.FileStore(self.root)
        self.archive = archive()
        self.archive.update(id='archive-001', name='A "quoted" <tool> & boundary',
                            description='A "hint" <describes> & does not enforce a boundary.')
        self.journal = dict(id='journal-001', date='2026-09-28',
                            text='A "boundary" <script> is an observation.\nNot a guarantee.',
                            source_urls=['https://example.org/journal-source'])
        self.store.save('published', 'archive', self.archive)
        self.store.save('published', 'journal', self.journal)
        self.responses = FileResponseStore(self.root)
        self.start_patch('server.LOG', self.root / 'requests.jsonl')
        self.start_patch('server.render_page', side_effect=lambda *args, **kwargs:
                         content.render_page(*args, store=self.store, **kwargs))
        self.start_patch('server.render_sitemap', side_effect=lambda:
                         content.render_sitemap(self.store))
        self.network = self.start_patch('socket.create_connection',
                                       side_effect=AssertionError('Network prohibited'))

    def start_patch(self, target, *args, **kwargs):
        active = patch(target, *args, **kwargs)
        self.addCleanup(active.stop)
        return active.start()

    def request(self, path, method='GET', host='untrusted.example'):
        raw = method + ' ' + path + ' HTTP/1.0\r\nHost: ' + host + '\r\n\r\n'
        sock = FakeSocket(raw.encode())
        with patch.object(server.Handler, 'log_message'):
            server.Handler(sock, ('127.0.0.1', 12345), None)
        header, body = sock.output.getvalue().split(b'\r\n\r\n', 1)
        lines = header.decode().split('\r\n')
        headers = {key.lower(): value for key, value in
                   (line.split(': ', 1) for line in lines[1:])}
        return int(lines[0].split()[1]), headers, body

    def sitemap_urls(self):
        status, headers, body = self.request('/sitemap.xml')
        self.assertEqual(status, 200)
        self.assertIn('application/xml', headers['content-type'])
        return [node.text for node in ET.fromstring(body).iter(
            '{http://www.sitemaps.org/schemas/sitemap/0.9}loc')]

    def test_public_details_get_head_and_observation_logging(self):
        for path in ('/archive/archive-001', '/journal/journal-001'):
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                head_status, head_headers, head_body = self.request(path, 'HEAD')
                self.assertEqual((status, head_status), (200, 200))
                self.assertIn('text/html', headers['content-type'])
                self.assertEqual(int(head_headers['content-length']), len(body))
                self.assertEqual(head_body, b'')
        logged = [json.loads(line)['path'] for line in
                  (self.root / 'requests.jsonl').read_text().splitlines()]
        self.assertEqual(logged, ['/archive/archive-001'] * 2 + ['/journal/journal-001'] * 2)
        self.network.assert_not_called()

    def test_drafts_and_unknown_ids_are_404_for_get_and_head(self):
        for kind, row in (('archive', self.archive), ('journal', self.journal)):
            row = dict(row, id='unpublished')
            self.store.save('drafts', kind, row)
            for ident in ('unpublished', 'unknown'):
                for method in ('GET', 'HEAD'):
                    with self.subTest(kind=kind, ident=ident, method=method):
                        status, _, body = self.request('/' + kind + '/' + ident, method)
                        self.assertEqual(status, 404)
                        if method == 'HEAD':
                            self.assertEqual(body, b'')

    def test_invalid_ids_traversal_and_noncanonical_spellings_are_404(self):
        suffixes = ('', '/', '../archive-001', '../../server.py',
                    '%2e%2e/server.py', '%61rchive-001', 'archive-001%2f',
                    'archive-001/extra', 'archive-001/', 'archive-001.json',
                    'Archive-001', '_private', 'a' * 81, 'archive-001;extra',
                    'archive-001?x=1', 'archive-001?', 'archive-001#x',
                    'archive-001%00', 'archive-001\\extra')
        for kind in ('archive', 'journal'):
            for suffix in suffixes:
                for method in ('GET', 'HEAD'):
                    with self.subTest(kind=kind, suffix=suffix, method=method):
                        self.assertEqual(self.request('/' + kind + '/' + suffix, method)[0], 404)
        self.assertEqual(self.request('//archive/archive-001')[0], 404)
        self.assertEqual(self.request('/archive//archive-001')[0], 404)

    def test_identifier_boundary_accepts_existing_valid_forms(self):
        for ident in ('a', 'a_b-09', 'a' * 80):
            self.store.save('published', 'journal', dict(self.journal, id=ident))
            self.assertEqual(self.request('/journal/' + ident)[0], 200)

    def test_archive_metadata_heading_date_source_and_escape(self):
        html = self.request('/archive/archive-001')[2].decode()
        meta = Metadata()
        meta.feed(html)
        self.assertIn(self.archive['name'], meta.titles[0])
        self.assertIn('archive-001', meta.titles[0])
        self.assertEqual(meta.headings, [self.archive['name']])
        self.assertEqual(meta.descriptions, [self.archive['description']])
        self.assertIn('datetime="2026-09-13"', html)
        self.assertIn('rel="noreferrer" href="https://example.org/">Source</a>', html)
        self.assertNotIn('<tool>', html)
        self.assertNotIn('<describes>', html)
        self.assertIn('content="' + escape(self.archive['description'], quote=True) + '"', html)
        self.assertIn(escape(self.archive['name']), html)

    def test_journal_metadata_uses_only_own_record_and_preserves_body(self):
        self.store.save('published', 'journal', dict(self.journal, id='other',
                                                   text='OTHER_RECORD'))
        html = self.request('/journal/journal-001')[2].decode()
        meta = Metadata()
        meta.feed(html)
        self.assertIn('journal-001', meta.titles[0])
        self.assertIn(self.journal['date'], meta.titles[0])
        self.assertEqual(meta.headings, ['Luna Journal / 2026-09-28'])
        self.assertEqual(meta.descriptions, [' '.join(self.journal['text'].split())])
        self.assertIn(escape(self.journal['text']), html)
        self.assertNotIn('<script>', html)
        self.assertNotIn('OTHER_RECORD', html)
        self.assertIn('datetime="2026-09-28"', html)
        self.assertIn('href="https://example.org/journal-source">Source</a>', html)

    def test_description_is_bounded_and_no_dates_are_invented(self):
        self.store.save('published', 'journal', dict(self.journal, id='long', text='x' * 12000))
        html = self.request('/journal/long')[2].decode()
        meta = Metadata()
        meta.feed(html)
        self.assertEqual(meta.descriptions, ['x' * 200])
        self.assertNotIn('dateModified', html)
        self.assertNotIn('datePublished', html)

    def test_canonical_is_self_referencing_and_ignores_host(self):
        for path in ('/archive', '/journal', '/archive/archive-001', '/journal/journal-001'):
            with self.subTest(path=path):
                html = self.request(path, host='attacker.example')[2].decode()
                self.assertEqual(html.count('rel="canonical"'), 1)
                self.assertIn('rel="canonical" href="' + content.PUBLIC_ORIGIN + path + '"', html)
                self.assertNotIn('attacker.example', html)

    def test_listings_keep_records_navigation_and_link_to_details(self):
        for kind, row in (('archive', self.archive), ('journal', self.journal)):
            html = self.request('/' + kind)[2].decode()
            self.assertIn('href="/' + kind + '/' + row['id'] + '"', html)
            self.assertIn('href="/archive">Archive</a>', html)
            self.assertIn('href="/journal">Journal</a>', html)
            self.assertIn(escape(row.get('text', row.get('description'))), html)
            self.assertIn('>Source</a>', html)

    def test_only_matching_published_responses_render_on_detail(self):
        for ident, journal_id, text in (
            ('visible', 'journal-001', '<unsafe> & "quoted"'),
            ('draft-only', 'journal-001', 'DRAFT_RESPONSE'),
            ('other-response', 'other', 'OTHER_RESPONSE')):
            if journal_id == 'other':
                self.store.save('published', 'journal', dict(self.journal, id='other'))
            row = build_draft_response(dict(agent_name='<Agent>', response=text,
                                           source_url='https://example.org/agent'),
                                       journal_id, response_id=ident)
            self.responses.save_draft(row)
            if ident != 'draft-only':
                self.responses.publish(ident, reviewed=True, publication_date='2026-09-29')
        html = self.request('/journal/journal-001')[2].decode()
        self.assertIn('data-journal-id="journal-001"', html)
        self.assertIn('Claimed agent: &lt;Agent&gt;', html)
        self.assertIn('&lt;unsafe&gt; &amp; &quot;quoted&quot;', html)
        self.assertIn('rel="nofollow ugc noreferrer"', html)
        self.assertNotIn('DRAFT_RESPONSE', html)
        self.assertNotIn('OTHER_RESPONSE', html)

    def test_sitemap_matches_only_public_records_and_each_url_resolves(self):
        self.store.save('drafts', 'journal', dict(self.journal, id='draft-only'))
        urls = self.sitemap_urls()
        self.assertEqual(urls, [content.PUBLIC_ORIGIN + path for path in
                               ('/', '/archive', '/journal', '/archive/archive-001', '/journal/journal-001',
                                '/tools/', '/tools/csv-test-cases/v0.1.0/')])
        self.assertEqual(len(urls), len(set(urls)))
        for url in urls:
            self.assertEqual(self.request(url.removeprefix(content.PUBLIC_ORIGIN))[0], 200)
        body = self.request('/sitemap.xml')[2]
        self.assertNotIn(b'lastmod', body)
        self.assertNotIn(b'draft-only', body)

    def test_new_publication_updates_sitemap_without_editing_template(self):
        before = self.sitemap_urls()
        self.store.save('drafts', 'journal', dict(self.journal, id='new'))
        with self.assertRaises(ValueError):
            self.store.publish('journal', 'new')
        self.assertEqual(self.sitemap_urls(), before)
        self.store.publish('journal', 'new', reviewed=True)
        self.assertIn(content.PUBLIC_ORIGIN + '/journal/new', self.sitemap_urls())
        self.assertEqual(self.request('/journal/new')[0], 200)

    def test_empty_store_sitemap_and_listings(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = content.FileStore(directory)
            root = ET.fromstring(content.render_sitemap(empty))
            self.assertEqual(len(root), 5)
            for kind in content.KINDS:
                self.assertIn(b'No published records yet.', content.render_page(kind, empty))
                with self.assertRaises(content.PublishedItemNotFound):
                    content.render_page(kind, empty, ident='unknown')

    def test_corrupt_public_data_fails_closed_without_exposing_error(self):
        path = self.store.directory('published', 'archive') / 'broken.json'
        path.write_text('NOT_PUBLIC_DIAGNOSTIC')
        for route in ('/archive/archive-001', '/archive', '/sitemap.xml'):
            for method in ('GET', 'HEAD'):
                status, _, body = self.request(route, method)
                self.assertEqual(status, 503)
                self.assertNotIn(b'NOT_PUBLIC_DIAGNOSTIC', body)

    def test_mismatched_filename_or_symlink_never_becomes_public(self):
        path = self.store.directory('published', 'archive') / 'archive-001.json'
        original = path.read_bytes()
        path.write_text(json.dumps(dict(self.archive, id='mismatch')))
        self.assertEqual(self.request('/archive/mismatch')[0], 503)
        self.assertEqual(self.request('/sitemap.xml')[0], 503)
        path.write_bytes(original)
        draft = self.store.save('drafts', 'archive', dict(self.archive, id='hidden'))
        link = path.parent / 'hidden.json'
        link.symlink_to(draft)
        self.assertEqual(self.request('/archive/hidden')[0], 503)
        self.assertEqual(self.request('/sitemap.xml')[0], 503)

    def test_source_url_attribute_quotes_are_escaped(self):
        source = 'https://example.org/"&quoted'
        self.store.save('published', 'archive', dict(self.archive, id='quoted', source_url=source))
        self.store.save('published', 'journal', dict(self.journal, id='quoted', source_urls=[source]))
        for kind in content.KINDS:
            html = self.request('/' + kind + '/quoted')[2].decode()
            self.assertIn('href="' + escape(source, quote=True) + '">Source</a>', html)
            self.assertNotIn('href="' + source + '"', html)

    def test_invalid_base_sitemap_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            template = Path(directory) / 'sitemap.xml'
            for invalid in ('<broken', '<urlset/>'):
                template.write_text(invalid)
                with patch('content.ROOT', Path(directory)):
                    status, _, body = self.request('/sitemap.xml')
                self.assertEqual(status, 503)
                self.assertNotIn(invalid.encode(), body)


if __name__ == '__main__':
    unittest.main()
