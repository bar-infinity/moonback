"""Local draft/review workflow. Never run as an HTTP endpoint."""
import argparse
import json
import os
from datetime import datetime, timezone
from uuid import uuid4
from pathlib import Path
from content import FileStore, public_url
from crawler import crawl
from journal_safety import detect_secret_rule
from journal import generate_journal_entry, JournalGenerationError


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
    review.add_argument('kind', choices=('archive', 'journal'))
    publish = commands.add_parser('publish', help='Copy a reviewed draft to local published data; no deploy')
    publish.add_argument('kind', choices=('archive', 'journal'))
    publish.add_argument('id')
    publish.add_argument('--reviewed', action='store_true', help='I reviewed the exact file, sources, privacy and publication rights')
    check = commands.add_parser('openai-check', help='One explicit OpenAI connectivity request; no crawl or storage')
    check.add_argument('--live', action='store_true', help='Explicitly allow one potentially billable API request')
    args = parser.parse_args()
    if args.command == 'openai-check':
        from openai_check import run_check
        parser.exit(run_check(live=args.live))
    store = FileStore()
    try:
        if args.command == 'crawl':
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
            print(json.dumps(store.read('drafts', args.kind), ensure_ascii=False, indent=2))
        elif args.command == 'publish':
            print(store.publish(args.kind, args.id, reviewed=args.reviewed))
    except (ValueError, OSError) as exc:
        parser.exit(1, 'Stopped: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
