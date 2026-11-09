import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from test_response_status_sync import sync, EMPTY, PENDING, WORKFLOW
BASE = datetime(2026, 11, 9, tzinfo=timezone.utc)

def stamp(v):
    return v.isoformat(timespec='microseconds').replace('+00:00', 'Z')

def success(signal=PENDING, at=BASE):
    return {'schema_version': 1, 'last_attempt_at': stamp(at), 'last_success_at': stamp(at), 'result': 'success', 'error_category': None, **signal}

class ResponseFreshnessTests(unittest.TestCase):

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name).resolve()
        self.destination = self.folder / 'status/response-status.json'
        self.monitor = self.folder / 'status/response-monitor.json'
        guard = patch('socket.create_connection', side_effect=AssertionError('network prohibited'))
        guard.start()
        self.addCleanup(guard.stop)

    def run_sync(self, signal=EMPTY, at=BASE, transport=None):
        transport = transport or Mock(return_value=json.dumps(signal).encode())
        changed = sync.sync_monitored(destination=self.destination, transport=transport, clock=lambda : at)
        transport.assert_called_once()
        return changed

    def value(self):
        return json.loads(self.monitor.read_text())

    def test_initial_seed_never_claims_observation(self):
        actual = sync.decode_monitor(sync.MONITOR_DESTINATION.read_bytes())
        self.assertEqual(actual, sync.never_checked())
        self.assertIsNone(actual['pending'])
        self.assertEqual(sync.monitor_state(actual, now=BASE), 'unknown')

    def test_first_success_creates_coherent_pair_and_allowlisted_fields(self):
        self.assertTrue(self.run_sync())
        self.assertEqual(json.loads(self.destination.read_text()), EMPTY)
        self.assertEqual(self.value(), success(EMPTY))
        self.assertEqual(set(self.value()), sync.MONITOR_FIELDS)
        sync.validate_publication(destination=self.destination, monitor=self.monitor, now=BASE)

    def test_unchanged_success_refreshes_monitor_only(self):
        self.run_sync(PENDING)
        old = self.destination.read_bytes()
        mtime = self.destination.stat().st_mtime_ns
        self.assertFalse(self.run_sync(PENDING, BASE + timedelta(hours=1)))
        self.assertEqual(self.destination.read_bytes(), old)
        self.assertEqual(self.destination.stat().st_mtime_ns, mtime)
        self.assertEqual(self.value(), success(PENDING, BASE + timedelta(hours=1)))

    def test_changed_success_then_failure_retains_proven_signal_and_time(self):
        self.run_sync(EMPTY)
        self.run_sync(PENDING, BASE + timedelta(minutes=1))
        old = self.destination.read_bytes()
        transport = Mock(side_effect=sync.SyncError('http_error'))
        with self.assertRaisesRegex(sync.SyncError, '^http_error$'):
            self.run_sync(at=BASE + timedelta(minutes=2), transport=transport)
        transport.assert_called_once()
        v = self.value()
        self.assertEqual(v['result'], 'failure')
        self.assertEqual(v['last_success_at'], stamp(BASE + timedelta(minutes=1)))
        self.assertEqual(v['last_attempt_at'], stamp(BASE + timedelta(minutes=2)))
        self.assertTrue(v['pending'])
        self.assertEqual(v['cursor'], PENDING['cursor'])
        self.assertEqual(self.destination.read_bytes(), old)
        sync.validate_publication(destination=self.destination, monitor=self.monitor, now=BASE + timedelta(minutes=2))

    def test_initial_failure_is_null_without_false_snapshot(self):
        with self.assertRaises(sync.SyncError):
            self.run_sync(transport=Mock(side_effect=TimeoutError('private')))
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.value(), {**sync.never_checked(), 'last_attempt_at': stamp(BASE), 'result': 'failure', 'error_category': 'connection_or_timeout'})
        sync.validate_publication(destination=self.destination, monitor=self.monitor, now=BASE)

    def test_legacy_does_not_supply_an_invented_success_time(self):
        self.destination.parent.mkdir()
        self.destination.write_text(json.dumps(EMPTY))
        with self.assertRaises(sync.SyncError):
            self.run_sync(transport=Mock(side_effect=RuntimeError('private')))
        self.assertIsNone(self.value()['pending'])
        self.assertIsNone(self.value()['last_success_at'])
        self.assertEqual(json.loads(self.destination.read_text()), EMPTY)

    def test_invalid_source_or_private_extra_fields_only_produce_fixed_failure(self):
        self.run_sync(PENDING)
        for raw in (b'not json', json.dumps({**EMPTY, 'agent_name': 'private value'}).encode(), b'{"pending":false,"pending":true,"cursor":null}'):
            with self.assertRaises(sync.SyncError):
                self.run_sync(at=BASE + timedelta(minutes=1), transport=Mock(return_value=raw))
            self.assertTrue(self.value()['pending'])
            self.assertNotIn('private value', self.monitor.read_text())
            self.assertEqual(set(self.value()), sync.MONITOR_FIELDS)

    def test_raw_provider_error_is_not_a_public_category(self):
        for error in (RuntimeError('secret provider text'), sync.SyncError('secret provider text')):
            with self.assertRaisesRegex(sync.SyncError, '^internal_error$'):
                self.run_sync(transport=Mock(side_effect=error))
            self.assertNotIn('secret provider text', self.monitor.read_text())

    def test_invalid_monitor_json_types_and_time_contradictions_are_unknown(self):
        for raw in (b'{', b'\xff', b'{"schema_version":1,"schema_version":2}', b'[' * 2000 + b']' * 2000):
            with self.assertRaises(sync.SyncError):
                sync.decode_monitor(raw)
        bad = [{**success(), 'schema_version': True}, {**success(), 'pending': None}, {**success(), 'last_success_at': None}, {**success(), 'last_attempt_at': '2026-02-30T00:00:00Z'}, {**success(), 'last_attempt_at': '2026-11-09T09:00:00+09:00'}, {**success(), 'last_success_at': stamp(BASE - timedelta(seconds=1))}, {**success(), 'error_category': 'http_error'}, {**success(), 'agent_name': 'private'}, {**sync.never_checked(), 'pending': False}, {**success(), 'result': 'failure', 'error_category': 'http_error', 'last_attempt_at': stamp(BASE - timedelta(seconds=1))}]
        for v in bad:
            self.assertEqual(sync.monitor_state(v, now=BASE), 'unknown')
        self.assertEqual(sync.monitor_state(success(at=BASE + timedelta(microseconds=1)), now=BASE), 'unknown')

    def test_three_hour_boundary_and_failure_priority(self):
        self.assertEqual(sync.monitor_state(success(), now=BASE + timedelta(hours=3)), 'healthy')
        self.assertEqual(sync.monitor_state(success(), now=BASE + timedelta(hours=3, microseconds=1)), 'stale')
        self.assertEqual(sync.monitor_state({**success(), 'result': 'failure', 'error_category': 'http_error'}, now=BASE), 'unknown')

    def test_notification_decisions_do_not_mutate_or_persist_history(self):
        known = set()
        self.assertEqual(sync.notification_decision(success(), now=BASE), 'initial_pending')
        self.assertEqual(sync.notification_decision(success(), now=BASE, notified_cursors=known), 'new_candidate')
        self.assertEqual(known, set())
        known.add(PENDING['cursor'])
        self.assertEqual(sync.notification_decision(success(), now=BASE, notified_cursors=known), 'already_notified')
        self.assertEqual(sync.notification_decision(success(EMPTY), now=BASE, notified_cursors=known), 'no_pending_at_last_success')
        self.assertEqual(known, {PENDING['cursor']})
        self.assertEqual(sync.notification_decision(success(), now=BASE + timedelta(hours=4), notified_cursors=set()), 'stale')
        self.assertEqual(sync.notification_decision(success(), now=BASE, notified_cursors={'raw-id'}), 'unknown')

    def test_publication_blocks_partial_pair_and_unattempted_seed(self):
        self.monitor.parent.mkdir()
        self.monitor.write_text(json.dumps(sync.never_checked()))
        with self.assertRaises(sync.SyncError):
            sync.validate_publication(destination=self.destination, monitor=self.monitor, now=BASE)
        self.monitor.write_text(json.dumps(success()))
        self.destination.write_text(json.dumps(EMPTY))
        with self.assertRaisesRegex(sync.SyncError, '^publication_mismatch$'):
            sync.validate_publication(destination=self.destination, monitor=self.monitor, now=BASE)

    def test_clock_rollback_records_failure_not_success(self):
        with self.assertRaisesRegex(sync.SyncError, '^invalid_timestamp$'):
            sync.sync_monitored(destination=self.destination, transport=lambda : json.dumps(EMPTY).encode(), clock=Mock(side_effect=[BASE, BASE - timedelta(seconds=1)]))
        self.assertEqual(self.value()['result'], 'failure')
        self.assertIsNone(self.value()['last_success_at'])
        self.assertFalse(self.destination.exists())

    def test_corrupt_previous_monitor_is_not_overwritten_or_used(self):
        self.monitor.parent.mkdir()
        self.monitor.write_text('invalid')
        transport = Mock()
        with self.assertRaises(sync.SyncError):
            self.run_sync(transport=transport)
        transport.assert_not_called()
        self.assertEqual(self.monitor.read_text(), 'invalid')
        with self.assertRaises(sync.SyncError):
            sync.validate_publication(destination=self.destination, monitor=self.monitor, now=BASE)

    def test_monitor_storage_failure_blocks_partial_pair_publication(self):
        self.run_sync(EMPTY)
        replace = sync.os.replace

        def fail(src, dst):
            if Path(dst) == self.monitor:
                raise OSError('private')
            return replace(src, dst)
        with patch.object(sync.os, 'replace', side_effect=fail), self.assertRaises(sync.SyncError):
            self.run_sync(PENDING, BASE + timedelta(minutes=1))
        with self.assertRaisesRegex(sync.SyncError, '^publication_mismatch$'):
            sync.validate_publication(destination=self.destination, monitor=self.monitor, now=BASE + timedelta(minutes=1))
        self.assertEqual(list(self.monitor.parent.glob('.response-monitor-*')), [])

    def test_symlink_monitor_is_never_followed(self):
        target = self.folder / 'target'
        target.write_text('private')
        self.monitor.parent.mkdir()
        self.monitor.symlink_to(target)
        with self.assertRaisesRegex(sync.SyncError, '^unsafe_destination$'):
            self.run_sync()
        self.assertEqual(target.read_text(), 'private')

    def test_main_records_fetch_failure_and_returns_nonzero(self):
        original = sync.sync_monitored
        output = io.StringIO()
        with patch.object(sync, 'sync_monitored', side_effect=lambda : original(destination=self.destination, transport=Mock(side_effect=TimeoutError('private')), clock=lambda : BASE)), patch.object(sync.sys, 'argv', ['sync_response_status.py', '--live']), contextlib.redirect_stdout(output):
            self.assertEqual(sync.main(), 1)
        self.assertEqual(self.value()['result'], 'failure')
        self.assertEqual(output.getvalue(), 'Status sync failed: connection_or_timeout\n')

    def test_publication_cli_is_offline(self):
        with patch.object(sync, 'validate_publication') as validate, patch.object(sync, 'public_get') as get, patch.object(sync.sys, 'argv', ['sync_response_status.py', '--validate-publication']), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sync.main(), 0)
            validate.assert_called_once()
            get.assert_not_called()

