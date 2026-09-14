# Moonback MVP

A tiny experiment in a web surface that is almost empty for humans but structured for machine visitors.

## Run locally

```bash
python3 server.py
```

Open:
- http://127.0.0.1:8000/          human-facing black page
- http://127.0.0.1:8000/observe   request observer
- http://127.0.0.1:8000/llms.txt
- http://127.0.0.1:8000/agent.json
- http://127.0.0.1:8000/signal/001

## Notes

The "agent-like" label is only a heuristic based on User-Agent text. It does not prove a visitor is an AI.
For real public deployment, place this behind HTTPS and use platform/server logs as an additional source of truth.

## v0.3 — Archive / Luna Journal

Python 3.9+ standard library only. Run from this directory:

```bash
python3 server.py
```

- `/archive`: minimal black/white archive, newest discovery first.
- `/journal`: minimal black/white journal, newest date first.
- `/` and `/observe` retain their original HTML. Existing signal routes and stats remain.
- Only explicitly listed public routes are served. Source, logs, backups, drafts,
  prompts and raw storage files return 404 for GET and HEAD.
- No HTTP write/publish endpoint, background job or added dependency. An optional
  OpenAI adapter exists behind explicit local CLI activation; normal crawl keeps
  external Journal generation disabled.

### Content language

Archive is a machine-facing historical record. Use concise, non-promotional
English by default for `name`, `category`, `status`, `description`, `note`, and
`tags`. Preserve proper nouns, specification names, filenames and source URLs.
Do not turn uncertain claims into assertions. Manual input is preserved as entered;
this is an editorial default, not an ASCII-only validation rule.

Luna Journal is Luna's observation record. Write and store entries in English by
default, preserving useful original technical terms. Do not tailor either record
to the Director or a Japanese-speaking reader. Do not translate Journal entries
into Japanese when saving them. Japanese Director-facing output belongs to the
separate Souvenir concept; v0.3 does not implement a Souvenir feature.

### Storage

One UTF-8 JSON object per file:

```
published/archive/<id>.json    # reviewed, Git-managed public records
published/journal/<id>.json    # reviewed, Git-managed public records
drafts/archive/<id>.json       # local only, ignored by Git
drafts/journal/<id>.json       # local only, ignored by Git
```

`content.FileStore` separates storage from rendering/generation and can later be
replaced with a durable backend. IDs are unique; saving/publishing refuses
existing filenames. Drafts are retained after publication. There is no automated
update/overwrite workflow. Stop the local server while editing published JSON.
Malformed published JSON returns a generic 503 rather than exposing diagnostics.

Archive schema: `id`, `discovered_at` (YYYY-MM-DD), `name`, `category`, `status`,
`source_url`, `description`, `note`, `tags` (string array). Discovery date is the
MoonBack observation date, not the technology's invention date. Status is free text.

Archive `note` is public content, not draft workflow metadata. Keep review state
out of it; draft status is represented by the `drafts/` location. For example:
"Primary source checked. The discovery date is the date recorded in MoonBack."
Private workflow metadata, if needed, belongs in a separate draft-only field and
is excluded by the publication field allowlist. Human review must check public
fields for workflow-only wording as well as factual and privacy concerns.

Publishing copies a draft; later draft edits do not change the published copy.
To correct an already-published note, edit its local `published/archive/` JSON
atomically and reload `/archive`. No regeneration or re-publication is needed;
the existing publish command intentionally refuses to overwrite records.

Journal schema: `id`, `date` (YYYY-MM-DD), `text`, `source_urls` (nonempty array).
Text is displayed literally, not interpreted as HTML or Markdown.

### Crawl → draft → review → local publication

Confirm the site's terms and that the exact URL contains no private information.
Do not put credentials, tokens, personal data or private paths in the URL/name.
Then explicitly fetch one public page:

```bash
python3 manage.py crawl https://www.sitemaps.org/protocol.html --name sitemap.xml
python3 manage.py review archive
```

Crawl creates an Archive draft with `unknown` classification, a placeholder
explanation and a local `review_context` containing the acquired text. It does
not invent a description or follow discovered links. Review the original source,
write a short description yourself, set category/status/tags, and remove private
or copyrighted material. The local review command prints the draft to your terminal.
Do not share that output before checking it.

Journal generation is **disabled by default**. The OpenAI adapter is implemented,
but normal crawl never selects it or reads an API key. Live generation requires
both `--journal-live` and a nonblank `OPENAI_API_KEY`; either one alone cannot
start an OpenAI request. The v0.3.1 orchestration is implemented in `journal.py`.
No chat, preferences, previous Journal or local documents are added to the current
crawl context. Generated entries are always saved as drafts and require a separate
human-reviewed `publish ... --reviewed` command before they become public.

