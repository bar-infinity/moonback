"""Provider-neutral boundary. No provider, network client or configuration here."""
from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class JournalRequest:
    """Fresh instructions plus untrusted source data; never a conversation.

    Future adapters MUST keep instructions separate from source data, send no
    history/session/previous-response IDs, disable tools and provider memory,
    and perform no additional fetches. Use bounded response size/timeouts, no
    automatic retries, and reject tool calls or truncated/incomplete responses.
    Normalize only a designated skip to None; return only completed body text.
    This contract is not a sandbox for arbitrary adapter code.
    """
    instructions: str
    source_url: str
    public_text: str


class JournalAdapterError(Exception):
    """Safe provider-neutral categories; arbitrary provider text is discarded."""
    CATEGORIES = frozenset({
        'adapter_failed', 'unavailable', 'authentication', 'permission_model_access',
        'rate_limit', 'quota_billing', 'timeout', 'connection', 'malformed_response',
        'incomplete_response', 'provider_internal', 'provider_request',
        'invalid_configuration', 'invalid_request',
    })

    def __init__(self, category='adapter_failed'):
        self.category = category if isinstance(category, str) and category in self.CATEGORIES else 'adapter_failed'
        super().__init__(self.category)


class JournalAdapter(Protocol):
    def generate_text(self, request: JournalRequest) -> Optional[str]:
        ...
