"""Read-only pages and downloads for the reviewed CSV Test Cases release."""
import hashlib
import json
from html import escape
from pathlib import Path

from navigation import render_navigation

DATASET = Path(__file__).resolve().parent.parent / 'luna_tools/csv_test_cases/v0.1.0'
TOOLS_PATH = '/tools/'
DATASET_PATH = TOOLS_PATH + 'csv-test-cases/v0.1.0/'
MANIFEST_PATH = DATASET_PATH + 'cases.json'
TOOL_PAGES = (TOOLS_PATH, DATASET_PATH)
GITHUB = 'https://github.com/bar-infinity/moonback/tree/main/luna_tools/csv_test_cases/v0.1.0'
LICENSE = 'https://github.com/bar-infinity/moonback/blob/main/luna_tools/csv_test_cases/v0.1.0/LICENSE.md'

# Adding a file to the dataset or editing the manifest cannot add a public route.
SAMPLE_FILES = (
    'csv-001-basic-records.csv',
    'csv-002-quoted-comma.csv',
    'csv-003-escaped-quotes.csv',
    'csv-004-multiline-field.csv',
    'csv-005-utf8-labels.csv',
    'csv-006-unclosed-quote.csv',
    'csv-007-text-after-quote.csv',
    'csv-008-uneven-field-count.csv',
    'csv-009-duplicate-record-id.csv',
    'csv-010-invalid-business-values.csv',
)
SAMPLE_ROUTES = {DATASET_PATH + 'samples/' + name: name for name in SAMPLE_FILES}
ROUTES = frozenset((*TOOL_PAGES, MANIFEST_PATH, *SAMPLE_ROUTES))
CLASSIFICATIONS = {
    'valid': 'Valid',
    'syntax_error': 'Syntax error',
    'data_quality': 'Data quality',
}


def _read(relative):
    path = DATASET / relative
    if path.resolve(strict=True) != path:
        raise ValueError('Invalid dataset path')
    return path.read_bytes()


def load_dataset():
    raw = _read('cases.json')
    manifest = json.loads(raw.decode('utf-8'))
    cases = manifest['cases']
    if [case['file'] for case in cases] != ['samples/' + name for name in SAMPLE_FILES]:
        raise ValueError('Invalid sample inventory')
    files = {}
    for case, name in zip(cases, SAMPLE_FILES):
        if case['classification'] not in CLASSIFICATIONS:
            raise ValueError('Invalid sample classification')
        if not all(isinstance(case[key], str) for key in ('name', 'purpose')):
            raise ValueError('Invalid sample text')
        body = _read('samples/' + name)
        if (len(body) != case['byte_length'] or
                hashlib.sha256(body).hexdigest() != case['sha256']):
            raise ValueError('Invalid sample bytes')
        files[name] = body
    return raw, cases, files


DATASET_ERRORS = (ValueError, OSError, KeyError, TypeError, RuntimeError)


def available_pages():
    try:
        load_dataset()
    except DATASET_ERRORS:
        return ()
    return TOOL_PAGES


def _page(title, description, canonical, body, *, path):
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{escape(title)}</title><meta name="description" content="{escape(description)}">'
        f'<link rel="canonical" href="{escape(canonical)}">'
        '<style>body{background:#000;color:#eee;font:1rem/1.6 monospace;'
        'max-width:52rem;margin:2rem auto;padding:0 1rem}a{color:#eee}'
        'li{margin:.75rem 0;overflow-wrap:anywhere}'
        'nav a[aria-current]{font-weight:bold}</style></head><body>'
        f'{render_navigation(path)}'
        f'<main>{body}</main></body></html>'
    ).encode('utf-8')


def serve_asset(path, origin):
    """Return bytes, MIME type and optional fixed attachment name."""
    raw, cases, samples = load_dataset()
    if path == MANIFEST_PATH:
        return raw, 'application/json; charset=utf-8', None
    if path in SAMPLE_ROUTES:
        name = SAMPLE_ROUTES[path]
        return samples[name], 'text/csv; charset=utf-8', name
    if path == TOOLS_PATH:
        title = 'LUNA Tools | MoonBack'
        description = 'Free, synthetic fixtures for testing data processing.'
        body = ('<h1>LUNA Tools</h1><p>Small test datasets for people and AI agents.</p>'
                f'<ul><li><a href="{DATASET_PATH}">CSV Test Cases v0.1.0</a> — '
                'ten synthetic CSV fixtures with expected results.</li></ul>')
    elif path == DATASET_PATH:
        title = 'CSV Test Cases v0.1.0 | LUNA Tools'
        description = 'Ten synthetic CSV fixtures with expected parsing and data quality results.'
        body = (
            '<h1>CSV Test Cases v0.1.0</h1>'
            '<p>Ten fictional inventory samples for developers, testers, and AI agents '
            'checking CSV ingestion. Five valid, two syntax errors, three data quality cases.</p>'
            '<h2>Interpretation</h2>'
            '<p>RFC 4180-style layout and quoting with a declared UTF-8 extension '
            '(no BOM): comma delimiter, double quotes, doubled embedded quotes and CRLF. '
            'This is a test profile, not a claim of universal RFC conformance.</p>'
            '<p>The reference is Python 3.9+ standard-library <code>csv.reader</code> '
            'with <code>strict=True</code>, <code>newline=\'\'</code>, '
            '<code>QUOTE_MINIMAL</code>, no escape character and no initial-space skipping. '
            'Values stay strings.</p>'
            '<p>Malformed quoting fails parsing. A parsed row can still have the wrong '
            'number of fields or violate a business rule such as unique IDs or nonnegative '
            'integer quantities. Later checks are skipped when an earlier stage fails.</p>'
            '<h2>Downloads</h2>'
            f'<p><a href="{MANIFEST_PATH}">JSON manifest and expected results</a> · '
            f'<a href="{GITHUB}">GitHub source</a> · '
            f'<a href="{LICENSE}">LICENSE (CC0-1.0)</a></p>'
            '<p>Use these synthetic fixtures under CC0-1.0. '
            'This license applies to the dataset, not the whole MoonBack project.</p><ul>'
        )
        for case, name in zip(cases, SAMPLE_FILES):
            body += (f'<li><a href="{DATASET_PATH}samples/{name}">{name}</a> — '
                     f'{escape(case["name"])} ({CLASSIFICATIONS[case["classification"]]}). '
                     f'{escape(case["purpose"])}</li>')
        body += '</ul>'
    else:
        raise ValueError('Unknown Tools route')
    return _page(title, description, origin + path, body, path=path), 'text/html; charset=utf-8', None
