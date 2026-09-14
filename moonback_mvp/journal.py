"""Isolated Journal generation. Only the caller saves drafts; no publishing."""
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from content import public_url, validate
from journal_adapter import JournalRequest, JournalAdapterError
from journal_safety import contains_secret

PROMPT_PATH = Path(__file__).resolve().with_name('journal_prompt.txt')
MAX_TEXT = 12000
JST = ZoneInfo('Asia/Tokyo')


class JournalGenerationError(ValueError):
    """Only fixed diagnostic codes; never expose inputs or provider messages."""


def generate_journal_entry(context, *, adapter=None):
    """Return a validated draft object or None (disabled/normal skip).

    No configured provider is selected implicitly. An adapter must be explicitly
    injected by reviewed application code. No environment or history is read.
    """
    observation_date = datetime.now(JST).date().isoformat()
    if type(context) is not dict or set(context) != {'source_url', 'public_text'}:
        raise JournalGenerationError('invalid_context')
    source_url, public_text = context['source_url'], context['public_text']
    if type(source_url) is not str or type(public_text) is not str or len(public_text) > MAX_TEXT:
        raise JournalGenerationError('invalid_context')
    try:
        public_url(source_url)
        source_url.encode('utf-8')
        public_text.encode('utf-8')
    except (ValueError, UnicodeError):
        raise JournalGenerationError('invalid_context') from None
    if adapter is None or not public_text.strip():
        return None
    try:
        instructions = PROMPT_PATH.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        raise JournalGenerationError('prompt_unavailable') from None
    if not instructions.strip():
        raise JournalGenerationError('prompt_empty')
    if any(contains_secret(value) for value in (source_url, public_text)):
        raise JournalGenerationError('input_secret_detected')
    request = JournalRequest(instructions, source_url, public_text)
    try:
        result = adapter.generate_text(request)
    except JournalAdapterError as exc:
        raise JournalGenerationError(exc.category) from None
    except Exception:
        raise JournalGenerationError('adapter_failed') from None
    if result is None:
        return None
    if type(result) is not str:
        raise JournalGenerationError('invalid_response')
    # Reject oversize raw output rather than silently truncating or retrying.
    if len(result) > MAX_TEXT:
        raise JournalGenerationError('response_too_long')
    text = result.strip()
    if not text:
        return None
    try:
        text.encode('utf-8')
    except UnicodeError:
        raise JournalGenerationError('invalid_response') from None
    if contains_secret(text):
        raise JournalGenerationError('output_secret_detected')
    entry = {
        'id': 'journal-' + observation_date + '-' + uuid4().hex,
        'date': observation_date,
        'text': text,
        'source_urls': [source_url],
    }
    try:
        return validate('journal', entry)
    except (ValueError, TypeError):
        raise JournalGenerationError('invalid_entry') from None
