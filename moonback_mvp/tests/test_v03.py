import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from email.message import Message

import content
import crawler
import server
import manage
from journal import generate_journal_entry


def archive():
    return dict(id='test', discovered_at='2026-09-13', name='<script>alert(1)</script>', category='file', status='unknown', source_url='https://example.org/', description='A & B', note='', tags=[])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = content.FileStore(self.tmp.name)

    def test_draft_is_not_public_and_review_required(self):
        row = archive()
        row['review_context'] = {'public_text': 'CONTEXT_NOT_FOR_PUBLICATION'}
        self.store.save('drafts', 'archive', row)
        self.assertNotIn(b'alert', content.render_page('archive', self.store))
        with self.assertRaises(ValueError):
            self.store.publish('archive', 'test')
        self.store.publish('archive', 'test', reviewed=True)
        html = content.render_page('archive', self.store).decode()
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>', html)
        self.assertNotIn('review_context', self.store.read('published', 'archive')[0])
        with self.assertRaises(FileExistsError):
            self.store.publish('archive', 'test', reviewed=True)

    def test_english_page_labels_and_empty_states(self):
        for kind, title in (('archive', 'AI/Web Archive'), ('journal', 'Luna Journal')):
            with self.subTest(kind=kind):
                html = content.render_page(kind, self.store).decode()
                self.assertIn('<html lang="en">', html)
                self.assertIn('<h1>' + title + '</h1>', html)
                self.assertIn('No published records yet.', html)
        self.store.save('published', 'archive', archive())
        self.store.save('published', 'journal', dict(
            id='original-term', date='2026-09-13', text='The term 「観測」 remains unresolved.',
            source_urls=['https://example.org/']))
        for kind in ('archive', 'journal'):
            html = content.render_page(kind, self.store).decode()
            self.assertIn('>Source</a>', html)
            self.assertNotIn('No published records yet.', html)
        self.assertIn('「観測」', content.render_page('journal', self.store).decode())

    def test_journal_prompt_language_and_review_policy(self):
        prompt = (content.ROOT / 'journal_prompt.txt').read_text(encoding='utf-8')
        for instruction in (
            'Write journal entries in English by default.',
            'Do not translate proper nouns unnecessarily.',
            'Preserve original technical terms where useful.',
            'Do not write for the Director.',
            'Do not optimize for a Japanese-speaking reader.',
            "Do not reference the Director's preferences, opinions, or chat history.",
            'Do not make the entry entertaining on purpose.',
            'Do not force a conclusion.',
            'A short unresolved observation is acceptable.',
            'Do not translate entries into Japanese for the Director when saving them.',
            'Never publish before human review.',
        ):
            self.assertIn(instruction, prompt)

    def test_workflow_metadata_stays_private_and_source_remains_visible(self):
        row = archive()
        row['note'] = 'Primary source checked. The discovery date is the date recorded in MoonBack.'
        row['workflow_note'] = 'Awaiting human review before publication.'
        self.store.save('drafts', 'archive', row)
        self.store.publish('archive', 'test', reviewed=True)
        published = self.store.read('published', 'archive')[0]
        self.assertNotIn('workflow_note', published)
        self.assertEqual(published['note'], row['note'])
        html = content.render_page('archive', self.store).decode()
        self.assertNotIn(row['workflow_note'], html)
        self.assertIn(row['note'], html)
        self.assertIn('href="https://example.org/">Source</a>', html)
        self.assertEqual(self.store.read('drafts', 'archive')[0]['workflow_note'], row['workflow_note'])
        # Existing published records are read afresh without republishing.
        published['note'] = 'Corrected public note.'
        path = self.store.directory('published', 'archive') / 'test.json'
        path.write_text(json.dumps(published), encoding='utf-8')
        self.assertIn('Corrected public note.', content.render_page('archive', self.store).decode())

    def test_invalid_data(self):
        for field, value in [('id','../test'),('tags','bad'),('name',None),('discovered_at','bad'),('source_url','javascript:alert(1)'),('source_url','https://example.org/?token=x')]:
            row=archive(); row[field]=value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.store.save('drafts','archive',row)

    def test_journal_order_escape_and_draft(self):
        for day in ('12','13'):
            self.store.save('published','journal',dict(id='j'+day,date='2026-09-'+day,text='<img src=x>\n観測',source_urls=['https://example.org/']))
        html=content.render_page('journal',self.store).decode()
        self.assertLess(html.index('2026-09-13'),html.index('2026-09-12'))
        self.assertNotIn('<img',html)
        self.assertIn('&lt;img',html)
        self.assertIsNone(generate_journal_entry({'source_url':'https://example.org/', 'public_text':'anything'}))

    def test_malformed_public_json_fails_closed(self):
        directory=self.store.directory('published','archive'); directory.mkdir(parents=True)
        (directory/'bad.json').write_text('{')
        with self.assertRaises(ValueError):
            content.render_page('archive',self.store)


