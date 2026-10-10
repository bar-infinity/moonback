import base64
import contextlib
import getpass
import io
import json
import unittest
from unittest.mock import patch, Mock

import manage
from response_admin import (ResponseAdminClient, ResponseAdminError, ADMIN_URL,
                            MAX_BYTES, safe_display, run_review)

SECRET = 'synthetic-admin-auth'
ID = 'response-test'
ROW = {'id': ID, 'journal_id': 'journal-test', 'received_at': '2026-10-10T00:00:00Z'}
DETAIL = dict(ROW, agent_name='Claimed Agent', response='Untrusted text', source_url='https://example.com/')

def encoded(data):
    return json.dumps(data).encode()

def cursor_for(item):
    return base64.urlsafe_b64encode(json.dumps(
        [item['received_at'], item['id']], separators=(',', ':')
    ).encode()).decode().rstrip('=')

class AdminClientTests(unittest.TestCase):
    def setUp(self):
        p = patch('socket.create_connection',side_effect=AssertionError('network forbidden'))
        p.start(); self.addCleanup(p.stop)

    def test_trusted_url_only(self):
        for url in ['http://'+ADMIN_URL[8:],ADMIN_URL+'/',ADMIN_URL+'?x=1',ADMIN_URL.replace('london-1976','other'),ADMIN_URL.replace('https://','https://user:pass@')]:
            with self.assertRaisesRegex(ResponseAdminError,'invalid_configuration'):
                ResponseAdminClient(SECRET,url=url)

    def test_list_one_get_auth_no_extra_material(self):
        transport=Mock(return_value=(200,encoded({'items':[ROW],'next_cursor':None})))
        client=ResponseAdminClient(SECRET,transport=transport)
        self.assertEqual(client.list_pending()['items'],[ROW])
        transport.assert_called_once()
        path,headers=transport.call_args.args
        self.assertEqual(path,'/v1/admin/responses?limit=20')
        self.assertEqual(set(headers),{'Authorization','Accept','User-Agent'})
        self.assertEqual(headers['Authorization'],'Bearer '+SECRET)
        self.assertNotIn(SECRET,path)
        self.assertFalse(hasattr(client,'save_draft'));self.assertFalse(hasattr(client,'publish'))
        client.close(); self.assertIsNone(client._secret)

    def test_detail_and_pending_only(self):
        transport=Mock(return_value=(200,encoded({'item':DETAIL,'status':'pending'})))
        client=ResponseAdminClient(SECRET,transport=transport)
        self.assertEqual(client.get_pending(ID)['item'],DETAIL)
        self.assertEqual(transport.call_args.args[0],'/v1/admin/responses/'+ID)
        for status in ['published','rejected']:
            transport.return_value=(200,encoded({'item':DETAIL,'status':status}))
            with self.assertRaises(ResponseAdminError): client.get_pending(ID)

    def test_pagination_and_bad_arguments_before_request(self):
        cursor=base64.urlsafe_b64encode(json.dumps([ROW['received_at'],ID],separators=(',',':')).encode()).decode().rstrip('=')
        transport=Mock(return_value=(200,encoded({'items':[],'next_cursor':None})))
        client=ResponseAdminClient(SECRET,transport=transport)
        client.list_pending(limit=2,cursor=cursor)
        self.assertIn('cursor=',transport.call_args.args[0])
        transport.reset_mock()
        for limit in [0,51,True,'20']:
            with self.assertRaises(ResponseAdminError):client.list_pending(limit=limit)
        with self.assertRaises(ResponseAdminError):client.list_pending(cursor='invalid')
        with self.assertRaises(ResponseAdminError):client.get_pending('../test')
        transport.assert_not_called()

    def test_fixed_errors_no_retry_or_provider_echo(self):
        for status,category in [(302,'redirect_refused'),(404,'not_found_or_unauthorized'),(429,'rate_limited'),(503,'service_unavailable'),(500,'provider_error')]:
            transport=Mock(return_value=(status,b'private-provider-details'))
            with self.assertRaisesRegex(ResponseAdminError,'^'+category+'$'):
                ResponseAdminClient(SECRET,transport=transport).list_pending()
            transport.assert_called_once()
        for error,category in [(TimeoutError(SECRET),'timeout'),(ConnectionError(SECRET),'connection')]:
            transport=Mock(side_effect=error)
            with self.assertRaisesRegex(ResponseAdminError,'^'+category+'$'):
                ResponseAdminClient(SECRET,transport=transport).list_pending()
            transport.assert_called_once()

    def test_malformed_and_huge_responses(self):
        cases=[b'not JSON',b'{"items":[],"items":[],"next_cursor":null}',encoded({'items':[DETAIL],'next_cursor':None}),encoded({'items':[],'next_cursor':'bad'}),b'x'*(MAX_BYTES+1)]
        for body in cases:
            with self.assertRaises(ResponseAdminError):
                ResponseAdminClient(SECRET,transport=lambda *args:(200,body)).list_pending()

    def test_deep_json_returns_fixed_error_for_list_and_detail(self):
        body = b'[' * 1500 + b'0' + b']' * 1500
        self.assertLess(len(body), MAX_BYTES)
        for operation in ['list', 'detail']:
            transport = Mock(return_value=(200, body))
            client = ResponseAdminClient(SECRET, transport=transport)
            with self.assertRaisesRegex(ResponseAdminError, '^malformed_response$'):
                client.list_pending() if operation == 'list' else client.get_pending(ID)
            transport.assert_called_once()

    def test_pagination_rejects_duplicate_order_boundary_and_cursor_errors(self):
        a = dict(ROW, id='response-a')
        b = dict(ROW, id='response-b')
        c = dict(ROW, id='response-c')
        cases = [
            ([a, a], None, None),
            ([a, dict(a, received_at='2026-10-11T00:00:00Z')], None, None),
            ([b, a], None, None),
            ([a, b], None, cursor_for(a)),
            ([a], None, cursor_for(b)),
            ([b], cursor_for(a), cursor_for(a)),
            ([b], cursor_for(c), cursor_for(a)),
            ([], cursor_for(a), None),
            ([], cursor_for(a), cursor_for(a)),
        ]
        for items, next_cursor, sent_cursor in cases:
            with self.subTest(items=items, next_cursor=next_cursor, sent_cursor=sent_cursor):
                transport = Mock(return_value=(200, encoded(
                    {'items': items, 'next_cursor': next_cursor})))
                with self.assertRaisesRegex(ResponseAdminError, '^malformed_response$'):
                    ResponseAdminClient(SECRET, transport=transport).list_pending(cursor=sent_cursor)
                transport.assert_called_once()

    def test_valid_pagination_uses_sqlite_binary_order_and_advances(self):
        ids = ['response-a-', 'response-a0', 'response-a_', 'response-aa']
        rows = [dict(ROW, id=ident) for ident in ids]
        transport = Mock(side_effect=[
            (200, encoded({'items': rows[:2], 'next_cursor': cursor_for(rows[1])})),
            (200, encoded({'items': rows[2:], 'next_cursor': None})),
            (200, encoded({'items': [], 'next_cursor': None})),
        ])
        client = ResponseAdminClient(SECRET, transport=transport)
        first = client.list_pending(limit=2)
        second = client.list_pending(limit=2, cursor=first['next_cursor'])
        self.assertEqual([r['id'] for r in first['items'] + second['items']], ids)
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(client.list_pending(cursor=cursor_for(rows[-1]))['items'], [])
        self.assertEqual(transport.call_count, 3)

    def test_safe_control_bidi_and_secret_display(self):
        text='\x1b[31m\x07\u202e<untrusted>'
        rendered=safe_display({'response':text})
        self.assertNotIn('\x1b',rendered);self.assertNotIn('\u202e',rendered)
        self.assertIn('\\u001b',rendered);self.assertIn('\\u202e',rendered)
        secret_value='password=synthetic-test-sensitive'
        rendered=safe_display({'response':secret_value,'agent_name':'Safe'})
        self.assertNotIn(secret_value,rendered)
        self.assertIn('[redacted: generic_credential_assignment]',rendered)

    def test_http_transport_is_get_without_redirect_or_retry(self):
        connection=Mock();connection.getresponse.return_value.status=200
        connection.getresponse.return_value.read.return_value=encoded({'items':[],'next_cursor':None})
        with patch('response_admin.http.client.HTTPSConnection',return_value=connection):
            ResponseAdminClient(SECRET).list_pending()
        connection.request.assert_called_once()
        self.assertEqual(connection.request.call_args.args[0],'GET')
        connection.getresponse.return_value.read.assert_called_once_with(MAX_BYTES+1)
        connection.close.assert_called_once()

