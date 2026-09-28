"""Validation and metadata construction for untrusted Journal responses."""
import re
from datetime import datetime, timezone
import unicodedata
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from journal_safety import detect_secret_rule


ID_PATTERN = re.compile(r'[a-z0-9][a-z0-9_-]{0,79}')
INPUT_FIELDS = frozenset(('agent_name', 'response', 'source_url'))
DRAFT_FIELDS = frozenset(
    ('id', 'journal_id', 'received_at', 'agent_name', 'response', 'source_url')
)
PUBLIC_FIELDS = (
    'id', 'journal_id', 'published_at', 'agent_name', 'response', 'source_url'
)
JST = ZoneInfo('Asia/Tokyo')


class ResponseValidationError(ValueError):
    """A fixed validation category; never includes submitted text."""


def _plain_text(value, field, maximum, *, multiline=False):
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise ResponseValidationError('invalid_' + field)
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise ResponseValidationError('invalid_' + field) from None
    for character in value:
        if (unicodedata.category(character).startswith('C')
                and not (multiline and character == '\n')):
            raise ResponseValidationError('invalid_' + field)
    return value.strip()


def _source_url(value):
    if type(value) is not str:
        raise ResponseValidationError('invalid_source_url')
    if value == '':
        return ''
    if (len(value) > 2048
            or any(c.isspace() or unicodedata.category(c).startswith('C')
                   for c in value)):
        raise ResponseValidationError('invalid_source_url')
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ResponseValidationError('invalid_source_url') from None
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or port not in (None, 443)):
        raise ResponseValidationError('invalid_source_url')
    return value


def validate_response_identifier(value, field='id'):
    if type(value) is not str or ID_PATTERN.fullmatch(value) is None:
        raise ResponseValidationError('invalid_' + field)
    return value


def _check_secrets(item):
    for field in ('agent_name', 'response', 'source_url'):
        rule = detect_secret_rule(item[field])
        if rule is not None:
            raise ResponseValidationError('secret_detected:' + rule)


def validate_external_response(item):
    if type(item) is not dict or not set(item).issubset(INPUT_FIELDS):
        raise ResponseValidationError('invalid_fields')
    if 'agent_name' not in item or 'response' not in item:
        raise ResponseValidationError('invalid_fields')
    validated = {
        'agent_name': _plain_text(item['agent_name'], 'agent_name', 80),
        'response': _plain_text(
            item['response'], 'response', 2000, multiline=True),
        'source_url': _source_url(item['source_url']) if 'source_url' in item else '',
    }
    _check_secrets(validated)
    return validated


def validate_draft_response(item):
    if type(item) is not dict or set(item) != DRAFT_FIELDS:
        raise ResponseValidationError('invalid_draft_fields')
    external = validate_external_response({
        'agent_name': item.get('agent_name'),
        'response': item.get('response'),
        'source_url': item.get('source_url'),
    })
    validate_response_identifier(item.get('id'))
    validate_response_identifier(item.get('journal_id'), 'journal_id')
    if type(item.get('received_at')) is not str:
        raise ResponseValidationError('invalid_received_at')
    try:
        received = datetime.fromisoformat(item['received_at'].replace('Z', '+00:00'))
    except ValueError:
        raise ResponseValidationError('invalid_received_at') from None
    if received.tzinfo is None or received.utcoffset() != timezone.utc.utcoffset(received):
        raise ResponseValidationError('invalid_received_at')
    return dict(
        id=item['id'], journal_id=item['journal_id'],
        received_at=item['received_at'], **external,
    )


def validate_published_response(item):
    if type(item) is not dict or set(item) != set(PUBLIC_FIELDS):
        raise ResponseValidationError('invalid_public_fields')
    external = validate_external_response({
        'agent_name': item.get('agent_name'),
        'response': item.get('response'),
        'source_url': item.get('source_url'),
    })
    validate_response_identifier(item.get('id'))
    validate_response_identifier(item.get('journal_id'), 'journal_id')
    if (type(item.get('published_at')) is not str
            or re.fullmatch(r'\d{4}-\d{2}-\d{2}', item['published_at']) is None):
        raise ResponseValidationError('invalid_published_at')
    try:
        datetime.strptime(item['published_at'], '%Y-%m-%d')
    except ValueError:
        raise ResponseValidationError('invalid_published_at') from None
    return dict(
        id=item['id'], journal_id=item['journal_id'],
        published_at=item['published_at'], **external,
    )


def build_draft_response(item, journal_id, *, now=None, response_id=None):
    """Add server-owned metadata to validated external input."""
    external = validate_external_response(item)
    validate_response_identifier(journal_id, 'journal_id')
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ResponseValidationError('invalid_received_at')
    instant = instant.astimezone(timezone.utc)
    local_date = instant.astimezone(JST).date().isoformat().replace('-', '')
    ident = response_id or 'response-' + local_date + '-' + uuid4().hex
    draft = {
        'id': ident,
        'journal_id': journal_id,
        'received_at': instant.isoformat().replace('+00:00', 'Z'),
        **external,
    }
    return validate_draft_response(draft)


def public_from_draft(item, *, publication_date=None):
    draft = validate_draft_response(item)
    published = {
        key: draft[key]
        for key in ('id', 'journal_id', 'agent_name', 'response', 'source_url')
    }
    published['published_at'] = publication_date or datetime.now(JST).date().isoformat()
    return validate_published_response(published)
