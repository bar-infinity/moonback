"""Sync a public signal and freshness metadata. No Git or credentials."""
from datetime import datetime, timedelta, timezone
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import sys
import tempfile

HOST = 'moonback-response-intake.london-1976.workers.dev'
PATH = '/api/responses/status'
DESTINATION = Path(__file__).resolve().parents[1] / 'moonback_mvp/status/response-status.json'
MAX_BYTES = 4096
MONITOR_DESTINATION = DESTINATION.with_name('response-monitor.json')
MONITOR_FIELDS = {'schema_version', 'last_attempt_at', 'last_success_at', 'result',
                  'error_category', 'pending', 'cursor'}
ERROR_CATEGORIES = frozenset({
    'invalid_schema', 'invalid_pending', 'invalid_cursor', 'inconsistent_signal',
    'response_too_large', 'malformed_json', 'redirect_refused', 'http_error',
    'invalid_content_type', 'connection_or_timeout', 'unsafe_destination',
    'invalid_existing_snapshot', 'invalid_existing_monitor', 'storage_error',
    'invalid_timestamp', 'internal_error', 'publication_mismatch',
})
FRESHNESS = timedelta(hours=3)

USER_AGENT = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
              'AppleWebKit/537.36 (KHTML, like Gecko) '
              'Chrome/140.0.0.0 Safari/537.36 MoonBack-Status-Sync/1.0')


class SyncError(ValueError):
    """Fixed categories; never provider data, submitted content or raw errors."""


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError()


def validate(value):
    if type(value) is not dict or set(value) != {'pending', 'cursor'}:
        raise SyncError('invalid_schema')
    if type(value['pending']) is not bool:
        raise SyncError('invalid_pending')
    cursor = value['cursor']
    if cursor is not None and (
            type(cursor) is not str
            or re.fullmatch(r'sha256:[0-9a-f]{64}', cursor) is None):
        raise SyncError('invalid_cursor')
    if value['pending'] and cursor is None:
        raise SyncError('inconsistent_signal')
    return {'pending': value['pending'], 'cursor': cursor}


def decode(body):
    if type(body) is not bytes or len(body) > MAX_BYTES:
        raise SyncError('response_too_large')
    try:
        return validate(json.loads(body.decode('utf-8'), object_pairs_hook=strict_object,
                                   parse_constant=reject_constant))
    except SyncError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise SyncError('malformed_json') from None


def public_get():
    """One fixed HTTPS GET, bounded read, default TLS verification, no redirects."""
    connection = http.client.HTTPSConnection(
        HOST, timeout=15, context=ssl.create_default_context())
    try:
        connection.request('GET', PATH, headers={
            'Accept': 'application/json', 'User-Agent': USER_AGENT})
        response = connection.getresponse()
        if response.status != 200:
            raise SyncError('redirect_refused' if 300 <= response.status < 400 else 'http_error')
        content_type = response.getheader('Content-Type', '')
        if content_type.split(';', 1)[0].strip().lower() != 'application/json':
            raise SyncError('invalid_content_type')
        return response.read(MAX_BYTES + 1)
    except SyncError:
        raise
    except (OSError, http.client.HTTPException):
        raise SyncError('connection_or_timeout') from None
    finally:
        connection.close()


def sync(*, destination=DESTINATION, transport=public_get):
    # Test-only injection is not accepted through CLI or environment variables.
    signal = decode(transport())
    temporary = None
    try:
        if destination.is_symlink() or destination.parent.is_symlink():
            raise SyncError('unsafe_destination')
        if destination.exists():
            if destination.stat().st_size > MAX_BYTES:
                raise SyncError('invalid_existing_snapshot')
            try:
                current = decode(destination.read_bytes())
            except SyncError:
                raise SyncError('invalid_existing_snapshot') from None
            if current == signal:
                return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(signal, indent=2) + '\n').encode('ascii')
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.response-status-',
                                         delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
        return True
    except OSError:
        raise SyncError('storage_error') from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def utc_now():
    return datetime.now(timezone.utc)


