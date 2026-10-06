# MoonBack response intake Worker

This directory contains the undeployed Phase 2A Worker source and D1 index
migration. The Worker accepts only `POST /v1/responses` from MoonBack Render,
checks the `MOONBACK_INTAKE_SECRET` bearer credential, validates the fixed draft
schema and inserts a pending row through the `DB` binding.

The bearer values are hashed with SHA-256 and every digest byte is compared.
Using equal-length digests avoids an early return at the first differing secret
character. The secret itself is never logged or stored in D1.

Before a future deploy, configure the existing `moonback-responses` database as
the `DB` binding and add `MOONBACK_INTAKE_SECRET` as a Worker secret. Do not put
its value in a Wrangler file. Apply `migrations/0001_status_index.sql` separately;
`IF NOT EXISTS` makes repeated application safe.

The Worker does not expose review, publication or arbitrary SQL endpoints. It
does not fetch `source_url`, call OpenAI, or emit custom request logs.

`wrangler.jsonc` identifies the existing `moonback-response-intake` Worker and
binds `DB` to the existing `moonback-responses` D1 database. Treat this file as
the deploy configuration source of truth and compare its Worker name, database
name and database ID with the Cloudflare dashboard before any remote command.

`MOONBACK_INTAKE_SECRET` is declared as a required secret name only. Set its
value separately as a Cloudflare Worker Secret. Never write the production
value to `wrangler.jsonc`, Git, logs, `.env`, or `.dev.vars`; `.dev.vars` is for
disposable local development values only and is not a production secret store.

Before a future migration, version upload, or deployment, confirm the selected
Cloudflare account and existing resources with read-only commands:

```bash
npx wrangler whoami
npx wrangler d1 list
npx wrangler deployments list --name moonback-response-intake
```

These commands are not part of local setup and require an explicitly authorized
Cloudflare connection. Production response intake remains off until the Worker
has been deployed and MoonBack Render is separately configured with all three
Cloudflare backend environment variables.
