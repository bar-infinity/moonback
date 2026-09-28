"""Local JSON storage; only published/ is read by public pages."""
import json
import os
import warnings
import re
from datetime import date
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from response_store import FileResponseStore

ROOT = Path(__file__).resolve().parent
KINDS = ('archive', 'journal')


def public_url(value):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError('Invalid source URL')
    p = urlsplit(value)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.query or p.fragment or p.port not in (None, 443):
        raise ValueError('Use a public HTTPS URL without credentials, query or fragment')
    return value


def validate(kind, item):
    if kind not in KINDS or not isinstance(item, dict):
        raise ValueError('Invalid content kind or object')
    required = ('id', 'discovered_at', 'name', 'category', 'status', 'source_url', 'description', 'note') if kind == 'archive' else ('id', 'date', 'text')
    for field in required:
        if not isinstance(item.get(field), str) or len(item[field]) > 12000:
            raise ValueError('Invalid field: ' + field)
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}', item['id']):
        raise ValueError('Invalid id')
    date.fromisoformat(item['discovered_at'] if kind == 'archive' else item['date'])
    if kind == 'archive':
        public_url(item['source_url'])
        if not all(item[f].strip() for f in ('name', 'category', 'status', 'description')):
            raise ValueError('Empty archive field')
        tags = item.get('tags')
        if not isinstance(tags, list) or len(tags) > 50 or any(not isinstance(t, str) or len(t) > 100 for t in tags):
            raise ValueError('Invalid tags')
    else:
        if not item['text'].strip():
            raise ValueError('Empty journal')
        sources = item.get('source_urls')
        if not isinstance(sources, list) or not sources:
            raise ValueError('Journal requires public sources')
        for source in sources:
            public_url(source)
    return item


class FileStore:
    def __init__(self, root=ROOT):
        self.root = Path(root)

    def directory(self, stage, kind):
        if stage not in ('drafts', 'published') or kind not in KINDS:
            raise ValueError('Invalid storage location')
        return self.root / stage / kind

    def read(self, stage, kind):
        return [validate(kind, json.loads(p.read_text(encoding='utf-8')))
                for p in sorted(self.directory(stage, kind).glob('*.json'))]

    def save(self, stage, kind, item):
        validate(kind, item)
        directory = self.directory(stage, kind)
        if stage == 'drafts':
            # Set permissions at creation, never briefly create a public draft.
            # Existing directories/files are deliberately not chmod'ed.
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / 'drafts').mkdir(mode=0o700, exist_ok=True)
            directory.mkdir(mode=0o700, exist_ok=True)
            if os.name != 'posix':
                warnings.warn('Draft permissions are best-effort on this platform; verify local access controls.',
                              RuntimeWarning, stacklevel=2)
        else:
            directory.mkdir(parents=True, exist_ok=True)
        path = directory / (item['id'] + '.json')
        # Exclusive creation: never overwrite a prior observation.
        def private_opener(filename, flags):
            return os.open(filename, flags, 0o600)

        opener = private_opener if stage == 'drafts' else None
        with open(path, 'x', encoding='utf-8', opener=opener) as f:
            json.dump(item, f, ensure_ascii=False, indent=2)
            f.write('\n')
        return path

    def publish(self, kind, ident, reviewed=False):
        if not reviewed:
            raise ValueError('Human review acknowledgement required')
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}', ident):
            raise ValueError('Invalid id')
        item = json.loads((self.directory('drafts', kind) / (ident + '.json')).read_text(encoding='utf-8'))
        validate(kind, item)
        # Copy only schema fields, never crawler context or unknown private metadata.
        fields = ('id', 'discovered_at', 'name', 'category', 'status', 'source_url', 'description', 'note', 'tags') if kind == 'archive' else ('id', 'date', 'text', 'source_urls')
        return self.save('published', kind, {k: item[k] for k in fields})


def render_page(kind, store=None, response_store=None):
    store = store or FileStore()
    title = 'AI/Web Archive' if kind == 'archive' else 'Luna Journal'
    rows = store.read('published', kind)
    rows.sort(key=lambda x: (x.get('discovered_at', x.get('date')), x['id']), reverse=True)
    responses = {}
    if kind == 'journal':
        response_store = response_store or FileResponseStore(store.root)
        published_responses = response_store.read_published()
        published_responses.sort(
            key=lambda x: (x['published_at'], x['id']))
        for response in published_responses:
            responses.setdefault(response['journal_id'], []).append(response)
    articles = []
    for row in rows:
        if kind == 'archive':
            articles.append('<article><p>' + escape(row['discovered_at']) + '</p><h2>' + escape(row['name']) + '</h2><p>' + escape(row['category']) + ' / ' + escape(row['status']) + '</p><p>' + escape(row['description']) + '</p><p>' + escape(row['note']) + '</p><a rel="noreferrer" href="' + escape(row['source_url'], quote=True) + '">Source</a></article>')
        else:
            sources = ' · '.join('<a rel="noreferrer" href="' + escape(u, quote=True) + '">Source</a>' for u in row['source_urls'])
            response_rows = []
            for response in responses.get(row['id'], ()):
                source = ''
                if response['source_url']:
                    source = '<p><a rel="nofollow ugc noreferrer" href="' + escape(response['source_url'], quote=True) + '">Source</a></p>'
                response_rows.append(
                    '<div class="response"><p class="agent">Claimed agent: '
                    + escape(response['agent_name']) + '</p><p class="entry">'
                    + escape(response['response']) + '</p>' + source + '</div>')
            response_section = (
                '<section class="responses"><h3>Responses</h3>'
                + ''.join(response_rows) + '</section>' if response_rows else '')
            articles.append('<article data-journal-id="' + escape(row['id'], quote=True) + '"><h2>' + escape(row['date']) + '</h2><p class="entry">' + escape(row['text']) + '</p><p>' + sources + '</p>' + response_section + '</article>')
    body = ''.join(articles) or '<p>No published records yet.</p>'
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MoonBack / ' + title + '</title><style>body{background:#000;color:#eee;font-family:ui-monospace,monospace;max-width:760px;margin:48px auto;padding:0 20px;line-height:1.8;overflow-wrap:anywhere}h1{font-size:20px}h2{font-size:17px}h3{font-size:15px}a{color:inherit}article{margin:48px 0}.entry{white-space:pre-wrap}.responses{margin-top:32px}.response{margin:24px 0}.agent{font-size:13px}nav{font-size:13px}</style><nav><a href="/archive">Archive</a> · <a href="/journal">Journal</a></nav><h1>' + title + '</h1>' + body + '</html>').encode('utf-8')