class CrawlTests(unittest.TestCase):
    def test_private_addresses(self):
        for ip in ('127.0.0.1','10.0.0.1','169.254.169.254','::1','192.168.1.1'):
            with patch('crawler.socket.getaddrinfo',return_value=[(None,None,None,None,(ip,443))]), patch('crawler.PinnedHTTPS') as connection:
                with self.assertRaises(ValueError): crawler.fetch('https://example.org/')
                connection.assert_not_called()

    def test_url_restrictions(self):
        for url in ('http://example.org/','file:///etc/passwd','https://u:p@example.org/','https://example.org:8443/','https://example.org/?key=x'):
            with self.assertRaises(ValueError): crawler.crawl(url)

    def test_robots_denial_and_errors_stop_target(self):
        for rules in ('User-agent: *\nDisallow: /', 'User-agent: *\nDisallow: /*secret'):
            with patch('crawler.fetch',return_value=(rules,'text/plain')) as fetch:
                with self.assertRaises(ValueError): crawler.crawl('https://example.org/secret')
                self.assertEqual(fetch.call_count,1)
        with patch('crawler.fetch',side_effect=ValueError('HTTP status refused: 404')) as fetch:
            with self.assertRaises(ValueError): crawler.crawl('https://example.org/')
            self.assertEqual(fetch.call_count,1)

    def test_public_context_and_delay(self):
        with patch('crawler.fetch',side_effect=[('User-agent: *\nAllow: /\nCrawl-delay: 3','text/plain'),('<p>public</p><script>secret</script>','text/html')]), patch('crawler.time.sleep') as sleep:
            result=crawler.crawl('https://example.org/')
            self.assertEqual(result,dict(source_url='https://example.org/',public_text='public'))
            sleep.assert_called_once_with(3.0)

    def test_http_redirect_and_size_are_refused(self):
        with patch('crawler.socket.getaddrinfo',return_value=[(None,None,None,None,('8.8.8.8',443))]), patch('crawler.PinnedHTTPS') as cls:
            response=cls.return_value.getresponse.return_value
            response.status=302
            with self.assertRaises(ValueError): crawler.fetch('https://example.org/')
            response.status=200; response.getheader.return_value='identity'
            response.headers=Message(); response.headers['Content-Type']='text/plain'
            response.read.return_value=b'a'*(crawler.LIMIT+1)
            with self.assertRaises(ValueError): crawler.fetch('https://example.org/')
            self.assertEqual(cls.call_args.args,('example.org','8.8.8.8'))


