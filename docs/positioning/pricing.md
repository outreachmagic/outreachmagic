# Outreach Magic — Pricing (launch)

> Maintainer source of truth. Hub copy and `SKILL.md` should match this doc (or the live portal if it differs).

## Plans

| Plan | Relay events/mo | Workspaces | Sequencer sync | Price |
|------|-----------------|------------|----------------|-------|
| **Free** | 1,000 | 1 | Manual pull only | $0 |
| **Pro** | 50,000 | Unlimited | All integrations | **$9/mo** |

Pro is capped at 50k relay events (not “unlimited”) — high enough that normal users never think about it.

## What never counts toward relay limits

- Hermes-originated tracking (`log-event`, `add-lead`, `update-stage`, etc.)
- Local queries (`show`, `stats`, `campaigns`, `history`)
- Import/export (`import-profiles`, `export-local`, `export`)
- Personalization store
- Email verification **recording** (`verify-email`)
- **lead-enrich dedup checks** (`enrich.py check` / `batch-check`) — local SQLite only
- **lead-email pre-checks** before trykitt — local SQLite only

Only **relay-synced webhook events** from connected sequencers count toward the monthly limit.

## Third-party costs (not OM)

| Service | Skill | User pays |
|---------|-------|-----------|
| Serper.dev | lead-enrich | User's Serper key |
| trykitt.ai | lead-email | User's trykitt key |

Skills are MIT / free to install. Pro is for OM relay infrastructure at [outreachmagic.io](https://outreachmagic.io).

## Setup

Agent connect (not chat): `python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py login`

Portal: [dev.outreachmagic.io/setup/agent](https://dev.outreachmagic.io/setup/agent)
