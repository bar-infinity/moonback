"""Opt-in, bounded, best-effort private observation delivery (stdlib only).

No disk spool: pending events cannot survive process destruction. Diagnostics
contain counters/categories only, never events, credentials or provider errors.
"""
import collections
from datetime import datetime
import json
import os
import re
import threading
import time
import unicodedata
from urllib import request, error
from urllib.parse import urlsplit
from uuid import uuid4

from journal_safety import contains_secret

FIELDS = ('event_id', 'schema_version', 'time', 'path', 'method', 'status',
          'response_completed', 'ua', 'agent_like')
MAX_EVENT_BYTES = 8192
USER_AGENT = 'Mozilla/5.0 (compatible; MoonBack-Observation/1.0)'


class ArchiveError(Exception):
    def __init__(self, category, retryable=False):
        allowed = {'invalid_event', 'secret_blocked', 'malformed_response',
                   'authentication', 'conflict', 'capacity', 'invalid_request',
                   'upstream_unavailable', 'connection', 'sender_failure'}
        category = category if category in allowed else 'sender_failure'
        self.category = category
        self.retryable = retryable
        super().__init__(category)


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_key')
        result[key] = value
    return result


def new_event_fields():
    return {'event_id': 'obs-' + uuid4().hex, 'schema_version': 1}


def encode_event(event):
    if not isinstance(event, dict) or set(event) != set(FIELDS):
        raise ArchiveError('invalid_event')
    if (not isinstance(event['event_id'], str) or
            not re.fullmatch(r'obs-[0-9a-f]{32}', event['event_id']) or
            type(event['schema_version']) is not int or event['schema_version'] != 1):
        raise ArchiveError('invalid_event')
    for key, maximum in (('time', 32), ('path', 2048), ('method', 4), ('ua', 180)):
        value = event[key]
        if (not isinstance(value, str) or len(value) > maximum or
                any(unicodedata.category(c).startswith('C') for c in value)):
            raise ArchiveError('invalid_event')
        if contains_secret(value):
            raise ArchiveError('secret_blocked')
    stamp = event['time']
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)', stamp):
        raise ArchiveError('invalid_event')
    try:
        datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    except ValueError:
        raise ArchiveError('invalid_event') from None
    if (not event['path'].startswith('/') or '?' in event['path'] or '#' in event['path'] or
            event['method'] not in ('GET', 'HEAD') or
            (event['status'] is not None and
             (type(event['status']) is not int or not 200 <= event['status'] <= 599)) or
            type(event['response_completed']) is not bool or
            type(event['agent_like']) is not bool):
        raise ArchiveError('invalid_event')
    data = json.dumps(event, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(data) > MAX_EVENT_BYTES:
        raise ArchiveError('invalid_event')
    return data


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ObservationTransport:
    def __init__(self, url, secret):
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme == 'https' and parsed.hostname and
                     parsed.path == '/v1/observations' and not parsed.query and
                     not parsed.fragment and not parsed.username and not parsed.password and
                     parsed.port in (None, 443))
        except (ValueError, TypeError):
            valid = False
        if not valid or not isinstance(secret, str) or not secret.strip() or any(ord(c) < 33 or ord(c) > 126 for c in secret):
            raise ValueError('invalid_archive_configuration')
        self._url, self._secret = url, secret
        # Do not inherit proxy settings or redirect credentials to another host.
        self._opener = request.build_opener(request.ProxyHandler({}), NoRedirect())

    def send(self, payload):
        req = request.Request(self._url, data=payload, method='POST', headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self._secret,
            'User-Agent': USER_AGENT,
        })
        try:
            with self._opener.open(req, timeout=8) as response:
                if response.status != 201:
                    raise ArchiveError('malformed_response')
                raw = response.read(1025)
                if len(raw) > 1024:
                    raise ArchiveError('malformed_response')
                result = json.loads(raw, object_pairs_hook=strict_object)
                expected_id = json.loads(payload)['event_id']
                if result != {'status': 'stored', 'event_id': expected_id}:
                    raise ArchiveError('malformed_response')
        except error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code in (401, 403, 404):
                raise ArchiveError('authentication') from None
            if code == 409:
                raise ArchiveError('conflict') from None
            if code == 507:
                raise ArchiveError('capacity') from None
            raise ArchiveError('upstream_unavailable' if code >= 500 or code == 429 else 'invalid_request',
                               retryable=code >= 500 or code == 429) from None
        except (TimeoutError, error.URLError, OSError):
            raise ArchiveError('connection', retryable=True) from None
        except (ValueError, UnicodeError, RecursionError):
            raise ArchiveError('malformed_response') from None


