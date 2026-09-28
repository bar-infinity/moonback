"""Replaceable storage boundary for untrusted Journal response drafts."""
from abc import ABC, abstractmethod
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time
import warnings

from response import (
    public_from_draft, validate_draft_response, validate_published_response,
    validate_response_identifier,
)


ROOT = Path(__file__).resolve().parent


class ResponseDraftStore(ABC):
    @abstractmethod
    def save_draft(self, item):
        raise NotImplementedError

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