To manually prepare a Journal, create a local JSON file with the Journal schema,
then import it (the same command supports Archive objects):

```bash
python3 manage.py import journal /path/to/local-entry.json
python3 manage.py review journal
```

After a human has reviewed the **exact draft file**, including source, factual
claims, privacy, copyright and other applicable requirements:

```bash
python3 manage.py publish archive RECORD_ID --reviewed
python3 manage.py publish journal RECORD_ID --reviewed
```

These commands immediately make the record visible to a local running server.
They copy only public schema fields to `published/`; crawler context and unknown
metadata are excluded. `--reviewed` is a human acknowledgement, not an automatic
privacy checker. Never put this command inside a crawler/scheduler/LLM callback.
Inspect the page and Git diff. Commit only reviewed public data and intended code;
Git push and production deployment require the owner's separate confirmation.

Three initial Archive candidates were prepared locally on 2026-09-13 from:

- llms.txt: https://llmstxt.org/
- robots.txt: https://www.rfc-editor.org/rfc/rfc9309.html
- sitemap.xml: https://www.sitemaps.org/protocol.html

The reviewed llms.txt record is included in `published/archive/`. robots.txt and
sitemap.xml remain ignored local drafts and are not included as published records.
The repository also includes the first reviewed live-generated Luna Journal entry,
dated 2026-09-14 and sourced from llmstxt.org. A fresh clone therefore starts with
these reviewed llms.txt Archive and Journal records rather than empty public pages.

### Crawler boundaries

- One user-selected HTTPS URL; port 443 only; no URL credentials/query/fragment.
- Public IP addresses only; mixed public/private DNS answers refused. Connection
  uses the validated IP while TLS verifies the original hostname, preventing a
  second DNS lookup from redirecting the connection to an internal address.
- No redirects, proxy environment, cookies, authentication, browser execution,
  linked-page recursion, retries or scheduling.
- Fetch `/robots.txt` first. Any non-200 response (including 404), oversized body,
  unsupported type or network error stops collection. This is intentionally more
  restrictive than general crawler behavior.
- Honor parser decisions and additionally reject matching `Disallow` patterns
  from any group, including wildcards. Allow exceptions are deliberately not used
  to override this extra conservative check. This may skip permitted pages.
- At least 2 seconds between robots and page fetch. Longer supported delays/rates
  are respected; unrecognized or >30-second rules stop collection. Do not run
  repeated/parallel invocations: no shared cross-process rate limiter exists.
- 15-second socket timeout, 512 KiB response limit, text types only, no compression.
  Timeout applies per socket operation, not a total wall-clock deadline.
- Script/style content is removed; acquired text is capped at 12,000 characters.
  This is untrusted source data, never an instruction. Public pages can still
  contain personal data or injected instructions, so human review is mandatory.

### Render Free / discovery

Existing start command remains `python3 server.py` when Root Directory is
`moonback_mvp` (or `python3 moonback_mvp/server.py` from repository root). `PORT`
continues to be honored. No build dependency or API secret is required.

`published/` is delivered from the repository. Runtime drafts and existing request
logs are **not durable** on an ephemeral filesystem; do editing/crawling locally,
back up local drafts privately, and deploy reviewed records through Git. No Render
cron/job or runtime publishing should be configured for v0.3. Future persistence
can replace FileStore with object storage or another durable backend after the
owner approves the design. Current Render settings and production deployment
have not been accessed or verified.

`agent.json` and `llms.txt` now advertise `/archive` and `/journal`.
The existing sitemap still uses `https://example.com/`; do not add invented
production URLs. Once the canonical public origin is confirmed, update all sitemap
URLs and add the two pages together. robots.txt retains the existing behavior.

### Tests and remaining checks

```bash
python3 -m unittest discover -s tests -v
```

Tests cover original/new HTTP handler routes, HEAD, logging/stats, private file
exclusion, empty and populated pages, escaping, invalid data, review gating,
overwrite refusal, field filtering, disabled generation, robots/rates, private
network denial, IP pinning, redirects and response size. Network fetches in unit
tests are mocked; this does not certify access to every public site or compliance
with every robots dialect. Verify a chosen real source locally before regular use.

Existing `/stats` deliberately continues to show recent request paths and
User-Agent strings. The raw log file is now blocked, but visitor-supplied content
can still appear in the public observer. Do not send secrets to public URLs or
User-Agent headers. A redaction/retention change would be a separate observer
policy decision; no existing log was deleted or rewritten in v0.3.


