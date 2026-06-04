# Outreach Magic skill suite

Three intentional skills — not a 50-skill dump. **Outreach Magic is category 4: data infrastructure.** Strategy and copy skills stay stateless; OM is persistence.

> Every other GTM skill tells your agent what to write. Outreach Magic tells your agent what's happening.

## Funnel

```mermaid
flowchart LR
  LE[lead-enrich]
  OM[outreachmagic]
  EF[email-finder]
  LE -->|"dedup free; save needs OM"| OM
  OM -->|"domain + linkedin on file"| EF
  EF -->|"apply-email-find-results or import-profiles"| OM
```

| Skill | Role | Public repo | Release tag |
|-------|------|-------------|-------------|
| **outreachmagic** | Data layer — pipeline, relay, SQLite | `outreachmagic/outreachmagic` | `v*` |
| **lead-enrich** | Discovery — Serper, LinkedIn, domain | `outreachmagic/lead-enrich` | `lead-enrich-v*` |
| **email-finder** | Email find (trykitt → Icypeas) + optional MV verify | `outreachmagic/email-finder` | `email-finder-v*` |

## Install order

1. **outreachmagic** — `pipeline.py init` then `pipeline.py login` in terminal  
2. **lead-enrich** — add `SERPER_API_KEY` to `~/.hermes/.env`  
3. **email-finder** (optional) — `TRYKITT_API_KEY` and/or `ICYPEAS_API_KEY`; needs domain. Batch OM save is fastest with **`lead_id` + `--workspace`** (pipeline ≥ v1.25.9, email-finder ≥ v2.2.3).  

**Canonical install commands (Hermes, Cursor, Claude):** [install-companions.md](./install-companions.md)

## Soft dependency

- **lead-enrich** and **email-finder** work without outreachmagic for JSON/API helpers, but **dedup + save require OM**.
- `check` / `batch-check` exit with a clear error if outreachmagic is missing — Serper paths still work when OM is installed elsewhere.

## Freemium

| Free forever (no relay count) | Counts as relay event |
|------------------------------|------------------------|
| Local pipeline queries | Webhook events synced from sequencers |
| `import-profiles`, `apply-email-find-results`, export | |
| lead-enrich dedup (`check`) | |
| email-finder OM pre-check | |
| `verify-email` recording | |

Launch limits: **1,000 relay events/mo free**, **Pro $9/mo** (50k cap). See [positioning/pricing.md](./positioning/pricing.md).

## Naming: find vs verify

- **`email_finder.py`** — find (`find`, `batch-find`) via trykitt / Icypeas; optional **`verify*`** via MillionVerifier.
- **`pipeline.py verify-email`** — writes verification status to SQLite (provider-agnostic).
- **`pipeline.py verification-candidates`** — lists workspace emails due for re-verify (used by `verify-bulk`).

## related_skills (Hermes frontmatter)

- outreachmagic → `[lead-enrich, email-finder]`
- lead-enrich → `[outreachmagic, email-finder]`
- email-finder → `[outreachmagic, lead-enrich]`

## Release docs

- [RELEASING.md](./RELEASING.md) — tags and CI  
- [registry-publish.md](./registry-publish.md) — marketplace order  
