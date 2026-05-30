# Registry publish checklist

Separate listings per skill. Skills are free (MIT); Pro is OM account only at [outreachmagic.io](https://outreachmagic.io).

## Priority order

| # | Channel | Slugs |
|---|---------|-------|
| 1 | [Hermes Hub](https://github.com/amanning3390/hermeshub) | `outreachmagic`, `lead-enrich`, `lead-email` |
| 2 | [skills.sh](https://skills.sh) | Same three |
| 3 | Agensi, MCP Market, ColdIQ directory | As bandwidth allows |
| 4 | ClawHub | After copy stable |

## Per-listing requirements

- One SEO link: https://outreachmagic.io  
- Setup: https://dev.outreachmagic.io/setup/agent  
- Differentiation one-liner from [positioning/hub-copy.md](./positioning/hub-copy.md)  
- Pricing from [positioning/pricing.md](./positioning/pricing.md)  
- SkillScan strict pass before tag (see `scripts/skill-scan.sh`)

## Hermes Hub — Reviewed Domains

| Skill | Domains |
|-------|---------|
| outreachmagic | `api.outreachmagic.io`, `dev.outreachmagic.io` |
| lead-enrich | `google.serper.dev` |
| lead-email | `api.trykitt.ai` |

Issue templates: `docs/hermeshub-reviewed-domains-*.md`

## GitHub release tags (monorepo → public repo)

| Skill | Monorepo tag | Public repo |
|-------|--------------|-------------|
| outreachmagic | `v1.20.x` | `outreachmagic/hermes-outreachmagic` |
| lead-enrich | `lead-enrich-v2.0.0` | `outreachmagic/lead-enrich` |
| lead-email | `lead-email-v1.0.0` | `outreachmagic/lead-email` (create repo) |

CI: `.github/workflows/publish-platforms.yml`, `publish-lead-enrich.yml`, `publish-lead-email.yml`

## Filesystem (no category migration)

`~/.hermes/skills/{outreachmagic,lead-enrich,lead-email}` — see [positioning/skill-path.md](./positioning/skill-path.md).
