import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import content
import manage
import server
from test_v03 import FakeSocket, archive


class OptionalJournalSourcesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = content.FileStore(self.root)
        self.row = dict(id='independent-observation', date='2026-11-04',
                        text='A <quiet> question & no conclusion.', source_urls=[])

    def test_empty_array_is_valid_but_field_and_url_validation_remain_required(self):
        self.assertEqual(content.validate('journal', self.row), self.row)
        missing = dict(self.row)
        del missing['source_urls']
        with self.assertRaises(ValueError):
            content.validate('journal', missing)
        for sources in (None, '', {}, (), 'https://example.org/', [None], [123],
                        [''], ['http://example.org/'], ['javascript:alert(1)'],
                        ['https://user:password@example.org/'],
                        ['https://example.org/?q=term'],
                        ['https://example.org/', 'invalid']):
            with self.subTest(sources=sources), self.assertRaises(ValueError):
                content.validate('journal', dict(self.row, source_urls=sources))

    def test_archive_source_stays_mandatory(self):
        for value in (None, '', [], 'http://example.org/'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                content.validate('archive', dict(archive(), source_url=value))
        missing = archive()
        del missing['source_url']
        with self.assertRaises(ValueError):
            content.validate('archive', missing)

    def test_both_journal_pages_omit_empty_source_paragraph_and_preserve_escape(self):
        self.store.save('published', 'journal', self.row)
        for ident in (None, self.row['id']):
            with self.subTest(ident=ident):
                html = content.render_page('journal', self.store, ident=ident).decode()
                self.assertNotIn('>Source</a>', html)
                self.assertNotIn('<p></p>', html)
                self.assertIn('A &lt;quiet&gt; question &amp; no conclusion.', html)
                self.assertIn('datetime="2026-11-04"', html)

    def test_existing_sources_keep_links_on_both_pages(self):
        sources = ['https://example.org/one', 'https://example.org/two']
        self.store.save('published', 'journal', dict(self.row, source_urls=sources))
        for ident in (None, self.row['id']):
            html = content.render_page('journal', self.store, ident=ident).decode()
            expected = ' · '.join('<a rel="noreferrer" href="' + url + '">Source</a>'
                                  for url in sources)
            self.assertIn('<p>' + expected + '</p>', html)

    def test_manual_import_review_and_reviewed_publish_preserve_exact_record(self):
        source = self.root / 'manual.json'
        source.write_text(json.dumps(self.row), encoding='utf-8')
        for args in (['import', 'journal', str(source)], ['review', 'journal']):
            with patch('manage.FileStore', return_value=self.store), \
                    patch('sys.argv', ['manage.py'] + args), \
                    patch('sys.stdout', new_callable=io.StringIO):
                manage.main()
        self.assertEqual(self.store.read('drafts', 'journal'), [self.row])
        self.assertEqual(self.store.read('published', 'journal'), [])
        self.assertNotIn(self.row['text'].encode(), content.render_page('journal', self.store))
        args = ['manage.py', 'publish', 'journal', self.row['id']]
        with patch('manage.FileStore', return_value=self.store), \
                patch('sys.argv', args), patch('sys.stderr', new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as stopped:
                manage.main()
            self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(self.store.read('published', 'journal'), [])
        with patch('manage.FileStore', return_value=self.store), \
                patch('sys.argv', args + ['--reviewed']), \
                patch('sys.stdout', new_callable=io.StringIO):
            manage.main()
        self.assertEqual(self.store.read('published', 'journal'), [self.row])
        self.assertEqual(self.store.read('drafts', 'journal'), [self.row])

    def test_get_head_tracking_navigation_canonical_and_sitemap(self):
        self.store.save('published', 'journal', self.row)
        with patch('server.LOG', self.root / 'requests.jsonl'), \
                patch('server.render_page', side_effect=lambda *a, **kw:
                      content.render_page(*a, store=self.store, **kw)), \
                patch.object(server.Handler, 'log_message'), \
                patch('socket.create_connection', side_effect=AssertionError('Network prohibited')):
            for path in ('/journal', '/journal/' + self.row['id']):
                for query in ('', '?utm_source=chatgpt.com&utm_medium=referral'):
                    for method in ('GET', 'HEAD'):
                        sock = FakeSocket((method + ' ' + path + query +
                                           ' HTTP/1.0\r\n\r\n').encode())
                        server.Handler(sock, ('127.0.0.1', 12345), None)
                        headers, body = sock.output.getvalue().split(b'\r\n\r\n', 1)
                        self.assertIn(b'200 OK', headers)
                        if method == 'HEAD':
                            self.assertEqual(body, b'')
                        else:
                            html = body.decode()
                            self.assertIn('<link rel="canonical" href="' + content.PUBLIC_ORIGIN + path + '">', html)
                            for target in ('/', '/archive', '/journal', '/tools/'):
                                self.assertIn('href="' + target + '"', html)
                            self.assertNotIn('>Source</a>', html)
        self.assertIn((content.PUBLIC_ORIGIN + '/journal/' + self.row['id']).encode(),
                      content.render_sitemap(self.store))
        self.assertEqual(len((self.root / 'requests.jsonl').read_text().splitlines()), 8)
