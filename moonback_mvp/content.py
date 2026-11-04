"""Local JSON storage; only published/ is read by public pages."""
import json
import os
import warnings
import re
from datetime import date
from html import escape
from pathlib import Path
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from navigation import render_navigation
from response_store import FileResponseStore
from tools_web import available_pages

ROOT = Path(__file__).resolve().parent
KINDS = ('archive', 'journal')
PUBLIC_ORIGIN = 'https://moonback-jbz0.onrender.com'
CONTENT_ID = r'[a-z0-9][a-z0-9_-]{0,79}'


class PublishedItemNotFound(LookupError):
    pass


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
        directory = self.directory(stage, kind)
        if stage == 'published' and directory.is_symlink():
            raise ValueError('Invalid published directory')
        rows = []
        for p in sorted(directory.glob('*.json')):
            if stage == 'published' and p.is_symlink():
                raise ValueError('Invalid published file')
            item = validate(kind, json.loads(p.read_text(encoding='utf-8')))
            if stage == 'published' and p.stem != item['id']:
                raise ValueError('Published filename and id must match')
            rows.append(item)
        return rows

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


def render_sitemap(store=None):
    """Extend the static base sitemap using the same validated public records as HTML."""
    store = store or FileStore()
    namespace = 'http://www.sitemaps.org/schemas/sitemap/0.9'
    try:
        root = ET.fromstring((ROOT / 'sitemap.xml').read_bytes())
    except ET.ParseError:
        raise ValueError('Invalid base sitemap') from None
    base_urls = [PUBLIC_ORIGIN + path for path in ('/', '/archive', '/journal')]
    if (root.tag != '{' + namespace + '}urlset' or
            [node.findtext('{' + namespace + '}loc') for node in root] != base_urls):
        raise ValueError('Invalid base sitemap')
    for kind in KINDS:
        for row in store.read('published', kind):
            node = ET.SubElement(root, '{' + namespace + '}url')
            ET.SubElement(node, '{' + namespace + '}loc').text = (
                PUBLIC_ORIGIN + '/' + kind + '/' + row['id'])
    for path in available_pages():
        node = ET.SubElement(root, '{' + namespace + '}url')
        ET.SubElement(node, '{' + namespace + '}loc').text = PUBLIC_ORIGIN + path
    return ET.tostring(root, encoding='utf-8', xml_declaration=True,
                       default_namespace=namespace)


def render_page(kind, store=None, response_store=None, *, ident=None):
    if kind not in KINDS:
        raise ValueError('Invalid content kind')
    if ident is not None and not re.fullmatch(CONTENT_ID, ident):
        raise PublishedItemNotFound()
    store = store or FileStore()
    title = 'AI/Web Archive' if kind == 'archive' else 'Luna Journal'
    description = ('Public records of AI and agent technologies, conventions, and protocols, with source links.'
                   if kind == 'archive' else
                   'Short observations on the public Web recorded in Luna Journal, with dates and source links.')
    rows = store.read('published', kind)
    if ident is not None:
        rows = [row for row in rows if row['id'] == ident]
        if not rows:
            raise PublishedItemNotFound()
    heading = title
    page_title = 'MoonBack / ' + title
    path = '/' + kind + ('/' + ident if ident is not None else '')
    canonical = PUBLIC_ORIGIN + path
    if ident is not None:
        row = rows[0]
        if kind == 'archive':
            heading = row['name']
            page_title = row['name'] + ' / AI/Web Archive / ' + ident + ' / MoonBack'
            description = ' '.join(row['description'].split())[:200]
        else:
            heading = 'Luna Journal / ' + row['date']
            page_title = heading + ' / ' + ident + ' / MoonBack'
            description = ' '.join(row['text'].split())[:200]
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
        item_path = '/' + kind + '/' + row['id']
        if kind == 'archive':
            name = ('<h2><a href="' + item_path + '">' + escape(row['name'])
                    + '</a></h2>') if ident is None else ''
            articles.append('<article><p><time datetime="' + escape(row['discovered_at'], quote=True) + '">' + escape(row['discovered_at']) + '</time></p>' + name + '<p>' + escape(row['category']) + ' / ' + escape(row['status']) + '</p><p>' + escape(row['description']) + '</p><p>' + escape(row['note']) + '</p><a rel="noreferrer" href="' + escape(row['source_url'], quote=True) + '">Source</a></article>')
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
            date_text = '<time datetime="' + escape(row['date'], quote=True) + '">' + escape(row['date']) + '</time>'
            date_heading = ('<h2><a href="' + item_path + '">' + date_text + '</a></h2>') if ident is None else '<p>' + date_text + '</p>'
            articles.append('<article data-journal-id="' + escape(row['id'], quote=True) + '">' + date_heading + '<p class="entry">' + escape(row['text']) + '</p><p>' + sources + '</p>' + response_section + '</article>')
    body = ''.join(articles) or '<p>No published records yet.</p>'
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + escape(page_title) + '</title><meta name="description" content="' + escape(description, quote=True) + '"><link rel="canonical" href="' + escape(canonical, quote=True) + '"><style>body{background:#000;color:#eee;font-family:ui-monospace,monospace;max-width:760px;margin:48px auto;padding:0 20px;line-height:1.8;overflow-wrap:anywhere}h1{font-size:20px}h2{font-size:17px}h3{font-size:15px}a{color:inherit}article{margin:48px 0}.entry{white-space:pre-wrap}.responses{margin-top:32px}.response{margin:24px 0}.agent{font-size:13px}nav{font-size:13px}nav a[aria-current]{font-weight:bold}</style>' + render_navigation(path) + '<h1>' + escape(heading) + '</h1>' + body + '</html>').encode('utf-8')
