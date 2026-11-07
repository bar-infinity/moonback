import base64
import contextlib
import io
import json
import socket
import unittest
from unittest.mock import Mock, patch

import observation_admin as admin


def ident(n=1):
    return 'obs-' + format(n, '032x')


def item(n=1, time='2026-11-07T00:00:00Z'):
    return {'event_id': ident(n), 'time': time, 'method': 'GET', 'status': 200}


def detail():
    return {**item(), 'schema_version': 1, 'path': '/journal', 'ua': 'Example',
            'response_completed': True, 'agent_like': False}


class ObservationAdminTests(unittest.TestCase):
    def setUp(self):
        configured=patch.dict('os.environ',{admin.ADMIN_URL_ENV:admin.ADMIN_URL})
        configured.start();self.addCleanup(configured.stop)
        active=patch('socket.create_connection',side_effect=AssertionError('network prohibited'))
        active.start();self.addCleanup(active.stop)

    def client(self, data=None, status=200, raw=None):
        transport=Mock(return_value=(status,json.dumps(data).encode() if raw is None else raw))
        result=admin.ObservationAdminClient('read-test-only',transport=transport)
        self.addCleanup(result.close)
        return result,transport

    def test_summary_saved_count_range_and_empty_not_render_queue_counts(self):
        for total,first,last in [(0,None,None),(1,'2026-11-07T00:00:00.000000Z','2026-11-07T00:00:00.000000Z')]:
            data={'total':total,'first_time':first,'last_time':last}
            client,transport=self.client(data)
            self.assertEqual(client.summary(),data)
            self.assertEqual(transport.call_args.args[0],admin.ADMIN_PATH+'/summary')
            self.assertEqual(transport.call_args.args[1]['Authorization'],'Bearer read-test-only')
            transport.assert_called_once()
        client,_=self.client({'total':0,'first_time':None,'last_time':None,'pending':0})
        with self.assertRaises(admin.ObservationAdminError):client.summary()

    def test_list_default_and_cursor_one_get_only_no_details(self):
        cursor=admin.encode_cursor(['2026-11-07T00:00:00.000000Z',ident()])
        data={'items':[item()], 'next_cursor':cursor}
        client,transport=self.client(data)
        self.assertEqual(client.list_records(),data)
        self.assertEqual(transport.call_args.args[0],admin.ADMIN_PATH+'?limit=20')
        transport.assert_called_once()
        following={'items':[item(2,'2026-11-07T00:00:00.000001+00:00')],'next_cursor':None}
        client,transport=self.client(following)
        self.assertEqual(client.list_records(limit=1,cursor=cursor),following)
        self.assertEqual(transport.call_args.args[0],admin.ADMIN_PATH+'?limit=1&cursor='+cursor)
        self.assertNotIn('ua',following['items'][0])

    def test_explicit_detail_schema_id_and_no_hash(self):
        client,transport=self.client(detail())
        self.assertEqual(client.get_record(ident()),detail())
        self.assertEqual(transport.call_args.args[0],admin.ADMIN_PATH+'/'+ident())
        for data in [{**detail(),'event_id':ident(2)},{**detail(),'payload_hash':'not allowed'},
                     {**detail(),'response_completed':1},{**detail(),'ua':'x'*181}]:
            client,_=self.client(data)
            with self.assertRaises(admin.ObservationAdminError):client.get_record(ident())

    def test_invalid_input_rejected_before_transport(self):
        client,transport=self.client()
        for limit in (0,51,True,'20'):
            with self.assertRaises(admin.ObservationAdminError):client.list_records(limit=limit)
        for cursor in ('bad','x'*600,admin.encode_cursor(['2026-02-30T00:00:00.000000Z',ident()])):
            with self.assertRaises(admin.ObservationAdminError):client.list_records(cursor=cursor)
        for value in ('../x','x OR 1=1',ident()+'?x=1'):
            with self.assertRaises(admin.ObservationAdminError):client.get_record(value)
        transport.assert_not_called()

    def test_page_duplicate_unsorted_before_cursor_and_invalid_next_are_rejected(self):
        cursor=admin.encode_cursor(['2026-11-07T00:00:00.000000Z',ident()])
        bad=[{'items':[item(),item()],'next_cursor':None},
             {'items':[item(2),item(1)],'next_cursor':None},
             {'items':[],'next_cursor':cursor},
             {'items':[item(2)],'next_cursor':cursor},
             {'items':[item()],'next_cursor':'bad'}]
        for data in bad:
            client,_=self.client(data)
            with self.assertRaises(admin.ObservationAdminError):client.list_records()
        client,_=self.client({'items':[item()],'next_cursor':None})
        with self.assertRaises(admin.ObservationAdminError):client.list_records(cursor=cursor)

    def test_summary_invalid_types_dates_order_fail_closed(self):
        good={'total':1,'first_time':'2026-11-07T00:00:00.000000Z','last_time':'2026-11-07T00:00:00.000000Z'}
        for update in [{'total':True},{'total':-1},{'total':0},{'first_time':'2026-02-30T00:00:00.000000Z'},
                       {'first_time':'2026-11-08T00:00:00.000000Z'}]:
            client,_=self.client({**good,**update})
            with self.assertRaises(admin.ObservationAdminError):client.summary()

    def test_redirect_auth_rate_storage_and_unexpected_http_fixed_categories(self):
        for status,category in [(302,'redirect_refused'),(404,'not_found_or_unauthorized'),
                                (429,'rate_limited'),(503,'storage_unavailable'),(500,'provider_error')]:
            client,transport=self.client(raw=b'private body read-test-only',status=status)
            with self.assertRaises(admin.ObservationAdminError) as caught:client.summary()
            self.assertEqual(str(caught.exception),category)
            transport.assert_called_once()

    def test_timeout_connection_exception_details_are_not_exposed_or_retried(self):
        for exception,category in [(TimeoutError('read-test-only'),'timeout'),(OSError('private provider error'),'connection')]:
            client,transport=self.client();transport.side_effect=exception
            with self.assertRaises(admin.ObservationAdminError) as caught:client.summary()
            self.assertEqual(str(caught.exception),category);transport.assert_called_once()

    def test_size_duplicate_keys_utf8_and_deep_json_rejected(self):
        for raw in (b'x'*65537,b'\xff',b'{"total":0,"total":1}', b'['*3000+b']'*3000):
            client,_=self.client(raw=raw)
            with self.assertRaises(admin.ObservationAdminError) as caught:client.summary()
            self.assertIn(str(caught.exception),('malformed_response','response_too_large'))

    def test_only_pinned_https_host_allowed_and_close_forgets_secret(self):
        for url in ('http://'+admin.TRUSTED_HOST+admin.ADMIN_PATH,'https://evil.example'+admin.ADMIN_PATH,
                    admin.ADMIN_URL+'?x=1',admin.ADMIN_URL+'/',admin.ADMIN_URL.replace('https://','https://user@')):
            with self.assertRaises(admin.ObservationAdminError):admin.ObservationAdminClient('read-test-only',url=url)
        for secret in ('','a\r\nHeader: x','x'*513):
            with self.assertRaises(admin.ObservationAdminError):admin.ObservationAdminClient(secret)
        client,transport=self.client();client.close()
        with self.assertRaises(admin.ObservationAdminError):client.summary()
        transport.assert_not_called()

    def test_real_get_transport_uses_get_tls_timeout10_bounded_read_no_redirect(self):
        with patch('observation_admin.http.client.HTTPSConnection') as factory:
            connection=factory.return_value
            connection.getresponse.return_value.status=302
            connection.getresponse.return_value.read.return_value=b'private'
            client=admin.ObservationAdminClient('read-test-only')
            with self.assertRaises(admin.ObservationAdminError):client.summary()
            self.assertEqual(factory.call_args.args[0],admin.TRUSTED_HOST)
            self.assertEqual(factory.call_args.kwargs['timeout'],10)
            connection.request.assert_called_once()
            self.assertEqual(connection.request.call_args.args[:2],('GET',admin.ADMIN_PATH+'/summary'))
            connection.getresponse.return_value.read.assert_called_once_with(65537)
            connection.close.assert_called_once()

    def test_escape_controls_bidi_and_mask_credentials_without_guessing_identity(self):
        raw='hello\x1b[2J\n\u202e\x00'
        shown=admin.safe_text(raw)
        for value in ('\x1b','\n','\u202e','\x00'):self.assertNotIn(value,shown)
        self.assertIn('\\u001b',shown);self.assertIn('\\u202e',shown)
        secret='Authorization: Bearer synthetic-credential-12345'
        self.assertEqual(admin.safe_text(secret),'[redacted:credential]')
        self.assertEqual(admin.safe_text('prefix read-test-only suffix','read-test-only'),'[redacted:credential]')
        self.assertEqual(admin.safe_text('Public hello@example.com'),'Public hello@example.com')

    def test_cli_no_tty_and_invalid_args_do_not_prompt_or_connect(self):
        with patch('observation_admin.sys.stdin.isatty',return_value=False),patch('observation_admin.getpass.getpass') as prompt, \
                patch('observation_admin.ObservationAdminClient') as client,contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(admin.main(['summary']),1)
            self.assertEqual(admin.main(['show','../bad']),1)
            self.assertEqual(admin.main(['list','--limit','51']),1)
            prompt.assert_not_called();client.assert_not_called()

    def test_cli_summary_list_detail_only_on_explicit_request_and_no_secret_output(self):
        for args in (['summary'],['list'],['show',ident()]):
            out,err=io.StringIO(),io.StringIO()
            with patch('observation_admin.sys.stdin.isatty',return_value=True), \
                    patch('observation_admin.getpass.getpass',return_value='read-test-only'), \
                    patch('observation_admin.ObservationAdminClient') as factory, \
                    contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
                client=factory.return_value
                client.summary.return_value={'total':1,'first_time':'2026-11-07T00:00:00.000000Z','last_time':'2026-11-07T00:00:00.000000Z'}
                client.list_records.return_value={'items':[item()],'next_cursor':None}
                client.get_record.return_value={**detail(),'ua':'Authorization: Bearer synthetic-credential-12345'}
                self.assertEqual(admin.main(args),0)
                self.assertNotIn('read-test-only',out.getvalue()+err.getvalue())
                self.assertNotIn('synthetic-credential',out.getvalue()+err.getvalue())
                if args[0]!='show':client.get_record.assert_not_called()
                else:self.assertIn('[redacted:credential]',out.getvalue())
                client.close.assert_called_once()

    def test_getpass_fallback_warning_is_safe_failure_no_client(self):
        with patch('observation_admin.sys.stdin.isatty',return_value=True), \
                patch('observation_admin.getpass.getpass',side_effect=admin.getpass.GetPassWarning()), \
                patch('observation_admin.ObservationAdminClient') as factory,contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(admin.main(['summary']),1);factory.assert_not_called()

    def test_new_private_code_and_history_routes_are_not_exposed_by_render(self):
        import tempfile
        from pathlib import Path
        import server
        from test_v03 import FakeSocket
        with tempfile.TemporaryDirectory() as tmp,patch.object(server,'LOG',Path(tmp)/'requests.jsonl'), \
                patch.object(server.Handler,'log_message'):
            for method in ('GET','HEAD'):
                for path in ('/observation_admin.py','/cloudflare/observation-archive/src/admin.js',
                             '/v1/admin/observations','/api/observations'):
                    sock=FakeSocket((method+' '+path+' HTTP/1.0\r\n\r\n').encode())
                    server.Handler(sock,('127.0.0.1',1),None)
                    self.assertEqual(int(sock.output.getvalue().split()[1]),404)

    def test_private_read_client_never_calls_generation_or_crawler(self):
        with patch('journal_openai.OpenAIJournalAdapter') as adapter,patch('crawler.crawl') as crawler:
            client,_=self.client({'total':0,'first_time':None,'last_time':None})
            self.assertEqual(client.summary()['total'],0)
            adapter.assert_not_called();crawler.assert_not_called()

    def test_missing_or_unapproved_configuration_stops_before_secret_prompt_and_transport(self):
        for value in (None,'', 'http://'+admin.TRUSTED_HOST+admin.ADMIN_PATH,
                      'https://evil.example'+admin.ADMIN_PATH,
                      'https://'+admin.TRUSTED_HOST+'.evil.example'+admin.ADMIN_PATH,
                      'https://other-worker.london-1976.workers.dev'+admin.ADMIN_PATH,
                      admin.ADMIN_URL.replace('https://','https://user:password@'),
                      admin.ADMIN_URL+'?x=1',admin.ADMIN_URL+'#fragment',
                      admin.ADMIN_URL+'/',admin.ADMIN_URL.replace(admin.ADMIN_PATH,'/%76' + admin.ADMIN_PATH[2:]),
                      admin.ADMIN_URL.replace('.dev/','.dev:443/'),
                      admin.ADMIN_URL.replace('.dev/','.dev:invalid/'),
                      admin.ADMIN_URL+'\n'):
            values={} if value is None else {admin.ADMIN_URL_ENV:value}
            with self.subTest(value=value),patch.dict('os.environ',values,clear=True), \
                    patch('observation_admin.getpass.getpass') as prompt, \
                    patch('observation_admin.ObservationAdminClient') as client, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(admin.main(['summary']),1)
                prompt.assert_not_called();client.assert_not_called()
                self.assertIn('configuration',err.getvalue())
                self.assertNotIn('password',err.getvalue())

    def test_client_requires_explicit_approved_configuration_and_sends_only_to_selected_host(self):
        with patch.dict('os.environ',{},clear=True),patch('observation_admin.http.client.HTTPSConnection') as factory:
            with self.assertRaises(admin.ObservationAdminError):
                admin.ObservationAdminClient('read-test-only')
            factory.assert_not_called()
        self.assertEqual(admin.approved_admin_url(admin.ADMIN_URL),admin.ADMIN_URL)
        with patch.dict('os.environ',{},clear=True):
            client=admin.ObservationAdminClient('read-test-only',url=admin.ADMIN_URL,transport=Mock())
            self.assertEqual(client._host,admin.TRUSTED_HOST)
            client.close()
