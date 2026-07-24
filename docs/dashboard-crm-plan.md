# Dashboard CRM Expansion — Build Plan

Big plan, broken into foundations + sections. Each section ships to the Hermes
install so you can test it live before the next one starts. Decisions already
locked in from brainstorming are marked **[decided]**.

## Progress (resume here)

Durable state lives in files, not chat: this doc + `dashboard_*.py` + `dashboard.html`
+ tests + CHANGELOG. To resume in a fresh session, read this checklist and the
listed files, then continue at the first unchecked item.

- [x] Section A — date-range correctness (pipeline/campaign active-in-range)
- [x] F2 — server-side lead search (`search_leads`, `campaign_leads`)
- [x] Section B — Contacts tab
- [x] Section F — campaign detail = leads (senders removed)
- [x] F1 — edit actions + generic edit panel (`dashboard_actions`: update_company,
      link_company, edit_sender_account, edit_sender_domain, resolve_merge_candidate)
- [x] Section C — Companies tab (search, detail, multi-domain, merge review, link control)
- [x] Section E — deliverability age filters + sender/domain edit
- [x] Section D — data quality + enrichment (buckets, one-click link, email-finder
      multi-domain, Serper research job, junk cleanup). Note: Serper's terminal
      map-to-fields step stays agent-in-the-loop — the job runs the query pack and
      returns formatted result blocks in the sync status for the agent to act on.
- [x] Section G — activity search across full range (event-type + text over
      name/email/domain/LinkedIn/company/company-domain; honors the date range)
- [x] Section H — sync item detail panel (outbox row → slide-over: queued ops,
      resolved live record, and the exact payload the push will send, via the
      real sync_contract payload builders)
- [ ] Section I — design polish

Live dashboard runs from Hermes: `python3 ~/.hermes/skills/outreachmagic/scripts/pipeline.py dashboard`.
Ship loop per section: edit `dashboard_*` → tests → `make manifests` → `bash scripts/sync-local.sh` → restart on :8765.

## Locked decisions

- **Date semantics [decided]:** 
This is a good start, now help me brainstorm on how we can implement these additional changes -there are a lot of prospecting contacts that just iso cup with ‘unknown’ no job title, no email no nothing -thre are a lot of contacts wit missing companies, can we have a robust way to be able to link it companies —the company name and any company attributes should come from the company table and we need a way to be Abel to link to those companies —can we have a way to show all contacts and all companies like a CRM if they wanted to update a contact or company in the system, you should be able to click on it and then see the full information for that contact / company -wuth the sync tab it should have more information if you click on ones of hte items that are pending to delete eo update in sync for example it be Abel to click on it and show a right panel come up with more details for that specific item
-with the attributes tab you should be able to click on any of the attributes an then for it to list all of the leads associated with tat attribute and then you should be able to click on that lead and then on the right panel it shows the full history and other attributes like ht other sidebar we have in pipeline -when you click on a campaign in in campaign It should load all of the leads associated with that campaign not the senders, the senders should be specific to hte deliverability tab, we want to know more about the leads with all other tabs, also teh dates are not applying correctly in teh campaigns for the senders we need to make sure the dates apply correcty when its set for the lead -teh dates are not applying correctly on the pipeline tab it should only be showing items within the specific date range for the pipeline section not all dates, all tabs shoudl be specific to that timeframe -on the deliverability tab there eshould be an easy way to disable on all the senders / domains that have never sent or haven’t sent in the last 3 months, 6 months 12months+ -with the contacts / companies tabs it shoudl be easy to find specific camopnies / leads based on any attributes linked email, domain, linekdin url, name, company name -with the activity tab its tricky because the filter is for teh event type and its not noted anywhere should be Abel to also search by name, email, linkedin url, compay name, company domain, and it also tricky that it only searches however many items on our hte page visible, it should search within the entire date range that is setup above IE: range last 90 days -I really liek the mimimal design, but I feel like we can do a little more with teh design adding some other colors or items to keep ti very mimimal still but make it easier on the eye and easier to navigate. -on the deliverability page you should be Abel to click on a sender account or sender domain and to be Abel to edit those items like the reseller, name, provider, cost, etc.. this whole page is a bit overwhelming, I think ti can be organized a lot better and haev a right side bar for editing those items similar to the pipeline page.when a range is set, current-state tabs show
  **leads active in that range** (counted by their current stage). Applies
  everywhere — pipeline, campaigns, attributes.
