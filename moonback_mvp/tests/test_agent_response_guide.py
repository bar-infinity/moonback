import json
import tempfile
import unittest
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock, patch

import content
import server
from response import validate_external_response, validate_draft_response
from test_tools_web import PageMetadata
from test_v03 import FakeSocket, archive


class GuideMarkup(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.endpoints = []
        self.forms = []
        self.ids = []
        self.feed(body.decode())

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'data-response-endpoint' in attrs:
            self.endpoints.append(attrs['data-response-endpoint'])
        if 'id' in attrs:
            self.ids.append(attrs['id'])
        if tag == 'form':
            self.forms.append(attrs)


class AgentResponseGuideTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = content.FileStore(self.root)
        self.row = dict(id='journal-guide-001', date='2026-11-04',
                        text='A <quiet> observation & a question.', source_urls=[])
        self.store.save('published', 'journal', self.row)
        self.store.save('published', 'archive', archive())
        self.spec = json.loads((content.ROOT / 'agent.json').read_text())
        for active in (
            patch('server.LOG', self.root / 'requests.jsonl'),
            patch('server.FileStore', return_value=self.store),
            patch('server.render_page', side_effect=lambda *args, **kwargs:
                  content.render_page(*args, store=self.store, **kwargs)),
            patch.object(server.Handler, 'log_message'),
            patch('socket.create_connection', side_effect=AssertionError('Network prohibited')),
        ):
            active.start()
            self.addCleanup(active.stop)

    def request(self, path, method='GET', body=b''):
        headers = 'Host: untrusted.invalid\r\n'
        if method == 'POST':
            headers += 'Content-Type: application/json\r\nContent-Length: ' + str(len(body)) + '\r\n'
        sock = FakeSocket((method + ' ' + path + ' HTTP/1.0\r\n' + headers + '\r\n').encode() + body)
        server.Handler(sock, ('127.0.0.1', 12345), None)
        header, body = sock.output.getvalue().split(b'\r\n\r\n', 1)
        lines = header.decode().split('\r\n')
        return int(lines[0].split()[1]), dict(line.lower().split(': ', 1) for line in lines[1:]), body

    def test_json_example_passes_existing_validation_and_body_limit(self):
        intake = self.spec['journal_response_intake']
        example = intake['example']
        self.assertEqual(validate_external_response(example), example)
        body = json.dumps(example, ensure_ascii=False).encode('utf-8')
        self.assertLessEqual(len(body), intake['max_request_bytes'])
        self.assertEqual(intake['max_request_bytes'], server.MAX_RESPONSE_BODY)
        self.assertEqual(intake['public_origin'], content.PUBLIC_ORIGIN)
        self.assertEqual(set(example), {'agent_name', 'response', 'source_url'})

    def test_example_is_accepted_by_fake_existing_intake_without_network(self):
        backend = Mock()
        limiter = Mock()
        limiter.allow.return_value = True
        example = self.spec['journal_response_intake']['example']
        path = '/api/journal/' + self.row['id'] + '/responses'
        with patch('server.response_store_for_request', return_value=('cloudflare', backend)), \
                patch('server.RESPONSE_RATE_LIMITER', limiter):
            code, _, body = self.request(path, 'POST', json.dumps(example).encode())
        self.assertEqual(code, 202)
        self.assertEqual(json.loads(body)['status'], 'accepted_for_review')
        backend.save_draft.assert_called_once()
        draft = backend.save_draft.call_args.args[0]
        validate_draft_response(draft)
        self.assertEqual(draft['journal_id'], self.row['id'])
        for field in example:
            self.assertEqual(draft[field], example[field])
        self.assertFalse((self.root / 'published' / 'responses').exists())

    def test_detail_get_head_and_tracking_keep_exact_fixed_origin_endpoint(self):
        path = '/journal/' + self.row['id']
        endpoint = content.PUBLIC_ORIGIN + '/api/journal/' + self.row['id'] + '/responses'
        for suffix in ('', '?utm_source=chatgpt.com&utm_medium=referral'):
            code, headers, body = self.request(path + suffix)
            head_code, head_headers, head_body = self.request(path + suffix, 'HEAD')
            self.assertEqual((code, head_code), (200, 200))
            self.assertEqual(head_body, b'')
            self.assertEqual(headers['content-type'], head_headers['content-type'])
            self.assertEqual(int(head_headers['content-length']), len(body))
            markup = GuideMarkup(body)
            self.assertEqual(markup.endpoints, [endpoint])
            self.assertEqual(markup.forms, [])
            self.assertEqual(len(markup.ids), len(set(markup.ids)))
            self.assertNotIn(b'untrusted.invalid', body)
            self.assertNotIn(b'utm_source', body)
            self.assertEqual(PageMetadata(body).canonical, [content.PUBLIC_ORIGIN + path])

    def test_all_real_published_journals_use_their_own_id(self):
        for row in content.FileStore().read('published', 'journal'):
            body = content.render_page('journal', ident=row['id'])
            self.assertEqual(GuideMarkup(body).endpoints,
                             [content.PUBLIC_ORIGIN + '/api/journal/' + row['id'] + '/responses'])

    def test_guide_is_detail_only_and_preserves_journal_text_and_source_escape(self):
        self.assertNotIn(b'reply-guide', self.request('/journal')[2])
        self.assertNotIn(b'reply-guide', self.request('/archive/test')[2])
        source = 'https://example.org/"quoted"&path'
        row = dict(self.row, id='sourced', source_urls=[source])
        self.store.save('published', 'journal', row)
        body = self.request('/journal/sourced')[2].decode()
        self.assertIn('<p class="entry">' + escape(row['text']) + '</p>', body)
        self.assertIn('href="' + escape(source, quote=True) + '">Source</a>', body)
        self.assertNotIn('<quiet>', body)

    def test_unpublished_unknown_and_unsafe_ids_remain_unavailable(self):
        self.store.save('drafts', 'journal', dict(self.row, id='draft-only'))
        for ident in ('draft-only', 'unknown', '../server.py', '%2e%2e/server.py',
                      'bad%22id', self.row['id'] + '/extra'):
            for method in ('GET', 'HEAD'):
                self.assertEqual(self.request('/journal/' + ident, method)[0], 404)
        for ident in ('../bad', 'bad"id', 'bad?query', None):
            with self.assertRaises(ValueError):
                content.journal_reply_guide(ident)

    def test_policy_describes_optional_untrusted_review_only_configuration(self):
        body = self.request('/journal/' + self.row['id'])[2].decode()
        for text in ('Participation is optional', 'self-asserted', '1–80', '1–2,000',
                     'application/json', '8 KiB', '202 Accepted', 'not published',
                     'human review', 'live intake has not been verified',
                     'No API key or authentication secret', 'Read-only agents',
                     'personal information', 'User-Agent does not verify identity',
                     'Guestbook is a future idea'):
            self.assertIn(text, body)
        self.assertEqual(self.spec['guestbook']['availability'], 'not_implemented')
        self.assertTrue(self.spec['journal_response_intake']['human_review_required'])
        self.assertFalse(self.spec['journal_response_intake']['automatic_publication'])

    def test_llms_guidance_and_agent_example_are_served_get_head(self):
        for path in ('/llms.txt', '/agent.json'):
            code, _, body = self.request(path)
            head_code, headers, head_body = self.request(path, 'HEAD')
            self.assertEqual((code, head_code), (200, 200))
            self.assertEqual(head_body, b'')
            self.assertEqual(int(headers['content-length']), len(body))
            if path == '/agent.json':
                validate_external_response(json.loads(body)['journal_response_intake']['example'])
            else:
                for text in (b'Optional Journal replies', b'1-80', b'1-2000',
                             b'8 KiB', b'202 Accepted', b'Read-only agents',
                             b'No submitter API key', b'not an available feature'):
                    self.assertIn(text, body)
