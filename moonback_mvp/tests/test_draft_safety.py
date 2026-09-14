import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from content import FileStore
import manage


def item():
    return dict(id='permission-test', discovered_at='2026-09-14', name='Example',
                category='unknown', status='unknown', source_url='https://example.org/',
                description='Public observation.', note='', tags=[])


class DraftSafetyTests(unittest.TestCase):
    def test_secret_stops_before_any_save_or_generation(self):
        for secret in ('password=synthetic123', 'sk-'+'a'*25,
                       '-----BEGIN PRIVATE KEY-----', 'Authorization: Bearer synthetic123'):
            with tempfile.TemporaryDirectory() as directory:
                store=FileStore(directory)
                existing=store.save('drafts','archive',item())
                before=existing.read_bytes()
                with patch('manage.FileStore',return_value=store), patch('manage.crawl',return_value={
                    'source_url':'https://example.org/','public_text':secret}), patch('manage.generate_journal_entry') as generate, patch('sys.argv',[
                    'manage.py','crawl','https://example.org/','--name','Example']), patch('sys.stdout',new_callable=io.StringIO) as out, patch('sys.stderr',new_callable=io.StringIO) as err:
                    with self.assertRaises(SystemExit) as caught: manage.main()
                    self.assertEqual(caught.exception.code,1)
                    generate.assert_not_called()
                    self.assertNotIn(secret,out.getvalue()+err.getvalue())
                    self.assertIn('No Archive or Journal draft created',err.getvalue())
                self.assertEqual(existing.read_bytes(),before)
                self.assertEqual(len(store.read('drafts','archive')),1)
                self.assertEqual(store.read('drafts','journal'),[])
                self.assertFalse((Path(directory)/'published').exists())

    def test_cli_reports_rule_only_and_never_secret_value(self):
        secret = 'dummy-secret-like-value'
        text = 'Authorization: Bearer ' + secret
        with tempfile.TemporaryDirectory() as directory:
            store = FileStore(directory)
            with patch('manage.FileStore', return_value=store), \
                 patch('manage.crawl', return_value={
                     'source_url': 'https://example.org/',
                     'public_text': text,
                 }), \
                 patch('manage.generate_journal_entry') as generate, \
                 patch('sys.argv', [
                     'manage.py', 'crawl', 'https://example.org/',
                     '--name', 'Example',
                 ]), \
                 patch('sys.stdout', new_callable=io.StringIO) as out, \
                 patch('sys.stderr', new_callable=io.StringIO) as err:
                with self.assertRaises(SystemExit) as caught:
                    manage.main()
                self.assertEqual(caught.exception.code, 1)
                generate.assert_not_called()
                output = out.getvalue() + err.getvalue()
                self.assertIn('rule=authorization_scheme', output)
                self.assertNotIn(secret, output)
                self.assertNotIn(text, output)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_secret_in_source_path_also_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('manage.FileStore',return_value=FileStore(directory)), patch('manage.crawl',return_value={
                'source_url':'https://example.org/password%3Dsynthetic123','public_text':'Public text'}), patch('manage.generate_journal_entry') as generate, patch('sys.argv',[
                'manage.py','crawl','https://example.org/','--name','Example']), patch('sys.stderr',new_callable=io.StringIO):
                with self.assertRaises(SystemExit): manage.main()
                generate.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()),[])

    def test_public_contacts_preserved_and_generation_reached(self):
        with tempfile.TemporaryDirectory() as directory:
            store=FileStore(directory)
            context={'source_url':'https://example.org/', 'public_text':
                     'Jane Example, Example Company. hello@example.org +81-3-1234-5678.'}
            with patch('manage.FileStore',return_value=store), patch('manage.crawl',return_value=context), patch('manage.generate_journal_entry',return_value=None) as generate, patch('sys.argv',[
                'manage.py','crawl','https://example.org/','--name','Example']), patch('sys.stdout',new_callable=io.StringIO):
                manage.main()
                generate.assert_called_once_with(context, adapter=None)
            self.assertEqual(store.read('drafts','archive')[0]['review_context'],context)

    @unittest.skipUnless(os.name=='posix','POSIX permission bits required')
    def test_new_draft_permissions_and_existing_public_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            store=FileStore(directory)
            public=store.save('published','archive',item())
            public_mode=stat.S_IMODE(public.stat().st_mode)
            public_bytes=public.read_bytes()
            path=store.save('drafts','archive',item())
            journal=store.save('drafts','journal',dict(id='journal-test',date='2026-09-14',text='Test.',source_urls=['https://example.org/']))
            for folder in (path.parent.parent,path.parent,journal.parent):
                self.assertEqual(stat.S_IMODE(folder.stat().st_mode),0o700)
            for file in (path,journal):
                self.assertEqual(stat.S_IMODE(file.stat().st_mode),0o600)
            self.assertEqual(stat.S_IMODE(public.stat().st_mode),public_mode)
            self.assertEqual(public.read_bytes(),public_bytes)
            with self.assertRaises(FileExistsError): store.save('drafts','archive',item())

    @unittest.skipUnless(os.name=='posix','POSIX permission bits required')
    def test_existing_draft_directory_not_chmoded(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)/'drafts'/'archive'
            folder.mkdir(parents=True); folder.chmod(0o755)
            path=FileStore(directory).save('drafts','archive',item())
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode),0o755)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)

    def test_permission_failure_does_not_write_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('content.os.open',side_effect=PermissionError('denied')):
                with self.assertRaises(PermissionError): FileStore(directory).save('drafts','archive',item())
            self.assertEqual(list(Path(directory).rglob('*.json')),[])

    def test_git_exclusion_and_gitignore_addable(self):
        root=Path(__file__).resolve().parents[2]
        result=subprocess.run(['git','check-ignore','-v','moonback_mvp/drafts/archive/example.json'],cwd=root,capture_output=True,text=True)
        self.assertEqual(result.returncode,0)
        self.assertIn('moonback_mvp/drafts/',result.stdout)
        result=subprocess.run(['git','ls-files','--','moonback_mvp/drafts'],cwd=root,capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout,'')
        result=subprocess.run(['git','check-ignore','.gitignore'],cwd=root,capture_output=True,text=True)
        self.assertEqual(result.returncode,1)
        self.assertTrue((root/'.gitignore').is_file())


if __name__=='__main__': unittest.main()