class FreshnessWorkflowTests(unittest.TestCase):

    def test_failed_read_can_publish_but_workflow_cannot_finish_success(self):
        text = WORKFLOW.read_text()
        self.assertIn('id: read_signal\n        continue-on-error: true', text)
        self.assertIn("if: ${{ !cancelled() && steps.validate_publication.outcome == 'success' }}", text)
        self.assertIn("if: ${{ !cancelled() && steps.read_signal.outcome != 'success' }}\n        run: exit 1", text)
        self.assertIn('run: python3 scripts/sync_response_status.py --validate-publication', text)
        self.assertLess(text.index('Validate publication'), text.index('Commit only changed'))
        self.assertLess(text.index('git push origin HEAD:main'), text.index('Preserve failed sync conclusion'))
        self.assertEqual(subprocess.run(['bash', '-c', 'exit 1']).returncode, 1)

    def test_failed_fetch_publication_then_failed_workflow_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            destination = base / 'moonback_mvp/status/response-status.json'
            monitor = destination.with_name('response-monitor.json')
            original = sync.sync_monitored
            fetch = Mock(side_effect=TimeoutError('untrusted diagnostic'))
            output = io.StringIO()
            with patch.object(sync, 'sync_monitored', side_effect=lambda : original(destination=destination, transport=fetch, clock=lambda : BASE)), patch.object(sync.sys, 'argv', ['sync_response_status.py', '--live']), contextlib.redirect_stdout(output):
                read_exit = sync.main()
            self.assertEqual(read_exit, 1)
            fetch.assert_called_once()
            record = sync.validate_publication(destination=destination, monitor=monitor, now=BASE)
            self.assertEqual(record['result'], 'failure')
            self.assertIsNone(record['pending'])
            self.assertFalse(destination.exists())
            self.assertNotIn('untrusted diagnostic', output.getvalue() + monitor.read_text())
            log = base / 'calls.jsonl'
            fake_git = base / 'git'
            fake_git.write_text('#!/usr/bin/env python3\nimport os,sys,json\na=sys.argv[1:]\nwith open(os.environ["CALL_LOG"],"a") as f:f.write(json.dumps(a)+"\\n")\nif a==["diff","--cached","--quiet"]:sys.exit(1)\nif a==["diff","--cached","--name-only"]:print("moonback_mvp/status/response-monitor.json")\n')
            fake_git.chmod(448)
            text = WORKFLOW.read_text().split('        run: |\n', 1)[1]
            shell = '\n'.join((line[10:] for line in text.splitlines() if line.startswith('          ')))
            env = dict(os.environ, PATH=str(base) + os.pathsep + os.environ['PATH'], CALL_LOG=str(log))
            published = subprocess.run(['bash', '-c', shell], cwd=base, env=env, capture_output=True)
            self.assertEqual(published.returncode, 0)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(calls[-1], ['push', 'origin', 'HEAD:main'])
            self.assertTrue(any(('commit' in call and '[skip render]' in call[-1] for call in calls)))
            final = subprocess.run(['bash', '-c', 'exit 1' if read_exit else 'exit 0'])
            self.assertEqual(final.returncode, 1)

    def test_actual_shell_two_path_allowlist_and_commit_push_failures(self):
        text = WORKFLOW.read_text().split('        run: |\n', 1)[1]
        shell = '\n'.join((line[10:] for line in text.splitlines() if line.startswith('          ')))
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / 'moonback_mvp/status').mkdir(parents=True)
            (base / 'moonback_mvp/status/response-monitor.json').write_text('{}')
            log = base / 'calls.jsonl'
            git = base / 'git'
            git.write_text('#!/usr/bin/env python3\nimport os,sys,json\na=sys.argv[1:]\nwith open(os.environ["CALL_LOG"],"a") as f:f.write(json.dumps(a)+"\\n")\nif a==["diff","--cached","--quiet"]:sys.exit(1)\nif a==["diff","--cached","--name-only"]:print(os.environ["STAGED"])\nif "commit" in a:sys.exit(int(os.environ.get("COMMIT_FAIL","0")))\nif a[:1]==["push"]:sys.exit(int(os.environ.get("PUSH_FAIL","0")))\n')
            git.chmod(448)
            monitor = 'moonback_mvp/status/response-monitor.json'
            legacy = 'moonback_mvp/status/response-status.json'
            for (staged, c, p, code, pushes) in [(monitor, 0, 0, 0, 1), (monitor + '\n' + legacy, 0, 0, 0, 1), (monitor + '\nunexpected', 0, 0, 1, 0), (monitor, 1, 0, 1, 0), (monitor, 0, 1, 1, 1)]:
                if log.exists():
                    log.unlink()
                env = dict(os.environ, PATH=str(base) + os.pathsep + os.environ['PATH'], CALL_LOG=str(log), STAGED=staged, COMMIT_FAIL=str(c), PUSH_FAIL=str(p))
                r = subprocess.run(['bash', '-c', shell], cwd=base, env=env, capture_output=True, text=True)
                self.assertEqual(r.returncode, code, r.stderr)
                calls = [json.loads(x) for x in log.read_text().splitlines()]
                self.assertEqual(sum((x[:1] == ['push'] for x in calls)), pushes)
                self.assertFalse(any(('--force' in x or 'rebase' in x for x in calls)))
                self.assertEqual(calls[0], ['add', '--', monitor])
