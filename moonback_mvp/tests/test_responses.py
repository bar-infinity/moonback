import ast
import io
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import content
import journal
import manage
import server
from content import FileStore
from response import (
    ResponseValidationError, build_draft_response, validate_external_response,
)
from response_store import FileResponseStore, LocalResponseRateLimiter


JOURNAL_ID = 'journal-test-001'


class FakeSocket:
    def __init__(self, request):
        self.input = io.BytesIO(request)
        self.output = io.BytesIO()

    def makefile(self, *args, **kwargs):
        return self.input

    def sendall(self, data):
        self.output.write(data)


def journal_row(ident=JOURNAL_ID):
    return {
        'id': ident,
        'date': '2026-09-28',
        'text': 'A published Journal observation.',
        'source_urls': ['https://example.org/journal'],
    }


def external(**changes):
    row = {
        'agent_name': 'Example Agent',
        'response': 'A short response.',
        'source_url': 'https://example.org/agent',
    }
    row.update(changes)
    return row


class ResponsePostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.journals = FileStore(self.root)
        self.journals.save('published', 'journal', journal_row())
        self.responses = FileResponseStore(self.root)
        self.limiter = LocalResponseRateLimiter(
            source_limit=100, journal_limit=100, window=60)
        network = patch('socket.create_connection',
                        side_effect=AssertionError('network forbidden'))
        network.start()
        self.addCleanup(network.stop)

    def request(self, *, body=None, path=None, content_type='application/json',
                content_length='auto', extra_headers=(), enabled=True,
                method='POST', client='127.0.0.1'):
        if body is None:
            body = json.dumps(external()).encode('utf-8')
        elif isinstance(body, str):
            body = body.encode('utf-8')
        path = path or f'/api/journal/{JOURNAL_ID}/responses'
        headers = [f'{method} {path} HTTP/1.0', 'Host: localhost',
                   'User-Agent: PRIVATE-UA-MARKER']
        if content_type is not None:
            headers.append('Content-Type: ' + content_type)
        if content_length == 'auto':
            headers.append('Content-Length: ' + str(len(body)))
        elif content_length is not None:
            headers.append('Content-Length: ' + content_length)
        headers.extend(extra_headers)
        request = ('\r\n'.join(headers) + '\r\n\r\n').encode() + body
        sock = FakeSocket(request)
        environment = {'MOONBACK_ENABLE_RESPONSE_POST': '1'} if enabled else {}
        with patch.dict(os.environ, environment, clear=True), \
             patch('server.FileStore', return_value=self.journals), \
             patch('server.FileResponseStore', return_value=self.responses), \
             patch('server.RESPONSE_RATE_LIMITER', self.limiter), \
             patch.object(server.Handler, 'log_message'):
            server.Handler(sock, (client, 12345), None)
        return sock.output.getvalue()

    def payload(self, response):
        return json.loads(response.split(b'\r\n\r\n', 1)[1])

    def test_valid_post_returns_202_and_saves_draft_only(self):
        response = self.request()
        self.assertIn(b'202 Accepted', response)
        payload = self.payload(response)
        self.assertEqual(payload['status'], 'accepted_for_review')
        drafts = self.responses.read_drafts()
        self.assertEqual(len(drafts), 1)
        self.assertEqual(payload['receipt_id'], drafts[0]['id'])
        self.assertEqual(drafts[0]['journal_id'], JOURNAL_ID)
        self.assertRegex(drafts[0]['id'],
                         r'^response-\d{8}-[a-f0-9]{32}$')
        self.assertTrue(drafts[0]['received_at'].endswith('Z'))
        self.assertEqual(self.responses.read_published(), [])
        self.assertNotIn(b'Example Agent', response)
        self.assertNotIn(b'A short response', response)

    def test_post_is_disabled_by_default(self):
        response = self.request(enabled=False)
        self.assertIn(b'404 Not Found', response)
        self.assertEqual(self.responses.read_drafts(), [])

    def test_non_loopback_client_is_rejected_even_when_enabled(self):
        response = self.request(client='203.0.113.10')
        self.assertIn(b'404 Not Found', response)
        self.assertEqual(self.responses.read_drafts(), [])

    def test_unpublished_or_invalid_journal_id_is_rejected(self):
        for path in ('/api/journal/not-published/responses',
                     '/api/journal/%2e%2e/responses',
                     f'/api/journal/{JOURNAL_ID}/responses?x=1'):
            with self.subTest(path=path):
                self.assertIn(b'404', self.request(path=path))
        self.assertEqual(self.responses.read_drafts(), [])

    def test_content_type_length_and_chunked_rules(self):
        cases = [
            ({'content_type': 'text/plain'}, b'415'),
            ({'content_type': None}, b'415'),
            ({'content_length': None}, b'411'),
            ({'content_length': '8193'}, b'413'),
            ({'extra_headers': ('Transfer-Encoding: chunked',)}, b'400'),
            ({'extra_headers': ('Content-Encoding: gzip',)}, b'415'),
        ]
        for arguments, status in cases:
            with self.subTest(arguments=arguments):
                self.assertIn(status, self.request(**arguments))
        self.assertEqual(self.responses.read_drafts(), [])

    def test_malformed_duplicate_and_unknown_json_are_rejected(self):
        bodies = [
            '{bad',
            '{"agent_name":"A","agent_name":"B","response":"text"}',
            json.dumps(dict(external(), journal_id=JOURNAL_ID)),
            '[]',
        ]
        for body in bodies:
            with self.subTest(body=body[:20]):
                self.assertIn(b'400', self.request(body=body))
        self.assertEqual(self.responses.read_drafts(), [])

    def test_field_lengths_controls_and_http_source_are_rejected(self):
        rows = [
            external(agent_name='a' * 81),
            external(response='a' * 2001),
            external(agent_name='bad\x00name'),
            external(response='bad\x00text'),
            external(agent_name='bad\u202ename'),
            external(source_url='http://example.org/'),
            external(source_url=None),
        ]
        for row in rows:
            with self.subTest(keys=tuple(row)):
                self.assertIn(b'400', self.request(body=json.dumps(row)))
        self.assertEqual(self.responses.read_drafts(), [])

    def test_optional_source_and_multiline_text_are_accepted(self):
        row = {'agent_name': 'Example Agent', 'response': 'First.\nSecond.'}
        self.assertIn(b'202', self.request(body=json.dumps(row)))
        draft = self.responses.read_drafts()[0]
        self.assertEqual(draft['source_url'], '')
        self.assertEqual(draft['response'], 'First.\nSecond.')

    def test_secret_is_not_saved_or_reflected(self):
        secret = 'password=synthetic-secret-value'
        response = self.request(body=json.dumps(external(response=secret)))
        self.assertIn(b'400', response)
        self.assertNotIn(secret.encode(), response)
        self.assertEqual(self.responses.read_drafts(), [])

    def test_rate_limits_and_draft_cap_return_429(self):
        self.limiter = LocalResponseRateLimiter(
            source_limit=1, journal_limit=100, window=60)
        self.assertIn(b'202', self.request())
        self.assertIn(b'429', self.request())
        with patch('server.MAX_RESPONSE_DRAFTS', 1):
            self.limiter = LocalResponseRateLimiter(
                source_limit=100, journal_limit=100, window=60)
            self.assertIn(b'429', self.request())

    def test_journal_rate_limit_is_independent_of_source_limit(self):
        self.limiter = LocalResponseRateLimiter(
            source_limit=100, journal_limit=1, window=60)
        self.assertIn(b'202', self.request())
        self.assertIn(b'429', self.request())

    def test_storage_failure_returns_503(self):
        with patch.object(self.responses, 'save_draft', side_effect=OSError('private')):
            response = self.request()
        self.assertIn(b'503', response)
        self.assertNotIn(b'private', response)

    def test_post_does_not_enter_public_stats_or_cors(self):
        log = self.root / 'requests.jsonl'
        with patch('server.LOG', log):
            response = self.request()
        self.assertFalse(log.exists())
        self.assertNotIn(b'PRIVATE-UA-MARKER', response)
        self.assertNotIn(b'Access-Control-Allow-Origin', response)
        self.assertNotIn(b'Access-Control-Allow-Credentials', response)
        options = self.request(method='OPTIONS', body=b'', content_type=None,
                               content_length=None)
        self.assertIn(b'405', options)
        self.assertNotIn(b'Access-Control-Allow-Origin', options)

    def test_response_post_never_calls_journal_openai_or_crawler(self):
        with patch('journal.generate_journal_entry') as generate, \
             patch('journal_openai.OpenAIJournalAdapter') as adapter, \
             patch('crawler.crawl') as crawl:
            self.assertIn(b'202', self.request())
        generate.assert_not_called()
        adapter.assert_not_called()
        crawl.assert_not_called()


class ResponseStorageAndCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.journals = FileStore(self.root)
        self.journals.save('published', 'journal', journal_row())
        self.responses = FileResponseStore(self.root)
        self.draft = build_draft_response(
            external(), JOURNAL_ID,
            now=datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc),
            response_id='response-test-001',
        )
        self.responses.save_draft(self.draft)

    def run_manage(self, arguments):
        with patch('manage.FileStore', return_value=self.journals), \
             patch('manage.FileResponseStore', return_value=self.responses), \
             patch('sys.argv', ['manage.py'] + arguments), \
             patch('sys.stdout', new_callable=io.StringIO) as stdout, \
             patch('sys.stderr', new_callable=io.StringIO) as stderr:
            stopped = None
            try:
                manage.main()
            except SystemExit as exc:
                stopped = exc
        return stopped, stdout.getvalue(), stderr.getvalue()

    def test_draft_permissions_and_overwrite_refusal(self):
        path = self.responses.directory('drafts') / 'response-test-001.json'
        if os.name == 'posix':
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        with self.assertRaises(FileExistsError):
            self.responses.save_draft(self.draft)
        with self.assertRaises(ResponseValidationError):
            self.responses.load_draft('../private')

    def test_review_response_prints_local_draft(self):
        stopped, output, error = self.run_manage(['review', 'response'])
        self.assertIsNone(stopped)
        self.assertIn('response-test-001', output)
        self.assertIn('A short response.', output)
        self.assertEqual(error, '')

    def test_publish_requires_review_and_copies_only_public_schema(self):
        stopped, output, error = self.run_manage(
            ['publish', 'response', 'response-test-001'])
        self.assertEqual(stopped.code, 1)
        self.assertIn('Human review acknowledgement required', error)
        self.assertEqual(self.responses.read_published(), [])
        stopped, output, error = self.run_manage(
            ['publish', 'response', 'response-test-001', '--reviewed'])
        self.assertIsNone(stopped)
        published = self.responses.read_published()[0]
        self.assertEqual(set(published), {
            'id', 'journal_id', 'published_at', 'agent_name', 'response',
            'source_url'})
        self.assertRegex(published['published_at'], r'^\d{4}-\d{2}-\d{2}$')
        self.assertNotIn('received_at', published)
        self.assertTrue((self.responses.directory('drafts') /
                         'response-test-001.json').exists())
        with self.assertRaises(FileExistsError):
            self.responses.publish('response-test-001', reviewed=True)

    def test_publish_rechecks_secret_and_published_journal(self):
        path = self.responses.directory('drafts') / 'response-test-001.json'
        unsafe = dict(self.draft, response='password=synthetic-secret-value')
        path.write_text(json.dumps(unsafe), encoding='utf-8')
        stopped, output, error = self.run_manage(
            ['publish', 'response', 'response-test-001', '--reviewed'])
        self.assertEqual(stopped.code, 1)
        self.assertNotIn('synthetic-secret-value', output + error)
        self.assertEqual(self.responses.read_published(), [])

        path.write_text(json.dumps(dict(self.draft, journal_id='missing-journal')),
                        encoding='utf-8')
        stopped, output, error = self.run_manage(
            ['publish', 'response', 'response-test-001', '--reviewed'])
        self.assertEqual(stopped.code, 1)
        self.assertIn('Published Journal not found', error)
        self.assertEqual(self.responses.read_published(), [])


class ResponseRenderingAndBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.journals = FileStore(self.root)
        self.journals.save('published', 'journal', journal_row())
        self.responses = FileResponseStore(self.root)

    def test_journal_id_is_machine_readable_and_empty_is_quiet(self):
        html = content.render_page('journal', self.journals,
                                   self.responses).decode()
        self.assertIn(f'data-journal-id="{JOURNAL_ID}"', html)
        self.assertNotIn('<h3>Responses</h3>', html)

    def test_only_published_response_is_rendered_and_escaped(self):
        draft = build_draft_response(
            external(agent_name='<Agent>', response='<script>alert(1)</script>'),
            JOURNAL_ID, response_id='response-visible-test')
        self.responses.save_draft(draft)
        html = content.render_page('journal', self.journals,
                                   self.responses).decode()
        self.assertNotIn('Claimed agent:', html)
        self.responses.publish(
            'response-visible-test', reviewed=True,
            publication_date='2026-09-29')
        html = content.render_page('journal', self.journals,
                                   self.responses).decode()
        self.assertIn('<h3>Responses</h3>', html)
        self.assertIn('Claimed agent: &lt;Agent&gt;', html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('rel="nofollow ugc noreferrer"', html)

    def test_response_modules_have_no_generation_or_crawler_dependency(self):
        forbidden_modules = {'journal', 'journal_openai', 'crawler'}
        for filename in ('response.py', 'response_store.py'):
            tree = ast.parse((content.ROOT / filename).read_text(encoding='utf-8'))
            imports = set()
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.add(node.module)
                elif isinstance(node, ast.Name):
                    names.add(node.id)
            self.assertTrue(forbidden_modules.isdisjoint(imports))
            self.assertNotIn('generate_journal_entry', names)
            self.assertNotIn('OpenAIJournalAdapter', names)

    def test_existing_journal_context_never_includes_responses(self):
        self.responses.save_draft(build_draft_response(
            external(response='PRIVATE_RESPONSE_MARKER'), JOURNAL_ID,
            response_id='response-private-test'))
        adapter = Mock()
        adapter.generate_text.return_value = None
        context_data = {
            'source_url': 'https://example.org/source',
            'public_text': 'CURRENT_PUBLIC_TEXT',
        }
        journal.generate_journal_entry(context_data, adapter=adapter)
        request = adapter.generate_text.call_args.args[0]
        self.assertEqual(set(vars(request)),
                         {'instructions', 'source_url', 'public_text'})
        self.assertNotIn('PRIVATE_RESPONSE_MARKER', json.dumps(vars(request)))

    def test_agent_json_documents_disabled_reviewed_endpoint(self):
        specification = json.loads(
            (content.ROOT / 'agent.json').read_text(encoding='utf-8'))
        intake = specification['journal_response_intake']
        self.assertEqual(intake['method'], 'POST')
        self.assertEqual(intake['content_type'], 'application/json')
        self.assertEqual(intake['max_request_bytes'], 8192)
        self.assertTrue(intake['human_review_required'])
        self.assertFalse(intake['automatic_publication'])
        self.assertIn('loopback', intake['phase1_network_scope'])
        self.assertIn('disabled', intake['production_intake'])


class ResponseValidationTests(unittest.TestCase):
    def test_external_schema_is_exact_and_name_is_unverified_data(self):
        row = validate_external_response(external(agent_name='Claimed Bot'))
        self.assertEqual(row['agent_name'], 'Claimed Bot')
        for forbidden in ('id', 'journal_id', 'received_at', 'published_at'):
            with self.subTest(field=forbidden):
                with self.assertRaises(ResponseValidationError):
                    validate_external_response(dict(external(), **{forbidden: 'x'}))


if __name__ == '__main__':
    unittest.main()
