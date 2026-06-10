# Outreach Magic — dev ecosystem

Private repos under `magic-creators/`. Clone as siblings under `~/Developer/` (or adjust paths in `outreach-magic.code-workspace`).

## Surfaces (what users see)

| URL | Repo | Stack | Purpose |
|-----|------|-------|---------|
| [outreachmagic.io](https://outreachmagic.io) | `outreach-magic-site` | Vite + React (SSR prerender) | **Marketing** — home, pricing, features, docs, blog, SEO |
| [app.outreachmagic.io](https://app.outreachmagic.io) | `wbhk-app` | Next.js + Prisma + Neon | **Portal** — auth, billing, agent keys, review sheets API, dashboard |
| [api.outreachmagic.io](https://api.outreachmagic.io) | `wbhk-worker` | Cloudflare Worker + D1 | **Relay** — webhooks, push/pull, usage limits |

## Agent install (what users download)

| Repo | Public? | Purpose |
|------|---------|---------|
| `outreachmagic-skill` (this repo) | Private dev monorepo | Skills source, tests, dark-factory, release CI |
| [outreachmagic/outreachmagic](https://github.com/outreachmagic/outreachmagic) | Public | `install.sh`, `pipeline.py update` target |
| [outreachmagic/lead-enrich](https://github.com/outreachmagic/lead-enrich) | Public | Companion skill |
| [outreachmagic/email-finder](https://github.com/outreachmagic/email-finder) | Public | Companion skill |

## Brand & GTM (strategy — not implementation)

| Repo | Purpose |
|------|---------|
| `outreachmagic-brand` | **Positioning, copy, launch, analytics playbooks** — single source of truth |

## Shared libraries

| Repo | Consumed by |
|------|-------------|
| `wbhk-billing` | `wbhk-app`, `wbhk-worker` — plan limits, usage policy |

## Where to edit what

| Change | Repo | Key paths |
|--------|------|-----------|
| Positioning, marketplace copy, brand voice, GTM | `outreachmagic-brand` | [README](../outreachmagic-brand/README.md) — see also [brand.md](./brand.md) in each repo |
| Skill CLI, SQLite pipeline, review export/sync | `outreachmagic-skill` | `skills/outreachmagic/scripts/` |
| Website hero, pricing page, blog, `/docs` | `outreach-magic-site` | `src/pages/`, `src/content/` — implement from `outreachmagic-brand` |
| Portal dashboard, review API, agent setup | `wbhk-app` | `src/app/`, `src/lib/review*.ts` — copy from `outreachmagic-brand/copy/portal/` |
| Relay limits, webhook ingest | `wbhk-worker` | `src/` |
| Free/Pro/Agency event caps (enforced) | `wbhk-billing` | `src/plans.ts` |

## Cursor workspace

Open all repos at once:

```bash
cursor outreachmagic-skill/outreach-magic.code-workspace
```

Or: **File → Open Workspace from File** → `outreach-magic.code-workspace`

## Marketing vs portal (don't mix them up)

- **Strategy & drafts** live in `outreachmagic-brand` — pricing narrative, hub copy, voice, launch plans.
- **Marketing implementation** lives in `outreach-magic-site` (outreachmagic.io).
- **Portal implementation** lives in `wbhk-app` (app.outreachmagic.io) — signup, billing, agent setup wizard.
- **Pricing narrative:** `outreachmagic-brand/product/pricing.md`. **Enforced limits:** `wbhk-billing/src/plans.ts`.
