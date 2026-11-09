# Response status and freshness monitor

GitHub Actions reads the existing public Cloudflare status endpoint once per run.
No response body, author, raw ID, management URL, credential or notification
history is copied to GitHub. This is Journal response monitoring, separate from
GEO-OBS observation logging.

## Public files

`response-status.json` keeps exactly the existing two fields: `pending` (boolean)
and `cursor` (null or `sha256:<64 lowercase hex characters>`). It changes only on
a successful, validated GET when the signal changes. A failed read leaves its
bytes untouched. If absent, it is unknown, not `pending=false`.

`response-monitor.json` has exactly these seven fields (initial checked-in state):

```json
{
  "schema_version": 1,
  "last_attempt_at": null,
  "last_success_at": null,
  "result": "never_checked",
  "error_category": null,
  "pending": null,
  "cursor": null
}
```

- UTC timestamps use `YYYY-MM-DDTHH:MM:SS.ffffffZ`. `last_attempt_at` is captured
  before the GET; `last_success_at` is captured after validating the response.
- Success sets `result=success`, clears `error_category`, and copies the proven
  signal. Even an unchanged signal refreshes both timestamps in the monitor.
- Fetch/validation failure sets `result=failure`, a new attempt time and a fixed
  error category, preserving the previous success time and signal. Initial
  failure leaves success time, pending and cursor null. Never infer a successful
  time from the legacy file's commit date or filesystem modification time.
- `never_checked` keeps version 1 and that result; its other five fields are null. A
  boolean signal is only valid with a real successful timestamp. Success time
  cannot precede its attempt; a retained success cannot follow a failed attempt.
  Future times, unknown fields, duplicate keys and contradictory data are invalid.

Fixed error categories: `invalid_schema`, `invalid_pending`, `invalid_cursor`,
`inconsistent_signal`, `response_too_large`, `malformed_json`, `redirect_refused`,
`http_error`, `invalid_content_type`, `connection_or_timeout`,
`unsafe_destination`, `invalid_existing_snapshot`, `invalid_existing_monitor`,
`storage_error`, `invalid_timestamp`, `internal_error`, `publication_mismatch`.
Raw provider errors are never used as categories or printed.

## Workflow and failure semantics

The existing hourly minute-17 UTC schedule (also minute 17 in JST) and manual
trigger on `main` remain unchanged. There is one fixed HTTPS GET, no redirect,
retry, authorization or new periodic task. The offline publication validation
step adds no network request.

The read step may fail without skipping publication: the workflow validates the
local JSON pair, commits only the two allowed paths with `[skip render]`, and
then explicitly fails the job if the read failed. A recorded failure therefore
never makes the workflow green. Commit/push/validation failures also fail the job.
A normal push conflict stops without force, rebase or retry. Concurrency prevents
overlapping sync jobs, but cannot lock a human push.

Each file uses an atomic replacement; the pair is not a filesystem transaction.
The publication gate rejects a partial or contradictory pair before staging.
Corrupt prior metadata is not silently overwritten. Disk failure, cancellation,
checkout failure, denied Git permissions or push conflict may prevent even the
failure metadata from reaching GitHub. The remote files then remain old: use the
freshness deadline and Actions conclusion, never assume no pending responses.
A partial local pair requires inspection rather than automatic repair.

Global workflow permissions remain empty; the one job uses `contents: write`.
GitHub does not provide file-path-scoped contents permission. Staging is limited
to the two fixed JSON paths and unexpected staged files abort publication.
Checkout remains pinned and uses GitHub's short-lived automatic `GITHUB_TOKEN`,
not a new PAT. External JSON never supplies shell code, paths or commit messages.

Successful runs now create heartbeat commits even when pending/cursor are
unchanged; failure attempts also update metadata. Every automatic commit includes
`[skip render]` to skip Render Auto-Deploy. This does not prohibit manual deploys.
No Render setting or build filter is changed. Standard public-repository Actions
runners require no new service; existing account allowances/policies still apply.
Schedules are best-effort and may be delayed, dropped or disabled after inactivity.
A workflow that never starts cannot record an attempt time or failure metadata.
`last_attempt_at` records an actual script attempt, not a scheduled slot;
the three-hour freshness rule must detect this gap as stale or unknown.

## Reading from ChatGPT / Luna

Read the exact `response-monitor.json` on `bar-infinity/moonback`, branch `main`,
not a search snippet or conversation copy. Connector indexing may lag: a GitHub
push does not prove that ChatGPT retrieved the latest revision. The legacy signal
alone cannot establish freshness. Reader rules are implemented as pure,
side-effect-free `monitor_state()` and `notification_decision()` functions in
`scripts/sync_response_status.py`; they do not create or change a Luna task.

1. Retrieval failure, invalid JSON/schema/timestamps, future dates,
   `never_checked`, or `result=failure`: **unknown / unable to confirm**.
2. Valid `result=success` with success age at most three hours: **healthy**.
3. Valid success older than three hours: **stale**, never "no new responses".
4. Healthy `pending=false`: "No pending responses at the last successful check."
5. Healthy `pending=true` without an established notification checkpoint:
   "Pending responses on the initial check" (`initial_pending`).
6. Healthy `pending=true` with a cursor absent from the supplied notified-cursor
   set: a **new candidate**. An already notified cursor suppresses a duplicate.

The caller supplies a set of successfully notified cursors. `None` means no
checkpoint; an empty set means initialized history with no notifications. Keep
history across `pending=false`, and update it only after successful notification.
Notification history is not human review, approval or read status. Do not infer
that any response has been reviewed from an alert or absence of an alert.

**Whether the actual Free-Roaming Luna task durably retains notified cursors is
unverified.** No automation, memory store or notification checkpoint is configured
by this change. Cross-run deduplication requires separately verifying that state.
A crash between notification and checkpoint storage can cause repeat alerts.

Cursor hashes the latest received response ID across statuses, not the current
pending set. Review/status changes alone do not change it. It is neither a list
nor a per-message read marker: multiple arrivals between polls can collapse into
one signal, and pending items can be missed if they disappear between polls.
Deleting/restoring rows can revert a cursor; retaining notified-cursor history
reduces repeats but cannot guarantee delivery or detect every individual reply.
Opaque cursors are not credentials or proof of an AI author's identity.

## Rollout (separate approval required)

1. Review and commit this implementation; local tests do not poll production.
2. Separately approve pushing/merging the workflow to the default `main` branch.
   An enabled schedule may run once merged; do not dispatch it during local work.
3. Verify repository policies allow the pinned checkout and ordinary bot push.
   Do not weaken protections or add credentials automatically if rejected.
4. With approval, run once and verify the seven-field monitor, unchanged legacy
   format, Actions conclusion, `[skip render]` commit and skipped Render deploy.
5. Verify the ChatGPT connector can retrieve the exact current monitor; update
   the existing Luna reading instructions only after separate authorization.
6. Verify checkpoint persistence before claiming durable duplicate suppression.

Official references:
- https://docs.github.com/en/actions/reference/workflows-and-actions/contexts
- https://docs.github.com/en/actions/reference/workflows-and-actions/expressions
- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- https://render.com/docs/deploys#skipping-an-auto-deploy
