# Private observation archive — local prototype

Standalone Worker and D1 schema; not the Journal response Worker/database.
No production configuration, deployment, database ID or secret is included.
The runtime entry is `src/index.js`; binding `OBS_DB` must point ONLY to a new
observation database. Apply `migrations/0001_observations.sql` there after approval.
Set Worker Secret `MOONBACK_OBSERVATION_INGEST_SECRET` separately after approval.
Never use either response intake/admin credential here.

Authenticated `POST /v1/observations` stores events. Ingest GET/HEAD and unknown
paths return fixed404; separate read-secret management routes are described below. No public read route, tools, outbound fetch or arbitrary SQL.
Credentials are hashed and compared over all 32 bytes. Bodies are bounded to
8 KiB, strict UTF-8, flat scalar JSON with no duplicate/unknown keys. Validation
is repeated before parameterized SQL. Event IDs are immutable; identical replays
return 201 without another row; different payloads for an existing ID return409.
D1's exact documented database-size, account-storage and Free daily row read/write
limit messages return507 with only `{"error":"capacity"}`; the existing Python
client stops retrying that event. Only full matches (with or without the
`D1_ERROR: ` prefix) qualify. Unknown quota/storage errors, changed message formats,
bare SQLite codes and other D1 errors remain503, never false success. See the
[official D1 error list](https://developers.cloudflare.com/d1/observability/debug-d1/#list-of-d1_errors).
No body, credential, provider exception or visitor metadata is logged.

Tests: `node --test` (built-in Node test runner, no package installation).
Tests use an in-memory fake and a local Python SQLite SQL/migration check.
No Wrangler deploy configuration is supplied intentionally: selecting a new
Worker/database and checking free quota requires separate approval.

## GEO-OBS-002: private read-only history (local implementation)

A separate Worker Secret, `MOONBACK_OBSERVATION_READ_SECRET`, is required for:

- `GET/HEAD /v1/admin/observations`: list one page, default20/max50.
  Only `event_id`, `time`, `method`, `status` are returned per item, plus a
  `next_cursor` (null on the final page).
- `GET/HEAD /v1/admin/observations/summary`: exact D1 saved-row count (`total`) and
  recorded UTC range (`first_time`, `last_time`); both dates null for an empty DB.
- `GET/HEAD /v1/admin/observations/<event_id>`: explicit detail with the nine
  event fields. The internal dedup hash is never returned. Missing IDs return404.

The read secret is never placed on Render. Unset/empty/invalid read secrets disable
management. Read/write secrets must be distinct; identical values disable both
management and ingest, failing closed rather than allowing a reader to write.
With no read secret, GEO-OBS-001 ingest remains unchanged. Never reuse either
Journal response secret. Unauthorized requests return fixed404 before any D1
query. No CORS relaxation, public read route or new authentication provider.
All management responses are no-store and limited to64 KiB. Write methods are
rejected. Errors are fixed400/404/429/503, without provider bodies or SQL.

Only `limit` and `cursor` are accepted on the list route. Unknown, duplicate,
encoded or malformed parameters are rejected before D1 reads. Detail and summary
accept no query. SQL is fixed SELECT with bound cursor/ID/limit parameters.
The canonical cursor encodes normalized UTC microseconds plus event ID. It is a
position marker, not an authentication token; editing it cannot bypass auth or
access another table. Its format is shared with the CLI and validated strictly.

Apply `0002_history_index.sql` to the **observation DB only**, after separate
approval. It adds an expression index; it does not edit existing records. The
index matches keyset queries even when timestamps use Z/+00:00 or different
fractional precision. Cursor order and summary dates normalize these equivalent
spellings; stored time and ingest dedup hashes remain unchanged. The index adds
storage and write work, to include in the free-quota review.

List pages and summary are independent live reads, not a cross-request snapshot.
Concurrent later inserts with an earlier timestamp/key can require restarting the
list to see them. No automatic full-history loop or export is implemented. The
summary COUNT scans saved entries; it runs only on an explicit summary request,
not every list page. Aggregate counts are not queue/failed/dropped counts from
Render and cannot prove that every Render request was archived. A best-effort
per-binding/per-isolate budget allows30 authenticated reads per minute (no visitor
identity stored); it is not a global/account quota and does not gate ingest.

The Mac CLI is `moonback_mvp/observation_admin.py`. Before Secret input, it
requires the non-secret `MOONBACK_OBSERVATION_ADMIN_URL` environment setting.
There is no implicit destination. Its HTTPS URL must match an exact code-reviewed
host allowlist (currently only `moonback-observation-archive.london-1976.workers.dev`)
and the exact `/v1/admin/observations` path. Userinfo, custom/explicit ports,
query/fragment, encoded paths, trailing slash, whitespace and other hosts are
rejected without a request. There is no arbitrary URL CLI flag or wildcard-host
permission. Redirects are never followed. The configured host, not a response or
redirect target, is used for the HTTPS connection.

The allowlisted host is a proposal, not an assertion that a resource exists.
After the production host is confirmed, set the URL only if it is allowlisted.
If it differs, review and update the exact host allowlist before setting it;
configuration alone cannot authorize a third-party or other account Worker host.
Only the non-secret URL is configured through the environment; the read Secret
still comes exclusively from hidden Terminal input, never the environment.
One explicit CLI command makes one HTTPS GET (timeout10 seconds, no retry,
64 KiB response bound). Secret comes only from getpass on a TTY; no command-line,
environment, disk, Keychain or log storage. Python memory cannot be securely
zeroized; close clears references as far as practical.

Details are untrusted historical data. CLI output escapes controls, ESC and bidi
characters and redacts entire fields detected by the existing secret detector
(or containing the entered read secret). It never opens/fetches logged paths or
User-Agents. Detection remains heuristic, not a complete personal-data filter.
Authorized details may carry original path/UA in process memory; terminal sessions
must remain private. No raw row, read secret or detail goes to public Observe,
`/stats`, GitHub, response status, OpenAI or crawler.

Local commands (from `moonback_mvp`, **after separately approved deployment**):

```sh
# Only after confirming this exact approved host; this value is NOT a secret.
export MOONBACK_OBSERVATION_ADMIN_URL='https://moonback-observation-archive.london-1976.workers.dev/v1/admin/observations'
python3 observation_admin.py summary
python3 observation_admin.py list --limit 20
python3 observation_admin.py list --limit 20 --cursor '<previous next_cursor>'
python3 observation_admin.py show '<event_id from private list>'
```

These are read-only commands and do not restore Observe or alter saved records.
No secret generation/registration, migration application, deployment, real reads
or real log transfers are performed by this local implementation.
