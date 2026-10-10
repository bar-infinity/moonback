"""Replaceable storage boundary for untrusted Journal response drafts."""
from abc import ABC, abstractmethod
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import threading
import time
import warnings
from urllib.parse import urlsplit, urlunsplit

from response import (
    public_from_draft, validate_draft_response, validate_published_response,
    validate_response_identifier,
)


ROOT = Path(__file__).resolve().parent


class ResponseIntakeStore(ABC):
    @abstractmethod
    def save_draft(self, item):
        raise NotImplementedError


class ResponseDraftStore(ResponseIntakeStore):

    @abstractmethod
    def read_drafts(self):
        raise NotImplementedError

    @abstractmethod
    def publish(self, ident, *, reviewed=False, publication_date=None):
        raise NotImplementedError


class FileResponseStore(ResponseDraftStore):
    """Local JSON implementation; production can replace this boundary."""

    def __init__(self, root=ROOT):
        self.root = Path(root)

    def directory(self, stage):
        if stage not in ('drafts', 'published'):
            raise ValueError('Invalid response storage location')
        return self.root / stage / 'responses'

    def _save(self, stage, item):
        validator = (validate_draft_response if stage == 'drafts'
                     else validate_published_response)
        item = validator(item)
        directory = self.directory(stage)
        if stage == 'drafts':
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / 'drafts').mkdir(mode=0o700, exist_ok=True)
            directory.mkdir(mode=0o700, exist_ok=True)
            if os.name != 'posix':
                warnings.warn(
                    'Draft permissions are best-effort on this platform; verify local access controls.',
                    RuntimeWarning, stacklevel=2)
        else:
            directory.mkdir(parents=True, exist_ok=True)
        path = directory / (item['id'] + '.json')

        def private_opener(filename, flags):
            return os.open(filename, flags, 0o600)

        opener = private_opener if stage == 'drafts' else None
        with open(path, 'x', encoding='utf-8', opener=opener) as output:
            json.dump(item, output, ensure_ascii=False, indent=2)
            output.write('\n')
        return path

    def save_draft(self, item):
        return self._save('drafts', item)

    def read_drafts(self):
        return self._read('drafts')

    def read_published(self):
        return self._read('published')

    def _read(self, stage):
        validator = (validate_draft_response if stage == 'drafts'
                     else validate_published_response)
        return [validator(json.loads(path.read_text(encoding='utf-8')))
                for path in sorted(self.directory(stage).glob('*.json'))]

    def load_draft(self, ident):
        validate_response_identifier(ident)
        path = self.directory('drafts') / (ident + '.json')
        return validate_draft_response(json.loads(path.read_text(encoding='utf-8')))

    def publish(self, ident, *, reviewed=False, publication_date=None):
        if not reviewed:
            raise ValueError('Human review acknowledgement required')
        draft = self.load_draft(ident)
        return self._save(
            'published',
            public_from_draft(draft, publication_date=publication_date),
        )


class CloudflareStoreError(RuntimeError):
    """A fixed safe category; never includes provider data or credentials."""

    def __init__(self, category):
        super().__init__(category)
        self.category = category


