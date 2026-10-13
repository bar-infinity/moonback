import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/sync_response_status.py'
WORKFLOW = ROOT / '.github/workflows/sync-response-status.yml'
spec = importlib.util.spec_from_file_location('sync_response_status', SCRIPT)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)
EMPTY = {'pending': False, 'cursor': None}
PENDING = {'pending': True, 'cursor': 'sha256:' + 'a' * 64}


class StatusSyncTests(unittest.TestCase):
    def setUp(self):
        guard = patch('socket.create_connection', side_effect=AssertionError('network forbidden'))
        guard.start(); self.addCleanup(guard.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.destination = self.directory / 'status/response-status.json'

    def save(self, value):
        self.destination.parent.mkdir(exist_ok=True)
        self.destination.write_text(json.dumps(value))

    def test_initial_file_only_after_valid_success(self):
        transport = Mock(return_value=json.dumps(EMPTY).encode())
        self.assertTrue(sync.sync(destination=self.destination, transport=transport))
        transport.assert_called_once()
        self.assertEqual(json.loads(self.destination.read_text()), EMPTY)
        self.assertEqual(set(json.loads(self.destination.read_text())), {'pending', 'cursor'})

    def test_changed_signal_and_false_with_historical_cursor(self):
        self.save(EMPTY)
        for value in [PENDING, {'pending': False, 'cursor': PENDING['cursor']}, EMPTY]:
            self.assertTrue(sync.sync(destination=self.destination, transport=lambda v=value: json.dumps(v).encode()))
            self.assertEqual(json.loads(self.destination.read_text()), value)

    def test_unchanged_semantics_do_not_write_or_add_timestamp(self):
        self.save(PENDING)
        before = self.destination.read_bytes()
        stamp = self.destination.stat().st_mtime_ns
        with patch.object(sync.os, 'replace') as replace:
            self.assertFalse(sync.sync(destination=self.destination, transport=lambda: json.dumps(PENDING, indent=4).encode()))
        replace.assert_not_called()
        self.assertEqual(self.destination.read_bytes(), before)
        self.assertEqual(self.destination.stat().st_mtime_ns, stamp)

    def test_all_invalid_payloads_preserve_previous_snapshot(self):
        self.save(PENDING); before = self.destination.read_bytes()
        cases = [b'not JSON', b'\xff', b'x' * 4097, b'[' * 1500 + b'0' + b']' * 1500,
                 b'{"pending":false,"pending":true,"cursor":null}',
                 b'{"pending":false,"cursor":NaN}']
        invalid = [[], {}, {'pending': 0, 'cursor': None}, {'pending': 'false', 'cursor': None},
                   {'pending': True, 'cursor': None}, {'pending': False, 'cursor': 'internal-id'},
                   {'pending': False, 'cursor': 'sha256:' + 'A' * 64},
                   dict(EMPTY, agent_name='do not save'), dict(EMPTY, response='$(touch /tmp/unsafe)'),
                   dict(EMPTY, id='internal-id'), dict(EMPTY, received_at='do not save')]
        cases += [json.dumps(value).encode() for value in invalid]
        for body in cases:
            with self.subTest(size=len(body)), self.assertRaises(sync.SyncError):
                sync.sync(destination=self.destination, transport=lambda b=body: b)
            self.assertEqual(self.destination.read_bytes(), before)
        self.assertEqual(list(self.destination.parent.glob('.response-status-*')), [])

    def test_fetch_failure_never_creates_initial_false_file(self):
        with self.assertRaises(TimeoutError):
            sync.sync(destination=self.destination, transport=Mock(side_effect=TimeoutError('private data')))
        self.assertFalse(self.destination.exists())
        self.save(PENDING); before = self.destination.read_bytes()
        with self.assertRaises(TimeoutError):
            sync.sync(destination=self.destination, transport=Mock(side_effect=TimeoutError('private data')))
        self.assertEqual(self.destination.read_bytes(), before)

    def test_atomic_write_failure_preserves_old_file_and_removes_temporary(self):
        self.save(PENDING); before = self.destination.read_bytes()
        with patch.object(sync.os, 'replace', side_effect=OSError('private data')):
            with self.assertRaisesRegex(sync.SyncError, '^storage_error$'):
                sync.sync(destination=self.destination, transport=lambda: json.dumps(EMPTY).encode())
        self.assertEqual(self.destination.read_bytes(), before)
        self.assertEqual(list(self.destination.parent.glob('.response-status-*')), [])

    def test_symlink_and_invalid_existing_snapshot_fail_safely(self):
        self.destination.parent.mkdir(); other = self.directory / 'other.json'
        other.write_text('unchanged'); self.destination.symlink_to(other)
        with self.assertRaisesRegex(sync.SyncError, '^unsafe_destination$'):
            sync.sync(destination=self.destination, transport=lambda: json.dumps(EMPTY).encode())
        self.assertEqual(other.read_text(), 'unchanged')
        self.destination.unlink(); self.destination.write_text('invalid')
        with self.assertRaisesRegex(sync.SyncError, '^invalid_existing_snapshot$'):
            sync.sync(destination=self.destination, transport=lambda: json.dumps(EMPTY).encode())
        self.assertEqual(self.destination.read_text(), 'invalid')

    def test_http_request_has_fixed_get_no_authorization_and_bounded_read(self):
        connection = Mock(); response = connection.getresponse.return_value
        response.status = 200; response.getheader.return_value = 'application/json; charset=utf-8'
        response.read.return_value = json.dumps(EMPTY).encode()
        with patch.object(sync.http.client, 'HTTPSConnection', return_value=connection) as factory:
            self.assertEqual(sync.decode(sync.public_get()), EMPTY)
        self.assertEqual(factory.call_args.args, (sync.HOST,))
        self.assertEqual(factory.call_args.kwargs['timeout'], 15)
        self.assertEqual(connection.request.call_args.args, ('GET', sync.PATH))
        self.assertEqual(set(connection.request.call_args.kwargs['headers']), {'Accept', 'User-Agent'})
        connection.request.assert_called_once(); response.read.assert_called_once_with(4097)
        connection.close.assert_called_once()

    def test_http_errors_redirects_and_timeouts_have_no_retry_or_echo(self):
        for status in [201, 302, 307, 404, 429, 503]:
            connection = Mock(); response = connection.getresponse.return_value; response.status = status
            with patch.object(sync.http.client, 'HTTPSConnection', return_value=connection), self.assertRaises(sync.SyncError):
                sync.public_get()
            response.read.assert_not_called(); connection.request.assert_called_once()
        connection = Mock(); connection.request.side_effect = TimeoutError('private-provider-details')
        with patch.object(sync.http.client, 'HTTPSConnection', return_value=connection):
            with self.assertRaisesRegex(sync.SyncError, '^connection_or_timeout$'): sync.public_get()
        connection.request.assert_called_once()

    def test_wrong_content_type_is_rejected_without_body(self):
        connection = Mock(); response = connection.getresponse.return_value
        response.status = 200; response.getheader.return_value = 'text/html'
        with patch.object(sync.http.client, 'HTTPSConnection', return_value=connection):
            with self.assertRaisesRegex(sync.SyncError, '^invalid_content_type$'): sync.public_get()
        response.read.assert_not_called()

    def test_cli_without_live_is_offline(self):
        result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('explicit_live_required', result.stdout)
        self.assertEqual(result.stderr, '')

    def test_cli_failure_displays_only_fixed_category(self):
        for error, expected in [(sync.SyncError('malformed_json'), 'malformed_json'),
                                (RuntimeError('private-provider-body'), 'internal_error')]:
            output = io.StringIO()
            with patch.object(sync.sys, 'argv', ['sync_response_status.py', '--live']), \
                    patch.object(sync, 'sync', side_effect=error), contextlib.redirect_stdout(output):
                self.assertEqual(sync.main(), 1)
            self.assertEqual(output.getvalue(), 'Status sync failed: ' + expected + '\n')


class WorkflowSafetyTests(unittest.TestCase):
    def test_workflow_permissions_triggers_pin_and_concurrency(self):
        text = WORKFLOW.read_text()
        self.assertIn("cron: '17 * * * *'", text)
        self.assertIn('workflow_dispatch:', text)
        self.assertIn('permissions: {}', text)
        self.assertEqual(text.count('contents: write'), 1)
        self.assertIn('cancel-in-progress: false', text)
        self.assertIn("github.repository == 'bar-infinity/moonback'", text)
        self.assertIn("github.ref == 'refs/heads/main'", text)
        self.assertRegex(text, r'actions/checkout@[0-9a-f]{40}')
        self.assertNotIn('pull_request', text)
        self.assertNotIn('secrets.', text)
        self.assertNotIn('--force', text)

    def test_actual_workflow_shell_commits_one_path_and_skips_unchanged(self):
        # Execute the actual workflow shell with a fake git; no Git repo/push is used.
        block = WORKFLOW.read_text().split('        run: |\n', 1)[1]
        shell = '\n'.join(line[10:] for line in block.splitlines())
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder); log = base / 'git-calls.jsonl'; fake = base / 'git'
            fake.write_text('#!/usr/bin/env python3\nimport os,sys,json\n'
                            'a=sys.argv[1:]\n'
                            'with open(os.environ["FAKE_GIT_LOG"],"a") as f:f.write(json.dumps(a)+"\\n")\n'
                            'if a==["diff","--cached","--quiet"]:sys.exit(int(os.environ["FAKE_CHANGED"]))\n'
                            'if a==["diff","--cached","--name-only"]:print(os.environ["FAKE_STAGED"])\n'
                            'if a[:1]==["push"]:sys.exit(int(os.environ.get("FAKE_PUSH_FAIL","0")))\n')
            fake.chmod(0o700)
            for changed, path, failure, expected_code, pushes in [
                    ('0', 'moonback_mvp/status/response-status.json', '0', 0, 0),
                    ('1', 'moonback_mvp/status/response-status.json', '0', 0, 1),
                    ('1', 'unexpected-file', '0', 1, 0),
                    ('1', 'moonback_mvp/status/response-status.json', '1', 1, 1)]:
                if log.exists(): log.unlink()
                env = dict(os.environ, PATH=str(base)+os.pathsep+os.environ['PATH'],
                           FAKE_GIT_LOG=str(log), FAKE_CHANGED=changed, FAKE_STAGED=path, FAKE_PUSH_FAIL=failure)
                result = subprocess.run(['bash', '-c', shell], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, expected_code, result.stderr)
                calls = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertEqual(sum(a[:1]==['push'] for a in calls), pushes)
                self.assertEqual(calls[0], ['add', '--', 'moonback_mvp/status/response-status.json'])
                commits = [a for a in calls if 'commit' in a]
                if pushes:
                    self.assertEqual(len(commits), 1)
                    self.assertEqual(commits[0][-1], 'Sync response status [skip render]')
                    self.assertEqual(calls[-1], ['push', 'origin', 'HEAD:main'])
                else:
                    self.assertEqual(commits, [])
