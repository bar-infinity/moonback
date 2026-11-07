"""Explicit private observation reads. No ingest, storage, restore or publication."""
import argparse
import base64
from datetime import datetime
import getpass
import http.client
import json
import os
import re
import ssl
import sys
import unicodedata
import warnings
from urllib.parse import urlsplit

from journal_safety import contains_secret

# Exact host allowlist, not a suffix match. This is a proposal, not a live resource.
# A different production host must be reviewed and added before it can be selected.
TRUSTED_HOST = 'moonback-observation-archive.london-1976.workers.dev'
ALLOWED_WORKER_HOSTS = frozenset({TRUSTED_HOST})
ADMIN_URL_ENV = 'MOONBACK_OBSERVATION_ADMIN_URL'
ADMIN_PATH = '/v1/admin/observations'
# Canonical example only: no implicit default destination.
ADMIN_URL = 'https://' + TRUSTED_HOST + ADMIN_PATH
MAX_BYTES = 64 * 1024
ID = re.compile(r'obs-[0-9a-f]{32}')
SUMMARY_FIELDS = {'event_id', 'time', 'method', 'status'}
DETAIL_FIELDS = SUMMARY_FIELDS | {'schema_version', 'path', 'ua', 'response_completed', 'agent_like'}
USER_AGENT = 'Mozilla/5.0 (compatible; MoonBack-Observation-Review/1.0)'


class ObservationAdminError(ValueError):
    """Only fixed categories are raised; never raw server bodies/errors."""


def approved_admin_url(value):
    if type(value) is not str or not value:
        raise ObservationAdminError('configuration_required')
    try:
        if any(ord(c) <= 32 or ord(c) > 126 for c in value):
            raise ValueError()
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or parsed.hostname not in ALLOWED_WORKER_HOSTS or
                parsed.username is not None or parsed.password is not None or
                parsed.port is not None or parsed.path != ADMIN_PATH or
                parsed.query or parsed.fragment or
                value != 'https://' + parsed.hostname + ADMIN_PATH):
            raise ValueError()
    except (ValueError, TypeError):
        raise ObservationAdminError('invalid_configuration') from None
    return value


def identifier(value):
    if type(value) is not str or ID.fullmatch(value) is None:
        raise ObservationAdminError('invalid_id')
    return value


def normalized_time(value):
    if type(value) is not str or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)', value):
        raise ObservationAdminError('malformed_response')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ObservationAdminError('malformed_response') from None
    return parsed.isoformat(timespec='microseconds').replace('+00:00', 'Z')


def encode_cursor(parts):
    return base64.urlsafe_b64encode(json.dumps(parts, separators=(',', ':')).encode('ascii')).decode().rstrip('=')


def cursor_parts(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', value):
        raise ObservationAdminError('invalid_cursor')
    try:
        parts = json.loads(base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)))
        if (type(parts) is not list or len(parts) != 2 or
                normalized_time(parts[0]) != parts[0] or not identifier(parts[1]) or
                encode_cursor(parts) != value):
            raise ValueError()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ObservationAdminError('invalid_cursor') from None
    return tuple(parts)


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def summary_item(item):
    if type(item) is not dict or set(item) != SUMMARY_FIELDS:
        raise ObservationAdminError('malformed_response')
    try:
        identifier(item['event_id']); normalized_time(item['time'])
        if item['method'] not in ('GET', 'HEAD') or not (
                item['status'] is None or type(item['status']) is int and 200 <= item['status'] <= 599):
            raise ValueError()
    except (ValueError, TypeError):
        raise ObservationAdminError('malformed_response') from None
    return item


def detail_item(item):
    if type(item) is not dict or set(item) != DETAIL_FIELDS:
        raise ObservationAdminError('malformed_response')
    summary_item({k: item[k] for k in SUMMARY_FIELDS})
    if type(item['schema_version']) is not int or item['schema_version'] != 1:
        raise ObservationAdminError('malformed_response')
    for key, maximum in [('path', 2048), ('ua', 180)]:
        value = item[key]
        if type(value) is not str or len(value) > maximum:
            raise ObservationAdminError('malformed_response')
        try:
            value.encode('utf-8')
        except UnicodeError:
            raise ObservationAdminError('malformed_response') from None
    if not item['path'].startswith('/') or '?' in item['path'] or '#' in item['path']:
        raise ObservationAdminError('malformed_response')
    if any(type(item[key]) is not bool for key in ('response_completed', 'agent_like')):
        raise ObservationAdminError('malformed_response')
    return item