class CloudflareResponseStore(ResponseIntakeStore):
    """Send validated drafts once to the private Worker intake endpoint."""

    MAX_PROVIDER_RESPONSE = 4096
    # Cloudflare Browser Integrity Check can reject generic Python client
    # signatures.  Keep this browser-compatible MoonBack service header fixed;
    # it must never contain request, credential, or user-derived data.
    USER_AGENT = (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/140.0.0.0 Safari/537.36'
    )

    CURSOR_PATTERN = re.compile(r'sha256:[0-9a-f]{64}')

    def __init__(self, worker_url, secret, *, timeout=8, transport=None,
                 status_transport=None):
        self.worker_url = self._validate_worker_url(worker_url)
        parsed = urlsplit(self.worker_url)
        self.status_url = urlunsplit((
            parsed.scheme, parsed.netloc, parsed.path + '/status', '', '',
        ))
        if type(secret) is not str or not secret:
            raise ValueError('Cloudflare response intake is not configured')
        if type(timeout) not in (int, float) or timeout <= 0:
            raise ValueError('Invalid Cloudflare response timeout')
        self._secret = secret
        self.timeout = timeout
        self._transport = transport or self._https_post
        self._status_transport = status_transport or self._https_get

    @staticmethod
    def _validate_worker_url(value):
        if type(value) is not str or not value:
            raise ValueError('Cloudflare response intake is not configured')
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise ValueError('Invalid Cloudflare Worker URL') from None
        if (parsed.scheme != 'https' or not parsed.hostname
                or parsed.username or parsed.password or parsed.query
                or parsed.fragment or port not in (None, 443)
                or parsed.path != '/v1/responses'):
            raise ValueError('Invalid Cloudflare Worker URL')
        return value

    def _https_post(self, payload, headers):
        parsed = urlsplit(self.worker_url)
        connection = http.client.HTTPSConnection(
            parsed.hostname, parsed.port or 443, timeout=self.timeout,
            context=ssl.create_default_context(),
        )
        try:
            connection.request('POST', parsed.path, body=payload, headers=headers)
            response = connection.getresponse()
            body = response.read(self.MAX_PROVIDER_RESPONSE + 1)
            return response.status, body
        finally:
            connection.close()

    def _https_get(self, url, headers):
        parsed = urlsplit(url)
        connection = http.client.HTTPSConnection(
            parsed.hostname, parsed.port or 443, timeout=self.timeout,
            context=ssl.create_default_context(),
        )
        try:
            connection.request('GET', parsed.path, headers=headers)
            response = connection.getresponse()
            body = response.read(self.MAX_PROVIDER_RESPONSE + 1)
            return response.status, body
        finally:
            connection.close()

    def save_draft(self, item):
        draft = validate_draft_response(item)
        payload = json.dumps(
            draft, ensure_ascii=False, separators=(',', ':'),
        ).encode('utf-8')
        headers = {
            'Authorization': 'Bearer ' + self._secret,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'User-Agent': self.USER_AGENT,
        }
        try:
            status, body = self._transport(payload, headers)
        except (TimeoutError, ConnectionError, OSError, http.client.HTTPException):
            raise CloudflareStoreError('unavailable') from None

        if status == 429:
            raise CloudflareStoreError('rate_limited')
        if status < 200 or status >= 300:
            raise CloudflareStoreError('unavailable')
        if type(body) is not bytes or len(body) > self.MAX_PROVIDER_RESPONSE:
            raise CloudflareStoreError('bad_gateway')
        try:
            result = json.loads(body.decode('utf-8'))
        except (UnicodeError, json.JSONDecodeError):
            raise CloudflareStoreError('bad_gateway') from None
        if (type(result) is not dict
                or set(result) != {'status', 'receipt_id'}
                or result.get('status') != 'stored'
                or result.get('receipt_id') != draft['id']):
            raise CloudflareStoreError('bad_gateway')
        return draft['id']

    def get_status(self):
        """Return only the validated public pending marker from the Worker."""
        headers = {
            'Authorization': 'Bearer ' + self._secret,
            'Accept': 'application/json',
            'User-Agent': self.USER_AGENT,
        }
        try:
            status, body = self._status_transport(self.status_url, headers)
        except (TimeoutError, ConnectionError, OSError, http.client.HTTPException):
            raise CloudflareStoreError('unavailable') from None
        if status < 200 or status >= 300:
            raise CloudflareStoreError('unavailable')
        if type(body) is not bytes or len(body) > self.MAX_PROVIDER_RESPONSE:
            raise CloudflareStoreError('bad_gateway')
        try:
            result = json.loads(body.decode('utf-8'))
        except (UnicodeError, json.JSONDecodeError):
            raise CloudflareStoreError('bad_gateway') from None
        if type(result) is not dict or set(result) != {'pending', 'cursor'}:
            raise CloudflareStoreError('bad_gateway')
        pending = result.get('pending')
        cursor = result.get('cursor')
        if type(pending) is not bool:
            raise CloudflareStoreError('bad_gateway')
        if cursor is not None and (
                type(cursor) is not str
                or self.CURSOR_PATTERN.fullmatch(cursor) is None):
            raise CloudflareStoreError('bad_gateway')
        if pending and cursor is None:
            raise CloudflareStoreError('bad_gateway')
        return {'pending': pending, 'cursor': cursor}


class LocalResponseRateLimiter:
    """Process-local Phase 1 guard; identifiers never enter response drafts."""

    def __init__(self, *, source_limit=3, journal_limit=10, window=60):
        self.source_limit = source_limit
        self.journal_limit = journal_limit
        self.window = window
        self._salt = secrets.token_bytes(32)
        self._sources = {}
        self._journals = {}
        self._lock = threading.Lock()

    def _source_key(self, source):
        return hmac.new(self._salt, source.encode('utf-8'), hashlib.sha256).digest()

    def allow(self, source, journal_id, *, now=None):
        current = time.monotonic() if now is None else now
        source_key = self._source_key(source)
        cutoff = current - self.window
        with self._lock:
            source_events = [t for t in self._sources.get(source_key, ()) if t > cutoff]
            journal_events = [t for t in self._journals.get(journal_id, ()) if t > cutoff]
            if (len(source_events) >= self.source_limit
                    or len(journal_events) >= self.journal_limit):
                return False
            source_events.append(current)
            journal_events.append(current)
            self._sources[source_key] = source_events
            self._journals[journal_id] = journal_events
            return True