- **Stale senders [decided]:** **filter only** (never-sent / 3 / 6 / 12mo).
  No disable flag, no status write, nothing that syncs to the sequencer.
- **Unknown/under-enriched leads [decided]:** data-quality view + one-click
  company link + trigger email-finder + trigger Serper research + cleanup of
  the truly-empty (event-less) subset.
- **Enrichment must be company/domain-aware [decided]:** a company can have
  multiple domains; the finder must know which domain(s) it's using.
- **Companies are multi-domain [decided]:** UI shows every domain/branch and
  surfaces merge/ambiguity candidates.

## Reuse map (functions that already exist — do NOT reimplement)

| Need | Existing function | File |
|---|---|---|
| Edit lead attributes | `enrich_lead` | pipeline.py:2853 |
| Link lead → company | `link_lead_company` / `ensure_company` | pipeline.py:1419 |
| Edit company fields | `_update_company_fields` (wrap it) | pipeline.py:1184 |
| Company domains/branches | `company_identities` (type=`domain`) | schema |
| Merge review queue | `list_company_merge_candidates`, `approve_company_merge_candidate`, `reject_company_merge_candidate`, `merge_companies`, `list_merge_proposals` | pipeline.py:2181+ |
| Edit sender account | `update_sender_account` | pipeline_sender_accounts.py:312 |
| Edit sender domain | `set_sender_domain_cost` | pipeline_sender_accounts.py:594 |
| Email finder (domain+company) | `email_finder.cmd_find(name, domain, company, …)` | email_finder.py:360 |
| Serper research flow | `enrich.check_lead_exists` / `batch_check` / serper_* | enrich.py |
| Safe junk cleanup | `cleanup_junk_leads(dry_run, confirm)` | junk_cleanup.py:124 |
| Stage/log write path | `lead_actions.*` (already built) | lead_actions.py |
| Background job runner | `dashboard_actions.SyncManager` (extend it) | dashboard_actions.py |

All writes route through these, so `trg_outbox_*` triggers queue them for relay
push automatically.

---

## Foundations (build first — the rest is wiring on top)

**F1. Universal detail slide-over.** Generalize the pipeline slide-over into one
component driven by a "detail spec": header, field groups, edit form(s), and
related-record lists. Instances: lead (have it), **company**, **sender account**,
**sender domain**, **sync/outbox item**. Every "click for detail" in the app uses
this — one look, one behavior.

**F2. Server-side searchable/paginated table.** Replace the client-only filter
(which only sees the current page) with a real search contract:
`?q=…&fields=…&limit=…&offset=…&sort=…`, querying the **whole range**. Shared by
Contacts, Companies, Activity, campaign-leads, pipeline drill-downs. Search
fields: name, email, email_domain, linkedin_url, company, company_domain.

**F3. "Active in range" lead-set helper.** One query fragment: "leads with a
`workspace_lead_events` row in [since, until]". Every current-state tab filters
its counts/lists through it, so ranges apply uniformly.

**F4. Design system pass.** Minimal but easier on the eye: one accent color for
active nav + section headers, status colors (good/warning/critical from the
dataviz palette) for health/bounce/sync/verification states, a consistent
card/table grid, and the single shared slide-over. Applied incrementally, tuned
last.

---

## Section A — Date-range correctness (bug fixes)

- Pipeline stages → count **leads active in range** (F3), not all-time.
- Campaign totals, **senders**, subjects, and daily matrix all honor range
  (senders currently use the no-range path — straight bug).
- Attributes tab already range-agnostic by nature; add "active in range" gating
  so it matches.
- Each current-state view gets a one-line label stating its range semantics.

## Section B — Contacts tab (CRM)

- Server-side lead search (F2) across all attributes, paginated + sortable.
- Row click → detail slide-over (F1): full attributes, **edit** (`enrich_lead`),
  event history (already built), and a **company link control**.
- When a lead is linked, show company name/industry/headcount from the
  **companies** table (authoritative), with the lead's denormalized `company`
  text as fallback only.

