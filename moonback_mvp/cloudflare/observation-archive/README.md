# Private observation archive — local prototype

Standalone Worker and D1 schema; not the Journal response Worker/database.
No production configuration, deployment, database ID or secret is included.
The runtime entry is `src/index.js`; binding `OBS_DB` must point ONLY to a new
observation database. Apply `migrations/0001_observations.sql` there after approval.
Set Worker Secret `MOONBACK_OBSERVATION_INGEST_SECRET` separately after approval.
Never use either response intake/admin credential here.

Only authenticated `POST /v1/observations` exists. All GET/HEAD and other paths
return fixed 404. No public read route, tools, outbound fetch or arbitrary SQL.
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