class RemoteCLITests(unittest.TestCase):
    def invoke(self,args):
        with patch('sys.argv',['manage.py']+args):manage.main()

    def test_no_tty_stops_before_prompt_and_transport(self):
        with patch('sys.stdin.isatty',return_value=False),patch('response_admin.getpass.getpass') as prompt,patch('response_admin.ResponseAdminClient') as client:
            with self.assertRaisesRegex(ResponseAdminError,'terminal_required'):run_review()
        prompt.assert_not_called();client.assert_not_called()

    def test_warning_never_falls_back_to_echo(self):
        with patch('sys.stdin.isatty',return_value=True),patch('response_admin.getpass.getpass',side_effect=getpass.GetPassWarning()),patch('response_admin.ResponseAdminClient') as client:
            with self.assertRaisesRegex(ResponseAdminError,'secure_input'):run_review()
        client.assert_not_called()

    def test_remote_list_or_detail_no_files_generation_or_secret_output(self):
        for ident in [None,ID]:
            client=Mock();client.list_pending.return_value={'items':[ROW],'next_cursor':None}
            client.get_pending.return_value={'item':dict(DETAIL,response='password=synthetic-private'), 'status':'pending'}
            output=io.StringIO()
            with patch('sys.stdin.isatty',return_value=True),patch('response_admin.getpass.getpass',return_value=SECRET),patch('response_admin.ResponseAdminClient',return_value=client),patch('manage.FileStore') as files,patch('manage.crawl') as crawl,patch('manage.generate_journal_entry') as generate,contextlib.redirect_stdout(output):
                self.invoke(['review','response']+([ident] if ident else [])+['--remote'])
            files.assert_not_called();crawl.assert_not_called();generate.assert_not_called()
            self.assertNotIn(SECRET,output.getvalue());self.assertNotIn('synthetic-private',output.getvalue())
            client.close.assert_called_once()
            (client.get_pending if ident else client.list_pending).assert_called_once()

    def test_remote_flags_do_not_change_other_commands(self):
        for args in [['review','journal','--remote'],['review','response',ID],['review','response',ID,'--remote','--limit','2']]:
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit): self.invoke(args)

    def test_input_cancelled_without_request(self):
        with patch('sys.stdin.isatty',return_value=True),patch('response_admin.getpass.getpass',side_effect=KeyboardInterrupt),patch('response_admin.ResponseAdminClient') as client:
            with self.assertRaisesRegex(ResponseAdminError,'secure_input'):run_review()
        client.assert_not_called()

    def test_deep_json_cli_reports_only_fixed_error(self):
        output = io.StringIO()
        with patch('sys.stdin.isatty', return_value=True), \
                patch('response_admin.getpass.getpass', return_value=SECRET), \
                patch('response_admin.ResponseAdminClient._get', return_value=(200, b'[' * 1500 + b'0' + b']' * 1500)) as transport, \
                contextlib.redirect_stderr(output), self.assertRaises(SystemExit):
            self.invoke(['review', 'response', '--remote'])
        self.assertEqual(output.getvalue(), 'Remote review stopped: malformed_response\n')
        transport.assert_called_once()

