"""Explicit Mac-only read access. No storage, publication or generation capability."""
import base64
import getpass
import http.client
import json
import re
import ssl
import sys
import warnings
from datetime import datetime
from urllib.parse import urlencode, urlsplit

from journal_safety import detect_secret_rule

TRUSTED_HOST = 'moonback-response-intake.london-1976.workers.dev'
ADMIN_URL = 'https://' + TRUSTED_HOST + '/v1/admin/responses'
MAX_BYTES = 64 * 1024
ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,79}')
TIMESTAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z')
SUMMARY_FIELDS = {'id', 'journal_id', 'received_at'}
DETAIL_FIELDS = SUMMARY_FIELDS | {'agent_name', 'response', 'source_url'}
USER_AGENT = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
              'AppleWebKit/537.36 (KHTML, like Gecko) '
              'Chrome/140.0.0.0 Safari/537.36')


class ResponseAdminError(ValueError):
    """Fixed safe messages only; never expose provider or submitted data."""


def identifier(value):
    if type(value) is not str or ID.fullmatch(value) is None:
        raise ResponseAdminError('invalid_id')
    return value


def timestamp(value):
    if type(value) is not str or TIMESTAMP.fullmatch(value) is None:
        raise ResponseAdminError('malformed_response')
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ResponseAdminError('malformed_response') from None


def validate_cursor(value):
    if type(value) is not str or re.fullmatch(r'[A-Za-z0-9_-]{1,256}', value) is None:
        raise ResponseAdminError('invalid_cursor')
    try:
        parts = json.loads(base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)))
        if type(parts) is not list or len(parts) != 2:
            raise ValueError()
        timestamp(parts[0])
        identifier(parts[1])
        canonical = base64.urlsafe_b64encode(
            json.dumps(parts, separators=(',', ':')).encode('ascii')
        ).decode().rstrip('=')
        if canonical != value:
            raise ValueError()
    except (ValueError, UnicodeError, TypeError):
        raise ResponseAdminError('invalid_cursor') from None
    return value


def summary(item):
    if type(item) is not dict or set(item) != SUMMARY_FIELDS:
        raise ResponseAdminError('malformed_response')
    try:
        identifier(item['id'])
        identifier(item['journal_id'])
        timestamp(item['received_at'])
    except ResponseAdminError:
        raise ResponseAdminError('malformed_response') from None
    return item


def detail(item):
    if type(item) is not dict or set(item) != DETAIL_FIELDS:
        raise ResponseAdminError('malformed_response')
    summary({key: item[key] for key in SUMMARY_FIELDS})
    for field, maximum in [('agent_name', 80), ('response', 2000), ('source_url', 2048)]:
        value = item[field]
        if type(value) is not str or len(value) > maximum:
            raise ResponseAdminError('malformed_response')
        if field != 'source_url' and not value.strip():
            raise ResponseAdminError('malformed_response')
        try:
            value.encode('utf-8')
        except UnicodeError:
            raise ResponseAdminError('malformed_response') from None
    if item['source_url']:
        try:
            url = urlsplit(item['source_url'])
            if (url.scheme != 'https' or not url.hostname or url.username or url.password
                    or url.query or url.fragment or url.port not in (None, 443)):
                raise ValueError()
        except ValueError:
            raise ResponseAdminError('malformed_response') from None
    return item


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


