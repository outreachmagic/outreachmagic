# CRM Sync

Push Outreach Magic pipeline data (leads, deals, event history) into external CRMs. Ships with GoHighLevel and HubSpot drivers. Salesforce support is planned.

Internal specs and roadmap are maintained in a private repository.

## Architecture

```
pipeline.py (update-stage, log-event)
       |  --crm-sync flag
       v
crm_sync.py (orchestrator)
       |
       ├── crm_drivers/ghl.py
       ├── crm_drivers/hubspot.py
       └── crm_drivers/salesforce.py (future)
       |
       v
Local SQLite (crm_workspace_config, crm_entity_map, crm_sync_log)
       ^
       | pull
       |
Relay (encrypted crmIntegrations on Organization)
       ^
       | PUT /api/workspace/{orgId}/crm-integrations
       |
Web App UI
```

## CLI Commands

```bash
python3 scripts/crm_sync.py sync --workspace popcam
python3 scripts/crm_sync.py sync --all
python3 scripts/crm_sync.py sync --all --dry-run
python3 scripts/crm_sync.py sync --lead-id 5 --workspace popcam
python3 scripts/crm_sync.py sync --all --skip-events
python3 scripts/crm_sync.py discover --workspace popcam
python3 scripts/crm_sync.py status
python3 scripts/crm_sync.py discover --workspace popcam --platform ghl
```

## Data Flow

### Trigger paths:
1. **Manual:** `python3 scripts/crm_sync.py sync --all`
2. **Agent-driven:** `update-stage --crm-sync` or `log-event --crm-sync` in pipeline.py
3. **Scheduled:** Cron job (`0 */6 * * *`)

### Per-lead sync flow:
1. Read `crm_workspace_config` for workspace + platform
2. Check `crm_entity_map` for existing CRM IDs + hash
3. If hash unchanged → skip entirely (zero API calls)
4. Look up or create contact in CRM (by email)
5. Look up or create deal in CRM (by contactId + pipeline)
6. Push event history (since `last_event_id_synced` cursor)
7. Write/update `crm_entity_map` with CRM IDs + cursor + hash
8. Log result to `crm_sync_log`

## Field Mapping

### Contact fields synced

| OM field | GHL | HubSpot |
|----------|-----|---------|
| `name` | `name` (always) | `firstname` + `lastname` (auto-split) |
| `email` | `email` (always) | `email` (always) |
| `company` / `company_name` | `companyName` (always, as text on contact) | `company` (always, as text on contact) |
| `title` | Custom field (requires `contact_field_mapping`) | `jobtitle` (automatic) |
| `industry` | Custom field (requires mapping) | `industry` (automatic) |
| `headcount` | Custom field (requires mapping) | `numemployees` (automatic) |
| `linkedin_url` | Custom field (requires mapping) | `linkedinbio` (automatic) |
| `company_domain` | Custom field (requires mapping) | `website` (automatic) |

### GHL custom field mapping

GHL only has native fields for name, email, and company. All other fields require custom field IDs configured in `contact_field_mapping`:

```json
{
  "title": "L0HmDyPLKZ6sBbXF9IQm",
  "linkedin_url": "vZE4qt3g21OBtptWTWNH",
  "company_domain": "eNaWfYkRBL09psTOXSd8"
}
```

Values are GHL's opaque custom field IDs (not merge tags like `{{contact.linkedin}}`). The `locations/customFields.readonly` + `locations/customFields.write` PIT scopes are required for custom field operations.

Fields not listed in the mapping are silently dropped. A coverage log is printed during sync:

```
[crm-sync] GHL fields for Popcam: ✓ title, ✓ linkedin_url, ✗ industry, ✗ headcount
```

### No-overwrite mode

By default, when updating an existing contact, fields that already have a non-empty value in the CRM are **not overwritten**. This preserves any manual edits the user made in their CRM.

