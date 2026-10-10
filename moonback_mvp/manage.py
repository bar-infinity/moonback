"""Local draft/review workflow. Never run as an HTTP endpoint."""
import argparse
import json
import os
from datetime import datetime, timezone
from uuid import uuid4
from pathlib import Path
from content import FileStore, public_url
from journal_safety import detect_secret_rule
from response_store import FileResponseStore


def crawl(*args, **kwargs):
    """Lazy boundary: response-only commands never import the crawler."""
    from crawler import crawl as implementation
    return implementation(*args, **kwargs)


def generate_journal_entry(*args, **kwargs):
    """Lazy boundary: response-only commands never import Journal generation."""
    from journal import generate_journal_entry as implementation
    return implementation(*args, **kwargs)


def main():
    parser = argparse.ArgumentParser(description='MoonBack local draft workflow')
    commands = parser.add_subparsers(dest='command', required=True)
    collect = commands.add_parser('crawl', help='Fetch exactly one public URL into archive drafts')
    collect.add_argument('url')
    collect.add_argument('--name', required=True)
    collect.add_argument('--journal-live', action='store_true',
                         help='Explicitly allow one potentially billable OpenAI Journal request')
    manual = commands.add_parser('import', help='Import a manually prepared JSON draft')
    manual.add_argument('kind', choices=('archive', 'journal'))
    manual.add_argument('file', type=Path)
    review = commands.add_parser('review', help='Print drafts locally for review')
    review.add_argument('kind', choices=('archive', 'journal', 'response'))
    review.add_argument('id', nargs='?')
    review.add_argument('--remote', action='store_true', help='Explicit read-only pending response access')
    review.add_argument('--limit', type=int, default=None)
    review.add_argument('--cursor')
    publish = commands.add_parser('publish', help='Copy a reviewed draft to local published data; no deploy')
    publish.add_argument('kind', choices=('archive', 'journal', 'response'))
    publish.add_argument('id')
    publish.add_argument('--reviewed', action='store_true', help='I reviewed the exact file, sources, privacy and publication rights')
    check = commands.add_parser('openai-check', help='One explicit OpenAI connectivity request; no crawl or storage')
    check.add_argument('--live', action='store_true', help='Explicitly allow one potentially billable API request')
    args = parser.parse_args()
    if args.command == 'review':
        if args.remote:
            if args.kind != 'response' or (args.id and (args.limit is not None or args.cursor is not None)):
                parser.error('Remote review requires response; pagination is list-only')
            from response_admin import run_review, ResponseAdminError
            try:
                run_review(ident=args.id, limit=20 if args.limit is None else args.limit, cursor=args.cursor)
            except ResponseAdminError as exc:
                parser.exit(1, 'Remote review stopped: ' + str(exc) + '\n')
            return
        if args.id is not None or args.limit is not None or args.cursor is not None:
            parser.error('ID and pagination require --remote')
    if args.command == 'openai-check':
        from openai_check import run_check
        parser.exit(run_check(live=args.live))
    store = FileStore()
    response_store = FileResponseStore(store.root)
    try:
        if args.command == 'crawl':
            from journal import JournalGenerationError
            journal_adapter = None
            api_key = None
            if args.journal_live:
                public_url(args.url)
                api_key = os.environ.get('OPENAI_API_KEY')
                if not api_key or not api_key.strip():
                    parser.exit(1, 'Crawl not started: OpenAI Journal adapter unavailable.\n')
                if any(ord(c) < 33 or ord(c) > 126 for c in api_key):
                    parser.exit(1, 'Crawl not started: OpenAI Journal authentication configuration invalid.\n')
            context = crawl(args.url)
            secret_rule = next(
                (rule for key in ('source_url', 'public_text')
                 if (rule := detect_secret_rule(context[key])) is not None),
                None,
            )
            if secret_rule is not None:
                parser.exit(
                    1,
                    'Crawl stopped: secret detected. rule=' + secret_rule
                    + '. No Archive or Journal draft created.\n',
                )
            today = datetime.now(timezone.utc).date().isoformat()
            ident = today + '-' + uuid4().hex[:12]
            item = dict(id=ident, discovered_at=today, name=args.name, category='unknown', status='unknown', source_url=args.url, description='Retrieved. Review the source and write a description in English.', note='', tags=[])
            item['review_context'] = context  # Local only; excluded during publish.
            print(store.save('drafts', 'archive', item))
            try:
                if args.journal_live:
                    from journal_openai import OpenAIJournalAdapter, OpenAIJournalConfig
                    journal_adapter = OpenAIJournalAdapter(
                        api_key=api_key,
                        config=OpenAIJournalConfig(model='gpt-5.6-luna'),
                        allow_live=True,
                    )
                entry = generate_journal_entry(context, adapter=journal_adapter)
                if entry is not None:
                    print(store.save('drafts', 'journal', entry))
                else:
                    print('Journal disabled/skipped. No journal generated.')
            except JournalGenerationError as exc:
                suffix = ' Human review required before another attempt.' if str(exc) == 'quota_billing' else ''
                parser.exit(1, 'Archive draft retained. Journal not created: ' + str(exc) + suffix + '\n')
            except (ValueError, OSError):
                parser.exit(1, 'Archive draft retained. Journal save failed; inspect local drafts.\n')
            finally:
                api_key = None
        elif args.command == 'import':
            print(store.save('drafts', args.kind, json.loads(args.file.read_text(encoding='utf-8'))))
        elif args.command == 'review':
            rows = (response_store.read_drafts() if args.kind == 'response'
                    else store.read('drafts', args.kind))
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        elif args.command == 'publish':
            if args.kind == 'response':
                if not args.reviewed:
                    raise ValueError('Human review acknowledgement required')
                draft = response_store.load_draft(args.id)
                journals = store.read('published', 'journal')
                if not any(row['id'] == draft['journal_id'] for row in journals):
                    raise ValueError('Published Journal not found')
                print(response_store.publish(args.id, reviewed=args.reviewed))
            else:
                print(store.publish(args.kind, args.id, reviewed=args.reviewed))
    except (ValueError, OSError) as exc:
        parser.exit(1, 'Stopped: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
