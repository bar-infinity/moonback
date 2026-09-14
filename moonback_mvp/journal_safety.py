"""Conservative secret detection, not a complete personal-data classifier.

Public contact details alone are not blocked. Do not print matched values.
Patterns are heuristic and may miss secrets or flag published examples.
"""
import re
from urllib.parse import unquote

_PATTERNS = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        ('private_key_header',
         r'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----'),
        ('openai_key', r'\bsk-(?:[a-z]+-)?[a-z0-9_-]{20,}\b'),
        ('github_key',
         r'\b(?:gh[pousr]_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,})\b'),
        ('aws_key', r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
        ('jwt',
         r'\beyJ[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\b'),
        ('authorization_scheme',
         r'\bauthorization\s*:\s*(?:bearer|basic)\s+[a-z0-9+/_.=-]{8,}'),
        ('generic_credential_assignment',
         r'''\b(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token|refresh[_ -]?token|client[_ -]?secret|password|passwd|credential|secret|token)["']?\s*[:=]\s*["']?[^\s"'<>;,]{4,}'''),
    )
)


def detect_secret_rule(value: str):
    """Return only a fixed rule name, never matched or surrounding text."""
    for candidate in (value, unquote(value)):
        for name, pattern in _PATTERNS:
            if pattern.search(candidate):
                return name
    return None


def contains_secret(value: str) -> bool:
    return detect_secret_rule(value) is not None