class WorkflowTests(unittest.TestCase):
    def test_cli_crawl_and_manual_journal_stay_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            store=content.FileStore(directory)
            with patch('manage.FileStore',return_value=store), patch('manage.crawl',return_value={'source_url':'https://example.org/','public_text':'Public source only'}), patch('sys.argv',['manage.py','crawl','https://example.org/','--name','Example']), patch('sys.stdout',new_callable=io.StringIO):
                manage.main()
            self.assertEqual(len(store.read('drafts','archive')),1)
            self.assertEqual(store.read('drafts', 'archive')[0]['description'],
                             'Retrieved. Review the source and write a description in English.')
            self.assertEqual(store.read('published','archive'),[])
            self.assertEqual(store.read('drafts','journal'),[])
            source=Path(directory)/'manual.json'
            source.write_text(json.dumps(dict(id='manual',date='2026-09-13',text='test observation',source_urls=['https://example.org/'])))
            for args in (['import','journal',str(source)], ['review','journal']):
                with patch('manage.FileStore',return_value=store), patch('sys.argv',['manage.py']+args), patch('sys.stdout',new_callable=io.StringIO):
                    manage.main()
            self.assertEqual(store.read('published','journal'),[])
            with patch('manage.FileStore',return_value=store), patch('sys.argv',['manage.py','publish','journal','manual']), patch('sys.stderr',new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as stopped: manage.main()
                self.assertEqual(stopped.exception.code,1)
            with patch('manage.FileStore',return_value=store), patch('sys.argv',['manage.py','publish','journal','manual','--reviewed']), patch('sys.stdout',new_callable=io.StringIO):
                manage.main()
            self.assertEqual(len(store.read('published','journal')),1)


class FakeSocket:
    def __init__(self, request):
        self.input=io.BytesIO(request)
        self.output=io.BytesIO()
    def makefile(self,*args,**kwargs): return self.input
    def sendall(self,data): self.output.write(data)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.log=Path(self.tmp.name)/'requests.jsonl'
        self.patch=patch('server.LOG',self.log); self.patch.start(); self.addCleanup(self.patch.stop)

    def request(self,path,method='GET'):
        sock=FakeSocket((method+' '+path+' HTTP/1.0\r\nUser-Agent: testbot\r\n\r\n').encode())
        with patch.object(server.Handler,'log_message'):
            server.Handler(sock,('127.0.0.1',12345),None)
        return sock.output.getvalue()

    def test_existing_and_new_routes(self):
        for path in ('/','/observe','/stats','/signal/001','/signal/002','/signal/003','/signal/004','/signal/005.json','/archive','/journal','/agent.json','/llms.txt','/sitemap.xml','/robots.txt'):
            with self.subTest(path=path): self.assertIn(b'200 OK',self.request(path))
        self.assertEqual(self.request('/').split(b'\r\n\r\n',1)[1],(server.ROOT/'index.html').read_bytes())
        paths=[json.loads(line)['path'] for line in self.log.read_text().splitlines()]
        self.assertIn('/archive',paths); self.assertIn('/journal',paths)
        stats=server.read_stats(); self.assertEqual(stats['deepest_signal'],'005')
        self.assertNotIn('/stats',paths)
        self.request('/missing')
        self.assertEqual(json.loads(self.log.read_text().splitlines()[-1])['path'],'/missing')

    def test_private_routes_get_and_head(self):
        for path in ('/drafts/archive/llms-txt.json','/published/archive/test.json','/data/requests.jsonl','/server.py','/server.py.backup','/.env','/%64rafts/','/signal/../server.py','/signal/','//drafts/archive/test.json'):
            for method in ('GET','HEAD'):
                with self.subTest(path=path,method=method): self.assertIn(b'404',self.request(path,method))

    def test_head_and_bad_content(self):
        response=self.request('/archive','HEAD')
        self.assertIn(b'200 OK',response)
        self.assertEqual(response.split(b'\r\n\r\n',1)[1],b'')
        with patch('server.render_page',side_effect=ValueError('private diagnostic')):
            response=self.request('/archive')
            self.assertIn(b'503',response)
            self.assertNotIn(b'private diagnostic',response)


if __name__ == '__main__': unittest.main()
