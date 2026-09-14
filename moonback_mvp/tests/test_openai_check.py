import io
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import manage
from content import FileStore
from journal_adapter import JournalAdapterError
import openai_check


class CheckTests(unittest.TestCase):
    def setUp(self):
        guard=patch('socket.create_connection',side_effect=AssertionError('network forbidden'))
        guard.start();self.addCleanup(guard.stop)

    def invoke(self, args, key=None, result=None, error=None):
        environment={} if key is None else {'OPENAI_API_KEY':key}
        with patch.dict(os.environ,environment,clear=True), patch('sys.argv',['manage.py','openai-check']+args), patch('sys.stdout',new_callable=io.StringIO) as out, patch('sys.stderr',new_callable=io.StringIO) as err, patch('openai_check.OpenAIJournalAdapter') as cls, patch('manage.crawl') as crawl, patch('manage.FileStore') as store, patch('manage.generate_journal_entry') as generate:
            cls.return_value.generate_text.return_value=result
            if error is not None: cls.return_value.generate_text.side_effect=error
            with self.assertRaises(SystemExit) as stopped: manage.main()
            crawl.assert_not_called();store.assert_not_called();generate.assert_not_called()
            output=out.getvalue()+err.getvalue()
            if key and key.strip(): self.assertNotIn(key,output)
            return stopped.exception.code,output,cls

    def test_no_live_even_with_key(self):
        code,out,cls=self.invoke([],key='fake-local-only')
        self.assertEqual(code,2);self.assertIn('not_attempted',out)
        cls.assert_not_called()

    def test_no_key_no_request(self):
        for key in (None,'',' '):
            code,out,cls=self.invoke(['--live'],key=key)
            self.assertEqual(code,1);self.assertIn('unavailable',out)
            cls.assert_not_called()

    def test_skip_once_no_storage(self):
        code,out,cls=self.invoke(['--live'],key='fake-local-only')
        self.assertEqual(code,0);self.assertIn('result=skip',out)
        self.assertIn('request=completed',out)
        cls.assert_called_once()
        self.assertEqual(set(cls.call_args.kwargs),{'api_key','config','allow_live'})
        self.assertTrue(cls.call_args.kwargs['allow_live'])
        cls.return_value.generate_text.assert_called_once()
        request=cls.return_value.generate_text.call_args.args[0]
        self.assertIn('{"text": null}',request.instructions)
        self.assertNotIn(request.instructions,out)
        self.assertNotIn(request.public_text,out)

    def test_success_body_is_not_printed(self):
        code,out,cls=self.invoke(['--live'],key='fake-local-only',result='PRIVATE_RESPONSE_BODY')
        self.assertEqual(code,0);self.assertIn('result=success',out)
        self.assertNotIn('PRIVATE_RESPONSE_BODY',out)
        cls.return_value.generate_text.assert_called_once()

    def test_safe_categories_and_no_retries(self):
        for category in JournalAdapterError.CATEGORIES:
            code,out,cls=self.invoke(['--live'],key='fake-local-only',error=JournalAdapterError(category))
            self.assertEqual(code,1);self.assertIn('error='+category,out)
            cls.return_value.generate_text.assert_called_once()
        code,out,cls=self.invoke(['--live'],key='fake-local-only',error=RuntimeError('PRIVATE_RAW_EXCEPTION'))
        self.assertIn('error=provider_internal',out)
        self.assertNotIn('PRIVATE_RAW_EXCEPTION',out)
        cls.return_value.generate_text.assert_called_once()

    def test_no_transport_or_input_cli_options(self):
        code,out,cls=self.invoke(['--live','--transport','custom'])
        self.assertEqual(code,2);cls.assert_not_called()

    def test_normal_crawl_remains_disabled_with_env_key(self):
        with tempfile.TemporaryDirectory() as directory:
            store=FileStore(directory)
            with patch.dict(os.environ,{'OPENAI_API_KEY':'fake-local-only'}), patch('sys.argv',['manage.py','crawl','https://example.org/','--name','Example']), patch('manage.crawl',return_value={'source_url':'https://example.org/','public_text':'Public example.'}), patch('manage.FileStore',return_value=store), patch('openai_check.OpenAIJournalAdapter') as cls, patch('sys.stdout',new_callable=io.StringIO):
                manage.main();cls.assert_not_called()
            self.assertEqual(len(store.read('drafts','archive')),1)
            self.assertEqual(store.read('drafts','journal'),[])
            self.assertEqual(store.read('published','journal'),[])