class ObservationAdminClient:
    def __init__(self, secret, *, url=None, transport=None):
        selected_url = approved_admin_url(os.environ.get(ADMIN_URL_ENV) if url is None else url)
        if (type(secret) is not str or not secret or len(secret) > 512 or
                any(ord(c) < 33 or ord(c) > 126 for c in secret)):
            raise ObservationAdminError('invalid_authentication_input')
        self._secret = secret
        self._host = urlsplit(selected_url).hostname
        self._transport = transport or self._get

    def close(self):
        self._secret = None  # Python cannot guarantee in-memory secret zeroization.

    def _get(self, path, headers):
        connection = http.client.HTTPSConnection(self._host, timeout=10,
                                                 context=ssl.create_default_context())
        try:
            connection.request('GET', path, headers=headers)
            response = connection.getresponse()
            # http.client never follows redirects; read at most the allowed bytes.
            return response.status, response.read(MAX_BYTES + 1)
        finally:
            connection.close()

    def _request(self, path):
        if self._secret is None:
            raise ObservationAdminError('client_closed')
        try:
            status, body = self._transport(path, {
                'Authorization': 'Bearer ' + self._secret,
                'User-Agent': USER_AGENT, 'Accept': 'application/json',
            })
        except TimeoutError:
            raise ObservationAdminError('timeout') from None
        except (OSError, http.client.HTTPException):
            raise ObservationAdminError('connection') from None
        if type(status) is not int:
            raise ObservationAdminError('malformed_response')
        if 300 <= status < 400:
            raise ObservationAdminError('redirect_refused')
        if status != 200:
            raise ObservationAdminError({404: 'not_found_or_unauthorized', 429: 'rate_limited',
                                         503: 'storage_unavailable'}.get(status, 'provider_error'))
        if type(body) is not bytes or len(body) > MAX_BYTES:
            raise ObservationAdminError('response_too_large')
        try:
            return json.loads(body.decode('utf-8'), object_pairs_hook=strict_object)
        except (ValueError, UnicodeError, RecursionError):
            raise ObservationAdminError('malformed_response') from None

    def summary(self):
        data = self._request(ADMIN_PATH + '/summary')
        if (type(data) is not dict or set(data) != {'total', 'first_time', 'last_time'} or
                type(data['total']) is not int or not 0 <= data['total'] <= 9007199254740991):
            raise ObservationAdminError('malformed_response')
        if data['total'] == 0:
            if data['first_time'] is not None or data['last_time'] is not None:
                raise ObservationAdminError('malformed_response')
        elif (normalized_time(data['first_time']) != data['first_time'] or
              normalized_time(data['last_time']) != data['last_time'] or data['first_time'] > data['last_time']):
            raise ObservationAdminError('malformed_response')
        return data

    def list_records(self, *, limit=20, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ObservationAdminError('invalid_limit')
        previous = cursor_parts(cursor) if cursor is not None else None
        path = ADMIN_PATH + '?limit=' + str(limit)
        if cursor is not None:
            path += '&cursor=' + cursor
        data = self._request(path)
        if (type(data) is not dict or set(data) != {'items', 'next_cursor'} or
                type(data['items']) is not list or len(data['items']) > limit):
            raise ObservationAdminError('malformed_response')
        seen = set()
        for item in data['items']:
            summary_item(item)
            key = (normalized_time(item['time']), item['event_id'])
            if item['event_id'] in seen or (previous is not None and key <= previous):
                raise ObservationAdminError('malformed_response')
            seen.add(item['event_id']); previous = key
        if data['next_cursor'] is not None:
            try:
                next_key = cursor_parts(data['next_cursor'])
            except ObservationAdminError:
                raise ObservationAdminError('malformed_response') from None
            if not data['items'] or next_key != previous:
                raise ObservationAdminError('malformed_response')
        return data

    def get_record(self, ident):
        identifier(ident)
        data = detail_item(self._request(ADMIN_PATH + '/' + ident))
        if data['event_id'] != ident:
            raise ObservationAdminError('malformed_response')
        return data


def safe_text(value, secret=None):
    if not isinstance(value, str):
        return str(value)
    if (secret and secret in value) or contains_secret(value):
        return '[redacted:credential]'
    return ''.join((('\\u%04x' if ord(c) <= 0xffff else '\\U%08x') % ord(c))
                   if unicodedata.category(c).startswith('C') else c for c in value)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Private D1 observation reads; no Render queue statistics')
    commands = parser.add_subparsers(dest='command', required=True)
    listing = commands.add_parser('list', help='One page; no paths/User-Agents')
    listing.add_argument('--limit', type=int, default=20)
    listing.add_argument('--cursor')
    commands.add_parser('summary', help='D1 saved count and recorded UTC time range only')
    showing = commands.add_parser('show', help='Explicit event detail; untrusted text is escaped/redacted')
    showing.add_argument('event_id')
    args = parser.parse_args(argv)
    secret = None
    client = None
    try:
        # Validate user inputs before asking for a credential or making a request.
        if args.command == 'list':
            if not 1 <= args.limit <= 50:
                raise ObservationAdminError('invalid_limit')
            if args.cursor is not None:
                cursor_parts(args.cursor)
        if args.command == 'show':
            identifier(args.event_id)
        url = approved_admin_url(os.environ.get(ADMIN_URL_ENV))
        if not sys.stdin.isatty():
            raise ObservationAdminError('tty_required')
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            secret = getpass.getpass('Observation READ secret (hidden): ')
        client = ObservationAdminClient(secret, url=url)
        if args.command == 'summary':
            data = client.summary()
            print('D1 saved records: ' + str(data['total']))
            print('Recorded period UTC: ' + str(data['first_time']) + ' .. ' + str(data['last_time']))
        elif args.command == 'list':
            data = client.list_records(limit=args.limit, cursor=args.cursor)
            for item in data['items']:
                print(item['event_id'], item['time'], item['method'], '-' if item['status'] is None else item['status'])
            print('next_cursor: ' + str(data['next_cursor']))
        else:
            data = client.get_record(args.event_id)
            for field in ('event_id', 'schema_version', 'time', 'method', 'status',
                          'response_completed', 'agent_like', 'path', 'ua'):
                print(field + ': ' + safe_text(data[field], secret))
        return 0
    except (getpass.GetPassWarning, EOFError, KeyboardInterrupt):
        print('Observation review stopped: secure_input_unavailable', file=sys.stderr)
        return 1
    except ObservationAdminError as exc:
        print('Observation review stopped: ' + str(exc), file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()
        secret = None


if __name__ == '__main__':
    sys.exit(main())