class AdditionalAdminBoundaryTests(unittest.TestCase):
    def test_schema_rejects_extra_fields_wrong_ids_and_non_https_source(self):
        for item in [dict(DETAIL, id='different-id'), dict(DETAIL, extra='unknown'), dict(DETAIL, source_url='http://example.com/')]:
            client=ResponseAdminClient(SECRET,transport=lambda *args,row=item:(200,encoded({'item':row,'status':'pending'})))
            with self.assertRaises(ResponseAdminError): client.get_pending(ID)

    def test_invalid_secret_and_cursor_never_connect(self):
        for secret in ['', 'bad\nheader', 'x'*513, None]:
            with self.assertRaises(ResponseAdminError):ResponseAdminClient(secret)
        transport=Mock()
        client=ResponseAdminClient(SECRET,transport=transport)
        with self.assertRaises(ResponseAdminError):client.list_pending(cursor='x'*257)
        transport.assert_not_called()

    def test_remote_error_output_is_fixed_and_no_local_storage(self):
        output=io.StringIO()
        with patch('sys.argv',['manage.py','review','response','--remote']),patch('sys.stdin.isatty',return_value=True),patch('response_admin.getpass.getpass',return_value=SECRET),patch('response_admin.ResponseAdminClient') as constructor,patch('manage.FileStore') as files,contextlib.redirect_stderr(output):
            constructor.return_value.list_pending.side_effect=ResponseAdminError('timeout')
            with self.assertRaises(SystemExit):manage.main()
        self.assertEqual(output.getvalue(),'Remote review stopped: timeout\n')
        self.assertNotIn(SECRET,output.getvalue());files.assert_not_called()
        constructor.return_value.close.assert_called_once()