class ObservationArchive:
    """One daemon sender; queue byte/count budgets include the in-flight event."""
    def __init__(self, transport, *, max_events=256, max_bytes=512 * 1024,
                 max_attempts=4, retry_seconds=2):
        if min(max_events, max_bytes, max_attempts) <= 0 or retry_seconds < 0:
            raise ValueError('invalid_archive_limits')
        self.transport = transport
        self.max_events, self.max_bytes = max_events, max_bytes
        self.max_attempts, self.retry_seconds = max_attempts, retry_seconds
        self._condition = threading.Condition()
        self._queue = collections.deque()
        self._bytes = self._count = 0
        self._stopped = False
        self._thread = None
        self._counts = collections.Counter()
        self._last_error = None

    def start(self):
        with self._condition:
            if self._thread is None and not self._stopped:
                self._thread = threading.Thread(target=self._run, daemon=True,
                                                name='moonback-observation')
                self._thread.start()

    def submit(self, event):
        try:
            payload = encode_event(event)  # Immutable copy, never retain request objects.
        except ArchiveError as exc:
            with self._condition:
                self._counts['rejected'] += 1
                self._last_error = exc.category
            return False
        with self._condition:
            if self._stopped or self._count >= self.max_events or self._bytes + len(payload) > self.max_bytes:
                self._counts['dropped'] += 1
                self._last_error = 'stopped' if self._stopped else 'queue_full'
                return False
            self._queue.append((payload, 0, time.monotonic()))
            self._count += 1
            self._bytes += len(payload)
            self._condition.notify_all()
        return True

    def health(self):
        with self._condition:
            return {**dict(self._counts), 'pending': self._count,
                    'pending_bytes': self._bytes, 'last_error': self._last_error,
                    'stopped': self._stopped}

    def close(self):
        # No claim of durability: discard pending memory, count it explicitly.
        with self._condition:
            self._stopped = True
            self._counts['abandoned'] += len(self._queue)
            for payload, _, _ in self._queue:
                self._count -= 1
                self._bytes -= len(payload)
            self._queue.clear()
            self._condition.notify_all()
        if self._thread:
            self._thread.join(timeout=0.1)  # In-flight network may finish within its timeout.

    def _run(self):
        while True:
            with self._condition:
                while not self._stopped:
                    if self._queue:
                        delay = self._queue[0][2] - time.monotonic()
                        if delay <= 0:
                            break
                        self._condition.wait(delay)
                    else:
                        self._condition.wait()
                if self._stopped:
                    return
                payload, attempts, _ = self._queue.popleft()
            failure = None
            try:
                self.transport.send(payload)
            except ArchiveError as exc:
                failure = exc
            except Exception:
                failure = ArchiveError('sender_failure')
            with self._condition:
                attempts += 1
                if failure is None:
                    self._counts['stored'] += 1
                else:
                    self._last_error = failure.category
                    if failure.retryable and attempts < self.max_attempts and not self._stopped:
                        self._counts['retries'] += 1
                        self._queue.append((payload, attempts,
                                            time.monotonic() + self.retry_seconds * 2 ** (attempts - 1)))
                        continue
                    self._counts['failed'] += 1
                self._count -= 1
                self._bytes -= len(payload)


def configured_archive(environ=None):
    env = os.environ if environ is None else environ
    if env.get('MOONBACK_OBSERVATION_ARCHIVE_ENABLED') != '1':
        return None
    try:
        transport = ObservationTransport(env.get('MOONBACK_OBSERVATION_ARCHIVE_URL'),
                                         env.get('MOONBACK_OBSERVATION_ARCHIVE_SECRET'))
    except (ValueError, TypeError):
        return None
    return ObservationArchive(transport)