## Section C — Companies tab (CRM) + multi-domain + merge review

- Company search + paginated list (name, domain, industry, # leads).
- Detail slide-over: company attributes with **edit** (wrap
  `_update_company_fields` into an explicit setter), **all domains/branches**
  from `company_identities` (each with role, verified flag; mark primary), and
  the **associated leads** list (click-through to a lead).
- **Needs-review panel:** the 2,316 `pending` `company_merge_candidates`, each
  showing the two candidates + reason, with **Approve / Reject** inline
  (`approve_company_merge_candidate` / `reject_company_merge_candidate`) and a
  manual `merge_companies` action. This is the "easily see what needs merging or
  is unsure" surface.
- Company linking initiated from Contacts resolves here (search → pick → link).

## Section D — Data quality + enrichment

- **Data-quality view** with buckets + counts: missing email (39% on popcam),
  missing company, missing title, unknown-name.
- **One-click company link** for the tiny linkable set (company text but no id;
  domain-matchable) via `link_lead_company`.
- **Trigger email-finder** on selected no-email leads (`email_finder.cmd_find`),
  **company/multi-domain aware**: resolve lead → `company_id` →
  `company_identities` domains, and either pick the primary or let the user
  choose which domain(s) to try. Selective + confirmed; provider-credit warning.
- **Trigger Serper research** (enrich.py flow) as a background job via an
  extended `SyncManager` (like CRM sync), for leads the email finder can't place.
- **Cleanup truly-empty** leads (`cleanup_junk_leads`, `confirm=True`) — only the
  event-less subset; dry-run preview first. (Note: the ~9.7k "unknown" leads that
  *have* events are NOT deletable here; they go through enrichment instead.)

## Section E — Deliverability reorg + editing + age filters

- Reorganize: mailboxes grouped/collapsible under their domain; the 465-row wall
  becomes scannable.
- **Age filters [decided]:** never-sent / not in 3 / 6 / 12mo — filter only.
- Sender/domain rows → click to **edit** in the slide-over
  (`update_sender_account`, `set_sender_domain_cost`): reseller, cost, provider,
  name, notes, etc.
- Senders live **here** (moved out of campaign detail).
- Color-coded health (status palette).

## Section F — Campaign detail = leads

- Campaign click → the campaign's **leads** (searchable, paginated,
  click-through to the slide-over), not senders.
- Keep the daily matrix + subject/copy audit; **remove** senders (now in
  Deliverability).
- All sub-queries honor the date range.

## Section G — Activity search

- Server-side search across the **full range** by event type **and**
  name / email / linkedin / company / company_domain — not just visible rows.
- Labeled controls (an event-type dropdown + a search box), so it's obvious what
  the filter does.

## Section H — Sync tab item detail

- Outbox row click → slide-over: resolved entity (current record), the pending
  payload that will push, attempts, and last error. Uses F1.

## Section I — Design polish

Finalize F4 across every tab once the surface is complete: accent, status colors,
spacing, consistent panels, tab icons.

---

## Sequencing

1. **Foundations** (F1–F3; F4 scaffolding)
2. **A — correctness** (fixes what's visibly wrong now)
3. **E, F — reorg** (deliverability editing + campaign=leads)
4. **B, C — Contacts + Companies + merge review** (biggest new surface)
5. **D — data quality + enrichment** (email finder + Serper + cleanup)
6. **G, H — search + sync detail**
7. **I — design polish**

Each step: new queries in `dashboard_queries.py`, actions in
`dashboard_actions.py`, routes in `dashboard_server.py`, tests, then
`scripts/sync-local.sh` to Hermes for live testing. Manifest regenerated and full
`pytest` before each Hermes sync.

## Open items to confirm as we go

- Email-finder domain choice for multi-domain companies: auto-pick primary vs.
  always prompt (leaning: prompt when >1 domain, auto when exactly 1).
- Serper research is a multi-step flow (queries → search → format → map); it runs
  as one background job with progress in the sync pill.
- Company-field editing needs a small new authoritative setter (wrapping
  `_update_company_fields`, which today only fills blanks) — the one genuinely new
  mutation; everything else is existing.
