# Registry publish checklist

Separate listings per skill. Skills are free (MIT); Pro is OM account only at [outreachmagic.io](https://outreachmagic.io).

## Priority order

| # | Channel | Slugs |
|---|---------|-------|
| 1 | [Hermes Hub](https://github.com/amanning3390/hermeshub) | `outreachmagic`, `lead-enrich`, `email-finder` |
| 2 | [skills.sh](https://skills.sh) | Same three |
| 3 | Agensi, MCP Market, ColdIQ directory | As bandwidth allows |
| 4 | ClawHub | After copy stable |

## Per-listing requirements

- One SEO link: https://outreachmagic.io  
- Setup: https://app.outreachmagic.io/setup/agent  
- Differentiation one-liner from [positioning/hub-copy.md](./positioning/hub-copy.md)  
- Pricing from [positioning/pricing.md](./positioning/pricing.md)  
- SkillScan strict pass before tag (see `scripts/skill-scan.sh`)

## Hermes Hub — Reviewed Domains

| Skill | Domains |
|-------|---------|
| outreachmagic | `api.outreachmagic.io`, `app.outreachmagic.io` |
| lead-enrich | `google.serper.dev` |
| email-finder | `api.trykitt.ai` |

Issue templates: `docs/hermeshub-reviewed-domains-*.md`

## GitHub release tags (monorepo → public repo)

| Skill | Monorepo tag | Public repo |
|-------|--------------|-------------|
| outreachmagic | `v1.20.x` | `outreachmagic/hermes-outreachmagic` |
| lead-enrich | `lead-enrich-v2.0.0` | `outreachmagic/lead-enrich` |
| email-finder | `email-finder-v1.0.0` | `outreachmagic/email-finder` (create repo) |

CI: `.github/workflows/publish-platforms.yml`, `publish-lead-enrich.yml`, `publish-email-finder.yml`

## Filesystem (no category migration)

`~/.hermes/skills/{outreachmagic,lead-enrich,email-finder}` — see [positioning/skill-path.md](./positioning/skill-path.md).
