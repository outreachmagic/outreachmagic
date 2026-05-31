# Skill install paths (launch)

**Do not migrate to `~/.hermes/skills/gtm/` at launch.** Hub category is metadata only.

```
~/.hermes/skills/
├── outreachmagic/     # data layer — SQLite, pipeline.py
├── lead-enrich/       # Serper research + dedup
└── email-finder/        # trykitt find + save
```

Profiles symlink: `~/.hermes/profiles/<name>/skills/<skill>` → `../../../skills/<skill>/`

Install: `platforms/hermes/install.sh` (optional `--with-lead-enrich`, `--with-email-finder`).

Dev sync: `bash scripts/sync-local.sh`

See [../skill-suite.md](../skill-suite.md) for funnel and freemium rules.