- **Always updated:** `name`, `email`, `company`/`companyName` (contact identifiers)
- **Checked before overwriting:** All other fields (title, industry, linkedin_url, etc.)
- **Empty OM values:** If OM has no value for a field (e.g., no linkedin_url), the CRM value is preserved

To force-overwrite all CRM fields with OM values, set `overwrite_existing: true` (configurable via the web app UI).

## Company Handling

Companies are created as first-class CRM objects and associated to contacts.

### Sync flow

1. `sync_company()` runs **before** contact sync for each lead
2. If the entity map already has a `crm_company_id`, it's reused (no API call)
3. Otherwise, the driver searches by domain/name and creates if not found
4. The resulting company ID is stored in `crm_entity_map.crm_company_id`
5. Contact create/update associates the contact to the company
6. Deal create/update associates the deal to the company (HubSpot only)

### GHL — Business entity

| Aspect | Details |
|--------|---------|
| API | `POST /businesses/`, `GET /businesses/` |
| Search | By name in location (`GET /businesses/?locationId=...`) |
| Contact link | `POST /contacts/`: auto-associates by `companyName` (read-only). `PUT /contacts/{id}`: explicit `businessId` accepted |
| Required scopes | `businesses.write`, `businesses.readonly` |
| Note | After adding scopes to an existing PIT, save the integration — scopes take effect immediately without regeneration |

### HubSpot — Company object

| Aspect | Details |
|--------|---------|
| API | `POST /crm/v3/objects/companies`, search via `/crm/v3/objects/companies/search` |
| Search | By domain (`propertyName: "domain", operator: "EQ"`) |
| Contact link | Association type `279` (contact-to-company) on create, PUT on update |
| Deal link | Association type `6` (deal-to-company) |
| Required scopes | `crm.objects.companies.write`, `crm.objects.companies.read` |
| Indexing delay | HubSpot search API has ~10-60s delay after creation; entity map handles in-sync dedup |

### Mapped fields

| OM companies table | HubSpot property | GHL business field |
|-------------------|-----------------|-------------------|
| `name` | `name` | `name` |
| `domain` | `domain` | Not directly (via `website`) |
| `industry` | `industry` (enum) | Not mapped |
| `phone` (from lead) | `phone` | `phone` |
| `hq_city` | `city` | `city` |
| `hq_state` | `state` | `state` |
| `hq_country` | `country` | Not mapped |
| `domain` / `company_domain` | `website` | `website` |

## Event History

Event history pushes to CRM using cursor-based tracking:

| Event type | GHL | HubSpot |
|-----------|-----|---------|
| `email_sent` (outbound) | Note: `[Sent] Subject` | Email object + Note |
| `reply` (inbound) | Note: `[Replied] Preview` | Email object + Note |
| `bounce` | Note: `[Bounced] Reason` | Note |
| `meeting_booked` | Note: `[Meeting] Details` | Note |
| `interested` / `not_interested` | Note: `[Interested]` / `[Not Interested]` | Note |
| `stage_change` | Note: `[Stage] from → to` | Note |
| Unknown event types | Note: `[Event Type]` | Note |

Cursor-based tracking via `wle.rowid` prevents duplicate events. At-least-once delivery — crash between push and cursor commit could produce duplicate notes (cosmetic, not data-corrupting).

Use `--skip-events` for contact/deal-only sync.

## Rate Limiting

| Driver | Limit | Strategy |
|--------|-------|----------|
| GHL | 80 req / 10s | Token bucket, exponential backoff |
| HubSpot | 400 req / 10s | Token bucket, exponential backoff |

## Key Design Decisions

- **Subprocess invocation from pipeline.py** — avoids import conflicts, keeps crm_sync.py standalone
- **Entity map for dedup** — CRM IDs stored locally, even survives `refresh --yes` via cloud_pending relay sync
- **Sync hash for change detection** — unchanged leads make zero API calls
- **Event history via rowid cursor** — tracks last synced event rowid per lead, not timestamp (avoids clock skew)
- **No-overwrite by default** — preserves manual CRM edits. Configurable via `overwrite_existing` flag
