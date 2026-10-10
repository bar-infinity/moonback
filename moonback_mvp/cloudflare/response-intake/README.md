# MoonBack response Worker

This directory contains the deployed Phase 2A / Phase 2B-1 source and D1 index
migration, plus the local-only Phase 2B-2A management implementation.
`POST /v1/responses` receives validated pending drafts from MoonBack Render.
`GET /v1/responses/status` returns only an anonymous pending signal and cursor.
Both require the `MOONBACK_INTAKE_SECRET` bearer credential and use binding `DB`.

The bearer values are hashed with SHA-256 and every digest byte is compared.
Equal-length digests avoid an early return at the first differing character.
Secrets are never logged or stored in D1. The Worker never fetches `source_url`,
calls OpenAI, accepts arbitrary SQL, or emits custom request logs.

`wrangler.jsonc` identifies `moonback-response-intake` and binds `DB` to
`moonback-responses`. The existing migration creates an index on
`(status, received_at, id)` using `IF NOT EXISTS`. Local validation does not apply
migrations or deploy code. Before any separately approved remote operation,
confirm the selected account, resources and production deployment with:

```bash
npx --no-install wrangler whoami
npx --no-install wrangler d1 list
npx --no-install wrangler deployments list --name moonback-response-intake
```

`MOONBACK_INTAKE_SECRET` is declared as a required secret name only. Its value is
configured separately as a Worker Secret. Never put production secrets in Git,
logs, ordinary variables, `.env`, `.dev.vars`, or Wrangler configuration.
Production intake requires the three explicit Render backend settings. The
Phase 2A setup is already active; local changes do not alter its configuration.

## Phase 2B-2A read-only management (not deployed)

`GET /v1/admin/responses` lists pending summaries, with default `limit=20` and
maximum `limit=50`. Each item contains only `id`, `journal_id`, and `received_at`.
The response has `items` and `next_cursor`. A separate opaque pagination cursor
supports explicit keyset pagination in ascending `(received_at, id)` order.
It is not the public status cursor. Pages are live reads, not a multi-request
snapshot: new arrivals or review status changes can affect subsequent pages.

`GET /v1/admin/responses/<id>` returns `{item: <six draft fields>, status:
"pending"}` only for pending rows. HEAD is supported without a response body.
All management responses use `Cache-Control: no-store` and are capped at 64 KiB.

Management requires a distinct optional Worker Secret named
`MOONBACK_RESPONSE_ADMIN_SECRET`. When missing or equal to the intake secret,
management is disabled. Authentication precedes any D1 query. Intake credentials
cannot authorize management; management credentials cannot authorize intake or
internal status. Unknown/duplicate query parameters, invalid cursors/IDs, and
write methods are rejected. All SQL is fixed SELECT with bound values. Missing
or non-pending rows return 404; D1 failure returns a fixed 503 without details.

There is no management publication, rejection, download-to-draft, status-update
or delete operation. The Worker DB binding itself is not a database read-only
credential; the SELECT-only boundary is enforced by this management code.

Do not put the management secret on Render. No management secret has been
created or configured in this phase. A future approved rollout must register it
and upload/deploy the new version. No production setting is changed locally.

Authenticated management reads have a separate best-effort 60/minute per-isolate
limit. Only recent timestamps are held in memory; no IP, agent identity or
credential is stored. It is not a global distributed quota and makes no D1
writes. It cannot consume MoonBack's intake rate-limit counters.
