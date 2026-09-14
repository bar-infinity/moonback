"""Explicit one-shot connectivity check. No crawl, storage or Journal generation."""
import os

from journal_adapter import JournalAdapterError, JournalRequest
from journal_openai import OpenAIJournalAdapter, OpenAIJournalConfig


def run_check(*, live=False):
    if not live:
        print('request=not_attempted error=live_flag_required')
        return 2
    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key or not api_key.strip():
        print('request=not_attempted error=unavailable')
        return 1
    # Avoid a header validation failure being described as a network attempt.
    if any(ord(c) < 33 or ord(c) > 126 for c in api_key):
        print('request=not_attempted error=authentication')
        return 1
    config = OpenAIJournalConfig()
    request = JournalRequest(
        instructions='This is a connectivity check, not a Journal task. Return the designated skip result: {"text": null}.',
        source_url='https://example.org/',
        public_text='Fixed synthetic connectivity test. No page was fetched. Skip this entry.',
    )
    try:
        adapter = OpenAIJournalAdapter(api_key=api_key, config=config, allow_live=True)
        result = adapter.generate_text(request)  # Exactly one call; no retry loop.
        if result is not None and type(result) is not str:
            raise JournalAdapterError('malformed_response')
    except JournalAdapterError as exc:
        category = JournalAdapterError(exc.category).category
        # An attempt need not have reached OpenAI (e.g. connection failure).
        print('request=attempted model=' + config.model + ' error=' + category)
        return 1
    except Exception:
        print('request=attempted model=' + config.model + ' error=provider_internal')
        return 1
    finally:
        api_key = None  # Does not unset the caller's environment or erase memory.
    outcome = 'skip' if result is None or not result.strip() else 'success'
    print('request=completed model=' + config.model + ' result=' + outcome)
    return 0