## v0.3.1 — Provider-independent Journal generation

An optional OpenAI adapter is implemented without an SDK dependency. Its one-shot
connectivity check and an explicitly authorized llmstxt.org live Journal crawl have
been verified with `gpt-5.6-luna`; no API credential or provider response is stored
in the repository. Production deployment has not been performed. Normal
`manage.py crawl` keeps generation disabled, and no environment variable silently
activates a provider. The separate `openai-check --live` command remains an
explicit connectivity probe.

`generate_journal_entry(context, *, adapter=None)` validates an exact dictionary
with only `source_url` and `public_text` (strings; text <=12,000 characters).
Extra/missing keys, invalid URLs and invalid UTF-8 strings fail. With no adapter,
or a whitespace-only input, it returns `None` without reading the prompt or making
a request. Otherwise it reads the fixed UTF-8 `journal_prompt.txt` on each call;
a missing, unreadable or empty prompt is an error.

`journal_adapter.JournalRequest` is an immutable, fresh object containing only
`instructions`, `source_url` and `public_text`. The adapter's
`generate_text(request)` returns completed body text or Python `None` for skip.
No metadata is accepted from the adapter. Wrong types, overlong raw output
(>12,000 characters), invalid Unicode and detected secrets fail. Surrounding
whitespace is trimmed; internal line breaks remain. Blank output is normal skip.
No truncation, rewriting or automatic retry is performed. Provider adapters must
normalize their explicit skip representation to None and reject tool calls,
truncated responses and incomplete responses before returning.

The observation date is captured at function entry with
`datetime.now(ZoneInfo('Asia/Tokyo'))`, before adapter execution, and never
recomputed. ID is `journal-YYYY-MM-DD-<32 hex UUID4 characters>`. `source_urls`
is always a new list containing the original validated crawl URL, even if the
returned body mentions another URL. The four-field object passes the existing
Journal validator and is returned to `manage.py`, which saves it under `drafts/`.
The Archive date remains unchanged (UTC in the existing Archive implementation).
Python 3.9+ and system IANA timezone data for Asia/Tokyo are required; no new
Python package is needed in the verified local environment.

`journal_safety.py` checks input URL/text before the adapter call and output text
before returning a draft. It detects recognizable secret patterns: private-key
headers, common key/token formats, bearer/basic credentials and credential-like
assignments. URL escapes are also inspected. Public email addresses, telephone
numbers, author names and company information alone do not block generation.
No automatic redaction is performed. These are heuristics: published credential
examples can be blocked and unrecognized secrets can be missed. The output
policy prohibits unnecessary personal-data reproduction, but automated detection
does not establish that a body is free of personal information. Human review is
still required for privacy, copyright, facts and source correspondence.

Before saving a crawled Archive draft, `manage.py` now checks both `source_url`
and `public_text` using the shared secret detector. On detection, it exits nonzero
with a fixed message: no new Archive/Journal draft is saved and the Journal
function is not called. No matched text or URL is printed. Public contact details
alone do not block saving. This check runs even when Journal generation is disabled.
After this check the Archive draft is saved, including `review_context`, before
Journal generation. Later Journal failures do not erase that Archive draft.
Detection remains heuristic, not guaranteed sanitization. Existing drafts and
manual imports are not rescanned by this crawl-specific gate. Keep local drafts
private and review them before sharing. Journal skip is success;
adapter/validation errors exit nonzero with a fixed diagnostic and retain the
Archive draft. Journal save errors also retain the Archive and use a generic
message; after a filesystem write failure inspect for a partial Journal draft.
Raw adapter exceptions and response contents are not printed by this code.

For every provider or model change, review timeout/response bounds, data retention,
request separation and failure handling. Every request must be stateless: no
conversation IDs, previous response IDs, history, memory, retrieval attachments,
tools, browser, code execution or additional fetch. Keep system instructions
separate from untrusted Web data. This boundary supplies no such capabilities,
but is NOT an OS sandbox: arbitrary adapter code could violate the contract.
Unit tests use fake adapters only; the authorized live checks establish only the
specific connectivity and generation paths that were exercised.

Tests cover context restrictions, current prompt loading, independent immutable
requests, JST midnight boundaries and date freezing, skip, output limits,
malformed responses, fixed sources, secrets versus public contacts, sanitized
errors, Archive retention and draft-only generation. Existing manual import,
review, publish gate and HTTP route tests remain in the full suite.


### Draft permissions and Git exclusion

