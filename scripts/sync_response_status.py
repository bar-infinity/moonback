"""Copy a validated public signal only. No Git, credentials, or publication API."""
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


def main():
    if sys.argv[1:] != ['--live']:
        print('Status sync not run: explicit_live_required')
        return 2
    try:
        changed = sync()
        print('Status sync: updated' if changed else 'Status sync: unchanged')
        return 0
    except SyncError as exc:
        print('Status sync failed: ' + str(exc))
        return 1
    except Exception:
        print('Status sync failed: internal_error')
        return 1


if __name__ == '__main__':
    sys.exit(main())