class ResponseAdminClient:
    def __init__(self, secret, *, url=ADMIN_URL, transport=None, timeout=10):
        if url != ADMIN_URL:
            raise ResponseAdminError('invalid_configuration')
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname != TRUSTED_HOST or parsed.path != '/v1/admin/responses':
            raise ResponseAdminError('invalid_configuration')
        if (type(secret) is not str or not secret or len(secret) > 512
                or any(ord(c) < 33 or ord(c) > 126 for c in secret)):
            raise ResponseAdminError('invalid_authentication_input')
        if type(timeout) not in (int, float) or timeout <= 0 or timeout > 30:
            raise ResponseAdminError('invalid_configuration')
        self._secret = secret
        self.timeout = timeout
        self._transport = transport or self._get

    def _get(self, path, headers):
        connection = http.client.HTTPSConnection(TRUSTED_HOST, timeout=self.timeout,
                                                 context=ssl.create_default_context())
        try:
            connection.request('GET', path, headers=headers)
            response = connection.getresponse()
            return response.status, response.read(MAX_BYTES + 1)
        finally:
            connection.close()

    def close(self):
        self._secret = None

    def _request(self, path):
        try:
            status, body = self._transport(path, {
                'Authorization': 'Bearer ' + self._secret,
                'User-Agent': USER_AGENT, 'Accept': 'application/json',
            })
        except TimeoutError:
            raise ResponseAdminError('timeout') from None
        except (ConnectionError, OSError, http.client.HTTPException):
            raise ResponseAdminError('connection') from None
        if 300 <= status < 400:
            raise ResponseAdminError('redirect_refused')
        if status != 200:
            category = {404: 'not_found_or_unauthorized', 429: 'rate_limited',
                        503: 'service_unavailable'}.get(status, 'provider_error')
            raise ResponseAdminError(category)
        if type(body) is not bytes or len(body) > MAX_BYTES:
            raise ResponseAdminError('response_too_large')
        try:
            return json.loads(body.decode('utf-8'), object_pairs_hook=strict_object)
        except (ValueError, UnicodeError, RecursionError):
            raise ResponseAdminError('malformed_response') from None

    def list_pending(self, *, limit=20, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ResponseAdminError('invalid_limit')
        query = {'limit': limit}
        if cursor is not None:
            query['cursor'] = validate_cursor(cursor)
        data = self._request('/v1/admin/responses?' + urlencode(query))
        if (type(data) is not dict or set(data) != {'items', 'next_cursor'}
                or type(data['items']) is not list or len(data['items']) > limit):
            raise ResponseAdminError('malformed_response')
        # Compare the original ASCII strings, as SQLite BINARY does, rather than
        # parsed datetimes (which could normalize different timestamp spellings).
        previous = (tuple(json.loads(base64.urlsafe_b64decode(
            cursor + '=' * (-len(cursor) % 4)))) if cursor is not None else None)
        seen = set()
        for item in data['items']:
            summary(item)
            key = (item['received_at'], item['id'])
            if item['id'] in seen or (previous is not None and key <= previous):
                raise ResponseAdminError('malformed_response')
            seen.add(item['id'])
            previous = key
        if data['next_cursor'] is not None:
            try:
                validate_cursor(data['next_cursor'])
            except ResponseAdminError:
                raise ResponseAdminError('malformed_response') from None
            if not data['items']:
                raise ResponseAdminError('malformed_response')
            next_key = tuple(json.loads(base64.urlsafe_b64decode(
                data['next_cursor'] + '=' * (-len(data['next_cursor']) % 4))))
            if next_key != previous:
                raise ResponseAdminError('malformed_response')
        return data

    def get_pending(self, ident):
        identifier(ident)
        data = self._request('/v1/admin/responses/' + ident)
        if type(data) is not dict or set(data) != {'item', 'status'} or data['status'] != 'pending':
            raise ResponseAdminError('malformed_response')
        detail(data['item'])
        if data['item']['id'] != ident:
            raise ResponseAdminError('malformed_response')
        return data


def safe_display(data):
    """Quote all external strings; mask the whole field if a secret rule matches."""
    def clean(value):
        if type(value) is str:
            rule = detect_secret_rule(value)
            return '[redacted: ' + rule + ']' if rule else value
        if type(value) is list:
            return [clean(item) for item in value]
        if type(value) is dict:
            return {key: clean(value) for key, value in value.items()}
        return value
    # ASCII JSON escapes ESC, controls, bidi and all other non-ASCII characters.
    return json.dumps(clean(data), ensure_ascii=True, indent=2)


def run_review(*, ident=None, limit=20, cursor=None):
    if not sys.stdin.isatty():
        raise ResponseAdminError('terminal_required')
    if ident is not None:
        identifier(ident)
    if not 1 <= limit <= 50:
        raise ResponseAdminError('invalid_limit')
    if cursor is not None:
        validate_cursor(cursor)
    secret = None
    client = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            secret = getpass.getpass('MoonBack response admin secret (hidden): ')
        client = ResponseAdminClient(secret)
        secret = None
        data = (client.get_pending(ident) if ident is not None
                else client.list_pending(limit=limit, cursor=cursor))
        print('Untrusted external response data (escaped; no publication):')
        print(safe_display(data))
    except (getpass.GetPassWarning, EOFError, KeyboardInterrupt):
        raise ResponseAdminError('secure_input_cancelled_or_unavailable') from None
    finally:
        secret = None
        if client is not None:
            client.close()
