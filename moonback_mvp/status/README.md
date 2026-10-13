# Response status snapshot

GitHub Actions will create `response-status.json` here only after a successful
validated public GET. No initial JSON is shipped: an unobserved state must not
be represented as `pending=false`.

The file contains exactly `pending` (boolean) and `cursor` (null or
`sha256:<64 lowercase hex characters>`). It is a last-known successful snapshot,
not a live query or a heartbeat. No response content, raw IDs, agent names,
timestamps, credentials or notification history are stored here.

The workflow runs hourly at minute 17 UTC (JST: every hour at minute 17) and can
also be dispatched manually on `main`. Identical semantic content creates no
commit, including after JSON formatting changes. Failed reads leave the previous
file untouched and fail the Actions run. A missing file means unknown.

## Reading from ChatGPT / Luna

Read this exact file from `bar-infinity/moonback`, branch `main`. Do not use a
search snippet or an old conversation copy as the current snapshot. Connector
indexing can lag; successful GitHub writing does not prove connector freshness.
Verify the connector can actually fetch this file after first deployment.

Notify only when `pending=true` and `cursor` differs from the last successfully
notified cursor. Keep that notification checkpoint in Luna's existing durable
task state, not in this shared snapshot. Retain it across `pending=false` and
record it only after notification succeeds. If the task cannot retain a durable
checkpoint, cross-run duplicate suppression is not guaranteed. No task is
created or modified by this implementation.

Because unchanged signals deliberately produce no commits, the last file commit
date is the time the signal last changed, not the last successful poll. Consult
the workflow's latest successful run for freshness (a suggested initial overdue
threshold is three hours for this hourly schedule). On a failed/overdue run or
unavailable GitHub fetch, report status unknown; do not infer no new responses.
Polling can miss transient signals that disappear between successful polls.

## GitHub and Render safety

The workflow is only for this repository's `main`, with no pull-request trigger.
Global permissions are empty; its sole job gets `contents: write` for the
automatic commit. GitHub does not offer file-path-scoped `GITHUB_TOKEN` contents
permissions. The script itself has no Git operations. The workflow stages only
the fixed snapshot path, verifies that staged path, uses a fixed commit message,
and performs a normal push without retry or force. A concurrent developer push
causes a safe non-fast-forward failure; the next authorized run can catch up.
Workflow concurrency prevents overlapping sync jobs; it cannot lock humans.

Public GET requires no MoonBack/Cloudflare/API secret. Checkout uses GitHub's
automatic short-lived `GITHUB_TOKEN` for repo access and push, not a new PAT.
Do not add Worker secrets to Actions. Checkout is pinned to an official commit.
External JSON never supplies paths, shell arguments, expressions or Git messages.

Render supports `[skip render]` in commit messages; every automatic snapshot
commit includes it. No Render setting or linked branch is changed. This is an
auto-deploy skip, not a prohibition on later manual deployments. Build filters
excluding this path are an optional separately approved safeguard. If an initial
implementation push contains application changes, do not skip its legitimate
deployment without reviewing that whole push first.

## Rollout (separate approval required)

1. Review and commit the local implementation; this step alone does not run it.
2. Approve pushing the workflow to the default `main` branch. A pushed schedule
   may start automatically if repository Actions are enabled.
3. Confirm repository Actions policy allows the pinned checkout and the job's
   `contents: write`; branch rules must allow the bot's ordinary push. Do not
   weaken protections or add a PAT automatically if a push is rejected.
4. Run the workflow once via Actions → Sync public response status → Run workflow.
5. Check the validated JSON, bot commit, and Render's skipped-deploy event.
6. Verify the ChatGPT GitHub connector reads the exact file on `main`; then update
   the existing Luna task's reading instruction and notification checkpoint.
7. Confirm a later unchanged run creates no commit, and failure remains unknown.

Scheduled runs are best-effort and can be delayed/dropped by GitHub. Public-repo
schedules can disable after prolonged repository inactivity. Review Actions
allowances before enabling; this implementation does not purchase services or
change billing settings. To stop polling, disable the workflow manually.

Official references:
- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- https://docs.github.com/en/actions/concepts/workflows-and-actions/concurrency
- https://render.com/docs/deploys#skipping-an-auto-deploy