class FixedAdminSQLTests(unittest.TestCase):
    def test_worker_fixed_sql_on_in_memory_sqlite(self):
        import re
        import sqlite3
        from pathlib import Path
        source=(Path(__file__).resolve().parents[1]/'cloudflare/response-intake/src/admin.js').read_text()
        queries=[]
        for expression in re.findall(r'env\.DB\.prepare\(\s*(.*?)\s*\)\.bind',source,re.S):
            queries.append(''.join(json.loads(piece) for piece in re.findall(r'"[^"\n]*"',expression)))
        self.assertEqual(len(queries),3)
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE responses(id TEXT PRIMARY KEY,journal_id TEXT,received_at TEXT,agent_name TEXT,response TEXT,source_url TEXT,status TEXT)')
        for ident,status in [('response-a','pending'),('response-b','pending'),('response-c','published'),('response-d','rejected')]:
            db.execute('INSERT INTO responses VALUES(?,?,?,?,?,?,?)',(ident,'journal-test',ROW['received_at'],'Claimed','Text',None,status))
        detail_sql,cursor_sql,list_sql=queries
        self.assertEqual([row[0] for row in db.execute(list_sql,(3,))],['response-a','response-b'])
        self.assertEqual([row[0] for row in db.execute(cursor_sql,(ROW['received_at'],'response-a',3))],['response-b'])
        self.assertEqual(db.execute(detail_sql,('response-a',)).fetchone()[0],'response-a')
        self.assertIsNone(db.execute(detail_sql,('response-c',)).fetchone())
        self.assertIsNone(db.execute(detail_sql,('response-d',)).fetchone())
        ids = ['response-a-', 'response-a0', 'response-a_', 'response-aa']
        db.execute('DELETE FROM responses')
        for ident in reversed(ids):
            db.execute('INSERT INTO responses VALUES(?,?,?,?,?,?,?)',
                       (ident,'journal-test',ROW['received_at'],'Claimed','Text',None,'pending'))
        self.assertEqual([r[0] for r in db.execute(list_sql,(5,))], ids)
        self.assertEqual([r[0] for r in db.execute(cursor_sql,(ROW['received_at'],ids[1],3))], ids[2:])