New directories created by MoonBack at `drafts/` and `drafts/<kind>/` request mode
0700; new draft JSON files request 0600 at exclusive creation, before any payload
is written. The process umask may make permissions stricter. Existing directories
and files are not chmod'ed, and published storage behavior is unchanged. An OS
permission error aborts saving rather than retrying with broader permissions.
On non-POSIX systems, creation modes are best-effort and a generic warning asks
for local access-control verification; ACLs/filesystem behavior still depend on
the environment. Existing draft directories may retain their older permissions.

`.gitignore` continues to exclude `moonback_mvp/drafts/`. The ignore file itself
is eligible for Git tracking and should be included in a future approved commit;
no staging or commit is performed by this change. Tests verify that draft paths
are ignored and no existing drafts are tracked. Force-add and copies outside the
ignored directory remain possible. Review output and retention are unchanged.


### OpenAI Journal adapter — explicit activation only

`journal_openai.py` implements the provider-neutral `generate_text(JournalRequest)`
boundary using Python's standard-library HTTPS client and the Responses API.
No SDK dependency was added. No environment variables or local files are read by
the adapter itself. Normal crawl does not select or activate it. Only
`openai-check --live` and `crawl ... --journal-live` read `OPENAI_API_KEY`
outside the adapter and inject it; no key is set or stored by the app.

Construction accepts `api_key`, immutable `OpenAIJournalConfig`, and an injectable
transport. Without a nonblank key it raises the safe `unavailable` category before
transport execution. Without an injected fake transport, real transport additionally
requires explicit `allow_live=True`; the default is false. Merely importing or
constructing the adapter never sends a request. No automatic model/provider fallback,
limit changes or retries exist. Each live use still requires an explicit CLI flag.

Defaults: model `gpt-5.6-luna`, socket-operation timeout 30 seconds (configurable
up to 120), max_output_tokens 1024 (configurable from 1 to 4096). The model string
is replaceable. API model/account access was confirmed by the authorized one-shot
check and first live Journal crawl. The token limit
includes any model reasoning and can produce an incomplete response, which is
rejected. It is not a monetary spending cap. Response reads are capped at 256 KiB.
The existing 12,000-character final Journal validation remains intact.

Each call builds a new payload: prompt instructions plus a fixed wire-format rule,
and exactly one user data object with `source_url` and `public_text`. No conversation,
previous_response_id, session, history, files, memory or earlier outputs are sent.
Tools are empty and tool_choice is none; store, stream and background are false.
Input truncation is disabled, reasoning effort and text verbosity are low, and
prompt-cache mode is explicit without breakpoints to disable the implicit cache.
The HTTPS
endpoint is fixed to api.openai.com/v1/responses, TLS verified, with no redirects,
proxy-environment handling, cookies, added fetches or tool execution. Store=false
prevents later API retrieval of this response. It is not a claim of zero provider
retention or processing; confirm account and provider policy before use.

Structured Outputs uses a strict schema with one required nullable string field:
`{"text":"body"}` means body; `{"text":null}` means explicit skip. A blank string
also skips. This is a wire envelope only: the common adapter returns str or None,
never metadata. The parser requires a completed response, exactly one completed
assistant message and one output_text block. Unknown/tool output types, refusals,
annotations, extra fields in the envelope, duplicate JSON keys, multiple messages,
invalid JSON, code fences, JSON-shaped bodies, nonstrings and incomplete responses
are rejected rather than salvaged. Empty-summary reasoning items can be ignored;
reasoning is never concatenated into the body. Validation is structural: prose
can still contain inappropriate claims or personal information, requiring review.

Common safe categories: unavailable, authentication, permission_model_access,
rate_limit, quota_billing, timeout, connection, malformed_response,
incomplete_response, provider_internal, provider_request, invalid_configuration,
and invalid_request. Error code/type distinguish quota/billing/credit/spend errors
from rate limits; provider error messages are never displayed. Unknown error shapes
remain failures, never skip. Billing errors leave the saved Archive draft in place,
create no Journal draft, exit nonzero and request human review before another try.
No persistent lock is added: an explicit future rerun is still a new attempt.

The adapter does not log keys, headers, input text, response text or raw exceptions.
Safe categories survive through Journal orchestration to the CLI. Transport errors
are sanitized, and there is no retry loop (including on timeout or rate limits).
A timeout may still represent a request processed by the provider; no retry or
billing refund assumption is made.

All automated tests use fake transports or a mocked HTTPSConnection, with socket
network creation blocked. They cover stateless payloads, tools off, explicit skip, malformed
and incomplete output, errors, no retries, fixed transport endpoint, replaceable
config, secret-safe messages and CLI Archive retention on billing failures.
No automated test uses an actual API key or API call. Separately from the test
suite, the owner-authorized connectivity check and first llmstxt.org live crawl
succeeded, and the resulting Journal was reviewed and published locally.

