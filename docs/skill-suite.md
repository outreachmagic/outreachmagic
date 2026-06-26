# Outreach Magic skill suite

Three intentional skills — not a 50-skill dump. **Outreach Magic is category 4: data infrastructure.** Strategy and copy skills stay stateless; OM is persistence.

**Machine config** (manifest paths, install pins, `install_required`): [`skill-suite.json`](../skill-suite.json) at the repo root. Regenerate manifests with `make manifests`; pre-tag gate: `make release-check`.

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

1. **outreachmagic** — `pipeline.py init` then `pipeline.py login` (browser device auth)  
2. **lead-enrich** — enable Serper in the portal; keys sync via `pipeline.py sync-secrets`  
3. **email-finder** (optional) — enable TryKitt/Icypeas in the portal; batch OM save needs **`lead_id` on every row + `--workspace`**.  

**Canonical install commands (Hermes, Cursor, Claude):** [install-companions.md](./install-companions.md)

## Soft dependency

- **lead-enrich** and **email-finder** work without outreachmagic for JSON/API helpers, but **dedup + save require OM**.
- `check` / `batch-check` exit with a clear error if outreachmagic is missing — Serper paths still work when OM is installed elsewhere.
- Both companions stamp **`{provider}_attempted`** tags on save (`serper_attempted`, `trykitt_attempted`, `icypeas_attempted`) so re-runs skip already-processed leads.

## Freemium

| Free forever (no metered count) | Counts as webhook/sync event |
|------------------------------|------------------------|
| Local pipeline queries | Webhook events synced from sequencers |
| `import-profiles`, `apply-email-find-results`, export | |
| lead-enrich dedup (`check`) | |
| email-finder OM pre-check | |
| `verify-email` recording | |

Launch limits: **1,000 webhook events/mo free**, **Pro $9/mo** (50k webhook and sync events). See [outreachmagic.io/pricing](https://outreachmagic.io/pricing).

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
- [outreachmagic.io/pricing](https://outreachmagic.io/pricing) — pricing and plans  