def utc_datetime(value):
    if (type(value) is not str or re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z', value) is None):
        raise SyncError('invalid_timestamp')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise SyncError('invalid_timestamp') from None


def timestamp(clock):
    value = clock()
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise SyncError('invalid_timestamp')
    return value.isoformat(timespec='microseconds').replace('+00:00', 'Z')


def never_checked():
    return {'schema_version': 1, 'last_attempt_at': None, 'last_success_at': None,
            'result': 'never_checked', 'error_category': None, 'pending': None, 'cursor': None}


def validate_monitor(value, *, now=None):
    if (type(value) is not dict or set(value) != MONITOR_FIELDS or
            type(value['schema_version']) is not int or value['schema_version'] != 1):
        raise SyncError('invalid_existing_monitor')
    result = value['result']
    if result == 'never_checked':
        if value != never_checked():
            raise SyncError('invalid_existing_monitor')
        return dict(value)
    if result not in ('success', 'failure'):
        raise SyncError('invalid_existing_monitor')
    attempt = utc_datetime(value['last_attempt_at'])
    succeeded = utc_datetime(value['last_success_at']) if value['last_success_at'] is not None else None
    if now is not None:
        if type(now) is not datetime or now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise SyncError('invalid_timestamp')
        if attempt > now or (succeeded is not None and succeeded > now):
            raise SyncError('invalid_timestamp')
    if result == 'success':
        if value['error_category'] is not None or succeeded is None or succeeded < attempt:
            raise SyncError('invalid_existing_monitor')
    else:
        if (type(value['error_category']) is not str or value['error_category'] not in ERROR_CATEGORIES
                or (succeeded is not None and succeeded > attempt)):
            raise SyncError('invalid_existing_monitor')
    if succeeded is None:
        if value['pending'] is not None or value['cursor'] is not None:
            raise SyncError('invalid_existing_monitor')
    else:
        validate({'pending': value['pending'], 'cursor': value['cursor']})
    return dict(value)


def decode_monitor(body, *, now=None):
    if type(body) is not bytes or len(body) > MAX_BYTES:
        raise SyncError('invalid_existing_monitor')
    try:
        return validate_monitor(json.loads(body.decode('utf-8'), object_pairs_hook=strict_object,
                                           parse_constant=reject_constant), now=now)
    except (UnicodeError, ValueError, RecursionError):
        raise SyncError('invalid_existing_monitor') from None


def safe_destination(path):
    # Check every parent, not just the final directory; never follow a symlink
    # to an unrelated file when publishing machine-readable health information.
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise SyncError('unsafe_destination')


def load_monitor(path, *, now):
    safe_destination(path)
    try:
        if not path.exists():
            return never_checked()
        if path.stat().st_size > MAX_BYTES:
            raise SyncError('invalid_existing_monitor')
        return decode_monitor(path.read_bytes(), now=now)
    except OSError:
        raise SyncError('storage_error') from None


def write_monitor(path, value):
    safe_destination(path)
    encoded = (json.dumps(validate_monitor(value), indent=2) + '\n').encode('ascii')
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.response-monitor-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError:
        raise SyncError('storage_error') from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def sync_monitored(*, destination=DESTINATION, monitor=None, transport=public_get, clock=utc_now):
    """Exactly one upstream GET. On failure, preserve the last proven signal.

    The two local files are not a filesystem transaction. The publication gate
    verifies coherence before the workflow stages them in a single Git commit.
    Never infer a prior successful time from the legacy file's commit/mtime.
    """
    monitor = destination.with_name('response-monitor.json') if monitor is None else monitor
    attempt = timestamp(clock)
    previous = load_monitor(monitor, now=utc_datetime(attempt))
    safe_destination(destination)
    if previous['last_success_at'] is not None:
        # Do not hide a partial prior write or corruption by publishing a new pair.
        validate_publication(destination=destination, monitor=monitor, now=utc_datetime(attempt))
    try:
        raw = transport()
        signal = decode(raw)
        succeeded = timestamp(clock)
        if utc_datetime(succeeded) < utc_datetime(attempt):
            raise SyncError('invalid_timestamp')
        # Reuse the legacy writer/validation with the already fetched bytes.
        changed = sync(destination=destination, transport=lambda: raw)
        write_monitor(monitor, {
            'schema_version': 1, 'last_attempt_at': attempt, 'last_success_at': succeeded,
            'result': 'success', 'error_category': None, **signal,
        })
        return changed
    except Exception as exc:
        if isinstance(exc, SyncError) and str(exc) in ERROR_CATEGORIES:
            category = str(exc)
        elif isinstance(exc, (OSError, http.client.HTTPException)):
            category = 'connection_or_timeout'
        else:
            category = 'internal_error'
        failure = {**previous, 'last_attempt_at': attempt, 'result': 'failure', 'error_category': category}
        write_monitor(monitor, failure)
        raise SyncError(category) from None


def validate_publication(*, destination=DESTINATION, monitor=MONITOR_DESTINATION, now=None):
    """Offline gate; a coherent success/failure record is required for staging."""
    safe_destination(destination)
    instant = utc_now() if now is None else now
    value = load_monitor(monitor, now=instant)
    if value['result'] == 'never_checked':
        raise SyncError('invalid_existing_monitor')
    try:
        current = None
        if destination.exists():
            if destination.stat().st_size > MAX_BYTES:
                raise SyncError('invalid_existing_snapshot')
            current = decode(destination.read_bytes())
        if value['last_success_at'] is not None and current != {
                'pending': value['pending'], 'cursor': value['cursor']}:
            raise SyncError('publication_mismatch')
    except OSError:
        raise SyncError('storage_error') from None
    return value


def monitor_state(value, *, now):
    """Pure reader rule: no network, notification, file or checkpoint writes."""
    try:
        value = validate_monitor(value, now=now)
    except (SyncError, TypeError):
        return 'unknown'
    if value['result'] != 'success':
        return 'unknown'
    return 'healthy' if now - utc_datetime(value['last_success_at']) <= FRESHNESS else 'stale'


def notification_decision(value, *, now, notified_cursors=None):
    """Caller supplies history; this does not pretend to persist Luna task state."""
    state = monitor_state(value, now=now)
    if state != 'healthy':
        return state
    if notified_cursors is not None and (type(notified_cursors) not in (set, frozenset) or
            any(type(c) is not str or re.fullmatch(r'sha256:[0-9a-f]{64}', c) is None for c in notified_cursors)):
        return 'unknown'
    if not value['pending']:
        return 'no_pending_at_last_success'
    if notified_cursors is None:
        return 'initial_pending'
    return 'already_notified' if value['cursor'] in notified_cursors else 'new_candidate'


def main():
    if sys.argv[1:] not in (['--live'], ['--validate-publication']):
        print('Status sync not run: explicit_live_required')
        return 2
    try:
        if sys.argv[1:] == ['--validate-publication']:
            validate_publication()
            print('Status publication: valid')
            return 0
        changed = sync_monitored()
        print('Status sync: updated' if changed else 'Status sync: unchanged')
        return 0
    except SyncError as exc:
        category = str(exc) if str(exc) in ERROR_CATEGORIES else 'internal_error'
        print('Status sync failed: ' + category)
        return 1
    except Exception:
        print('Status sync failed: internal_error')
        return 1


if __name__ == '__main__':
    sys.exit(main())