Official references consulted for the wire contract:
- https://developers.openai.com/api/docs/guides/structured-outputs
- https://developers.openai.com/api/docs/models/gpt-5.6-luna
- https://developers.openai.com/api/docs/guides/error-codes

Before future live or recurring use, continue to review account/model access,
strict-schema compatibility, output/reasoning limits, retention, project spend
controls and timeout behavior. The successful initial run does not cover every
provider exception or future API variation.

### Explicit live Journal crawl

Normal `python3 manage.py crawl URL --name NAME` never reads `OPENAI_API_KEY` and
continues with Journal generation disabled, even if that variable exists. A live
Journal request requires both the explicit flag and a nonblank key:

```bash
python3 manage.py crawl URL --name NAME --journal-live
```

Missing or malformed key configuration stops before crawling or saving. With the
two gates present, MoonBack crawls the one requested page through the existing
robots/safety path, checks its exact `source_url` and `public_text` for recognizable
secrets, then saves the Archive draft. Only after that save does it construct
OpenAIJournalAdapter with model `gpt-5.6-luna` and `allow_live=True`, load the
runtime Journal prompt, and make one request. The key is passed only as adapter
authentication; it is not part of the model input, JSON, draft, logs or CLI output.

Skip leaves the Archive draft and creates no Journal. Text receives code-generated
ID, JST date, and the original crawl URL before validation and draft-only storage.
Provider or Journal validation errors exit nonzero, keep the Archive, create no
Journal, do not publish, retry, or fall back. A draft storage failure is also
reported without its raw exception. No CLI option can inject a transport, model,
prompt, prior content, tools, metadata, URL list or destination.

The live request's only source material is this crawl's `source_url` and processed
`public_text`. `journal_prompt.txt` supplies instructions. Past Archive/Journal
data, Director or chat data, local documents, observation logs, visitor data,
stats, cookies, other environment variables and credentials are not read for the
request. The crawler still performs its required robots.txt check and the single
requested-page fetch; the adapter performs no Web fetch or tool call.

Tests use mocked crawl results and a fake adapter with socket creation blocked.
They cover both activation gates, one adapter call, runtime prompt loading,
source-only context, skip/text/error outcomes, fixed metadata/source, no
publication, no fallback or retry, and exclusion of old local records and secrets.


### Explicit one-shot connectivity CLI

`python3 manage.py openai-check --live` is a separate, potentially billable probe.
It does not run a crawler, read pages, generate a Journal entry, construct FileStore,
or save/publish any data. A fixed synthetic input requests `{"text": null}`.
The illustrative source URL is never fetched. No journal_prompt file is read by
this probe; it tests authentication/model access/API/schema/skip handling only.

Without `--live`, exit 2 and `request=not_attempted error=live_flag_required`.
With no nonblank OPENAI_API_KEY, exit 1 and `error=unavailable`, no adapter call.
A malformed header key is also rejected before the adapter call. Only the explicit
live path constructs OpenAIJournalAdapter with allow_live=True. CLI options cannot
inject a transport, arbitrary prompt, file, URL or response handler.

The probe calls generate_text exactly once. A validated None/blank result displays
skip; other validated text displays success, without printing that text. Success
uses `request=completed`; failure uses `request=attempted` plus a safe category,
not a claim that the provider actually received the request. Failure exits 1.
There are no retries, fallback models, storage writes, automatic key changes or
limit changes. Timeout does not prove no charge occurred; inspect before rerunning.
The key remains in the invoking shell unless cleared by that shell. Adapter
memory is released on process exit, not guaranteed cryptographically erased.

For a future explicitly authorized one-shot check, use a zsh subshell:

```zsh
(
  unsetopt XTRACE VERBOSE
  cd /Users/papepo/Documents/ChatGPT/MoonBack/moonback_mvp || exit 1
  read -r -s "OPENAI_API_KEY?OpenAI API key: " || exit 1
  printf '\n'
  export OPENAI_API_KEY
  PYTHONDONTWRITEBYTECODE=1 python3 manage.py openai-check --live
  check_result=$?
  unset OPENAI_API_KEY
  exit "$check_result"
)
```

The hidden input is not a literal command containing the key. Do not put a key in
shell startup files, command arguments, repository files or a .env file for this
check. Exiting the subshell removes its variable; a previously set parent-shell
variable must be cleared separately with `unset OPENAI_API_KEY`. The command has
been successfully run once with an explicit live flag; the key was not saved by
MoonBack. All automated tests use mocks with real socket creation blocked. A
successful skip can still incur API usage charges.
