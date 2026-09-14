import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import journal
import manage
from content import FileStore
from journal_adapter import JournalAdapterError


CRAWL_CONTEXT = {
    'source_url': 'https://llmstxt.org/',
    'public_text': 'A public page from this crawl only.',
}


class LivePipelineTests(unittest.TestCase):
    def setUp(self):
        network_guard = patch('socket.create_connection',
                              side_effect=AssertionError('network forbidden'))
        network_guard.start()
        self.addCleanup(network_guard.stop)

    def run_crawl(self, store, argv, *, key=None, output=None, error=None,
                  context=None, prompt='Runtime Journal policy.'):
        environment = {} if key is None else {'OPENAI_API_KEY': key}
        prompt_dir = tempfile.TemporaryDirectory()
        self.addCleanup(prompt_dir.cleanup)
        prompt_path = Path(prompt_dir.name) / 'journal_prompt.txt'
        prompt_path.write_text(prompt, encoding='utf-8')
        with patch.dict(os.environ, environment, clear=True), \
             patch('sys.argv', ['manage.py', 'crawl'] + argv), \
             patch('manage.FileStore', return_value=store), \
             patch('manage.crawl', return_value=context or CRAWL_CONTEXT) as crawl, \
             patch('journal.PROMPT_PATH', prompt_path), \
             patch('journal_openai.OpenAIJournalAdapter') as adapter_class, \
             patch('sys.stdout', new_callable=io.StringIO) as stdout, \
             patch('sys.stderr', new_callable=io.StringIO) as stderr:
            adapter_class.return_value.generate_text.return_value = output
            if error is not None:
                adapter_class.return_value.generate_text.side_effect = error
            stopped = None
            try:
                manage.main()
            except SystemExit as exc:
                stopped = exc
            return {
                'stopped': stopped,
                'stdout': stdout.getvalue(),
                'stderr': stderr.getvalue(),
                'crawl': crawl,
                'adapter_class': adapter_class,
                'adapter': adapter_class.return_value,
                'prompt': prompt,
            }

    def test_normal_crawl_stays_off_even_when_key_exists(self):
        for key in (None, 'fake-local-only'):
            with self.subTest(key_present=key is not None), tempfile.TemporaryDirectory() as root:
                store = FileStore(root)
                result = self.run_crawl(
                    store, ['https://llmstxt.org/', '--name', 'llms.txt'], key=key)
                self.assertIsNone(result['stopped'])
                result['adapter_class'].assert_not_called()
                result['crawl'].assert_called_once_with('https://llmstxt.org/')
                self.assertEqual(len(store.read('drafts', 'archive')), 1)
                self.assertEqual(store.read('drafts', 'journal'), [])
                self.assertFalse((Path(root) / 'published').exists())

    def test_live_without_key_stops_before_crawl_or_storage(self):
        for key in (None, '', ' '):
            with self.subTest(key=repr(key)), tempfile.TemporaryDirectory() as root:
                store = FileStore(root)
                result = self.run_crawl(
                    store,
                    ['https://llmstxt.org/', '--name', 'llms.txt', '--journal-live'],
                    key=key,
                )
                self.assertEqual(result['stopped'].code, 1)
                result['crawl'].assert_not_called()
                result['adapter_class'].assert_not_called()
                self.assertEqual(list(Path(root).iterdir()), [])
                self.assertIn('adapter unavailable', result['stderr'])

    def test_live_url_is_validated_before_key_lookup_or_crawl(self):
        with tempfile.TemporaryDirectory() as root:
            store = FileStore(root)
            result = self.run_crawl(
                store,
                ['http://example.org/', '--name', 'Example', '--journal-live'],
                key=None,
            )
            self.assertEqual(result['stopped'].code, 1)
            result['crawl'].assert_not_called()
            result['adapter_class'].assert_not_called()
            self.assertEqual(list(Path(root).iterdir()), [])
            self.assertNotIn('OPENAI_API_KEY', result['stdout'] + result['stderr'])

    def test_live_skip_keeps_only_archive_and_calls_once(self):
        key = 'fake-local-only'
        with tempfile.TemporaryDirectory() as root:
            store = FileStore(root)
            result = self.run_crawl(
                store,
                ['https://llmstxt.org/', '--name', 'llms.txt', '--journal-live'],
                key=key,
                output=None,
            )
            self.assertIsNone(result['stopped'])
            result['crawl'].assert_called_once_with('https://llmstxt.org/')
            result['adapter_class'].assert_called_once()
            kwargs = result['adapter_class'].call_args.kwargs
            self.assertEqual(set(kwargs), {'api_key', 'config', 'allow_live'})
            self.assertEqual(kwargs['api_key'], key)
            self.assertTrue(kwargs['allow_live'])
            self.assertEqual(kwargs['config'].model, 'gpt-5.6-luna')
            result['adapter'].generate_text.assert_called_once()
            request = result['adapter'].generate_text.call_args.args[0]
            self.assertEqual(request.instructions, result['prompt'])
            self.assertEqual(request.source_url, CRAWL_CONTEXT['source_url'])
            self.assertEqual(request.public_text, CRAWL_CONTEXT['public_text'])
            self.assertEqual(set(vars(request)),
                             {'instructions', 'source_url', 'public_text'})
            self.assertEqual(len(store.read('drafts', 'archive')), 1)
            self.assertEqual(store.read('drafts', 'journal'), [])
            self.assertFalse((Path(root) / 'published').exists())
            self.assertNotIn(key, result['stdout'] + result['stderr'])
            self.assertNotIn(CRAWL_CONTEXT['public_text'],
                             result['stdout'] + result['stderr'])
            saved = ''.join(path.read_text(encoding='utf-8')
                            for path in Path(root).rglob('*.json'))
            self.assertNotIn(key, saved)

    def test_live_text_creates_valid_journal_draft_with_fixed_source(self):
        with tempfile.TemporaryDirectory() as root:
            store = FileStore(root)
            result = self.run_crawl(
                store,
                ['https://llmstxt.org/', '--name', 'llms.txt', '--journal-live'],
                key='fake-local-only',
                output='A short observation mentioning https://untrusted.example/.',
            )
            self.assertIsNone(result['stopped'])
            archive = store.read('drafts', 'archive')
            entries = store.read('drafts', 'journal')
            self.assertEqual(len(archive), 1)
            self.assertEqual(len(entries), 1)
            entry = entries[0]
            self.assertEqual(set(entry), {'id', 'date', 'text', 'source_urls'})
            self.assertRegex(entry['id'],
                             r'^journal-\d{4}-\d{2}-\d{2}-[a-f0-9]{32}$')
            self.assertRegex(entry['date'], r'^\d{4}-\d{2}-\d{2}$')
            self.assertEqual(entry['source_urls'], ['https://llmstxt.org/'])
            self.assertFalse((Path(root) / 'published').exists())

    def test_secret_stops_before_adapter_and_both_drafts(self):
        secret = 'password=synthetic-secret-value'
        with tempfile.TemporaryDirectory() as root:
            store = FileStore(root)
            result = self.run_crawl(
                store,
                ['https://llmstxt.org/', '--name', 'llms.txt', '--journal-live'],
                key='fake-local-only',
                context={'source_url': 'https://llmstxt.org/',
                         'public_text': secret},
            )
            self.assertEqual(result['stopped'].code, 1)
            result['crawl'].assert_called_once()
            result['adapter_class'].assert_not_called()
            self.assertEqual(list(Path(root).iterdir()), [])
            self.assertNotIn(secret, result['stdout'] + result['stderr'])

    def test_provider_error_keeps_archive_without_retry_or_leaks(self):
        for category in ('authentication', 'permission_model_access',
                         'quota_billing', 'rate_limit', 'timeout', 'connection',
                         'malformed_response', 'incomplete_response',
                         'provider_internal'):
            with self.subTest(category=category), tempfile.TemporaryDirectory() as root:
                store = FileStore(root)
                result = self.run_crawl(
                    store,
                    ['https://llmstxt.org/', '--name', 'llms.txt', '--journal-live'],
                    key='fake-local-only',
                    error=JournalAdapterError(category),
                )
                self.assertEqual(result['stopped'].code, 1)
                result['adapter'].generate_text.assert_called_once()
                self.assertEqual(len(store.read('drafts', 'archive')), 1)
                self.assertEqual(store.read('drafts', 'journal'), [])
                self.assertFalse((Path(root) / 'published').exists())
                output = result['stdout'] + result['stderr']
                self.assertIn(category, output)
                self.assertNotIn('fake-local-only', output)
                self.assertNotIn(CRAWL_CONTEXT['public_text'], output)

    def test_past_local_content_never_enters_request(self):
        with tempfile.TemporaryDirectory() as root:
            store = FileStore(root)
            store.save('drafts', 'journal', {
                'id': 'old-journal', 'date': '2026-09-13',
                'text': 'OLD_JOURNAL_PRIVATE_MARKER',
                'source_urls': ['https://example.org/'],
            })
            old_archive = {
                'id': 'old-archive', 'discovered_at': '2026-09-13',
                'name': 'OLD_ARCHIVE_PRIVATE_MARKER', 'category': 'test',
                'status': 'unknown', 'source_url': 'https://example.org/',
                'description': 'Old.', 'note': '', 'tags': [],
            }
            store.save('drafts', 'archive', old_archive)
            result = self.run_crawl(
                store,
                ['https://llmstxt.org/', '--name', 'llms.txt', '--journal-live'],
                key='fake-local-only', output=None,
            )
            request = result['adapter'].generate_text.call_args.args[0]
            serialized = json.dumps(vars(request))
            self.assertNotIn('OLD_JOURNAL_PRIVATE_MARKER', serialized)
            self.assertNotIn('OLD_ARCHIVE_PRIVATE_MARKER', serialized)


if __name__ == '__main__':
    unittest.main()
