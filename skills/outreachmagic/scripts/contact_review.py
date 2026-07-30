"""Persisting contact-sourcing results, and the queue the agent drains.

`contact_extract.py` extracts and scores; this module owns everything that
touches the database -- the same split `serper_candidates.py` keeps from
`serper_review.py`, and for the same reason: extraction is a pure function over
text, while everything here has to survive being run twice.

### The agent-delegated contract

Identical in shape to `personalize-pending` / `personalize-set --batch`, which
is the contract this codebase already uses to hand a model a batch of judgement
calls:

    pipeline.py contact-extract-pending --workspace W --limit 20 --json
    # -> [{"company_id": 83672, "company": "...", "url": "...", "markdown": "...",
    #      "regex_found": 1, "icp": {...}}]

    pipeline.py contact-apply --batch --json '[{"company_id": 83672, "contacts": [
    #   {"name": "...", "title": "...", "phone": "...", "email": null}]}]'

Only pages the regex pass could not crack come back from `contact-extract-pending`
-- the pass handles the bulk, and the agent is spent on the tail, where judgement
is actually worth something.

### Idempotency is the whole point of this phase

676 staff pages surface the same person on several pages and across dealer
groups, so "applied twice" is the normal case, not the edge case. Two mechanisms:

  * **Stable identities.** A sourced contact is keyed on `name|domain`, never on
    `name|domain|title`. `build_import_identities()` prefers the title-bearing
    key when a title is present, which means a person whose title is scraped as
    "Sales Manager" on Monday and "Sales Manager, Bilingual" on Tuesday becomes
    two leads. Contact sourcing passes its identities explicitly to avoid that.
  * **A page is applied once per ICP version.** `contact-extract-pending` skips
    companies that already have an agent observation under the same
    `icp_config_hash`; changing the profile makes them eligible again, which is
    exactly when re-deciding is worth doing.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Optional

import contact_extract as ce
import contact_icp
from db_conn import get_conn
from workspace_routing import (
    DEFAULT_ORG_ID,
    normalize_company_name_key,
    normalize_person_name,
    resolve_workspace_identity,
    upsert_workspace_lead,
)

# What a contact-sourced lead records as its provenance, so a later audit can
# tell these apart from a CSV import or a sequencer reply.
SOURCE = "contact_sourcing"
PROVIDER = "firecrawl"
EXTRACTOR_REGEX = "regex"
EXTRACTOR_AGENT = "agent"

DEFAULT_PENDING_LIMIT = 20


class ContactReviewError(ValueError):
    """User-facing failure (unknown company, unknown workspace, bad batch item)."""


# ── observations ─────────────────────────────────────────────────────────────

def record_observation(
    conn: sqlite3.Connection,
    company_id: int,
    *,
    outcome: str,
    workspace_id: Optional[str] = None,
    url: Optional[str] = None,
    cache_hit: bool = False,
    fetch_ms: Optional[int] = None,
    http_status: Optional[int] = None,
    regex_found: int = 0,
    extractor: Optional[str] = None,
    contacts_attached: int = 0,
    contacts_queued: int = 0,
    icp_config_hash: Optional[str] = None,
    cost_estimate_usd: Optional[float] = None,
    provider: str = PROVIDER,
    error: Optional[str] = None,
) -> int:
    """Append one row to company_contact_observations.

    Called on every path including failure. A page that returned nothing is a
    fact worth keeping -- it is the only thing that stops the next run paying to
    find out the same thing again. `contact_discovery.py` writes through here too.
    """
    cur = conn.execute(
        """INSERT INTO company_contact_observations (
               company_id, workspace_id, provider, url, cache_hit, fetch_ms,
               http_status, regex_found, extractor, contacts_attached,
               contacts_queued, icp_config_hash, cost_estimate_usd, outcome, error
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (company_id, workspace_id, provider, url, 1 if cache_hit else 0, fetch_ms,
         http_status, int(regex_found), extractor, int(contacts_attached),
         int(contacts_queued), icp_config_hash, cost_estimate_usd, outcome, error),
    )
    return int(cur.lastrowid)


# ── the pending queue ────────────────────────────────────────────────────────

def extract_pending(
    conn: sqlite3.Connection,
    workspace_id: Optional[str] = None,
    *,
    icp: Optional[dict] = None,
    icp_hash: Optional[str] = None,
    limit: int = DEFAULT_PENDING_LIMIT,
    force: bool = False,
) -> list[dict]:
    """Cached pages the regex pass could not crack, ready for the agent.

    Returns the page markdown inline. That is deliberate and it is why
    `SKILL.md` insists on subagents: 20 pages is ~200k tokens, which belongs in
    a disposable context that dies with the batch, not in the main thread.
    """
    icp = icp or {}
    # "Belongs to this workspace" is either an existing lead there OR a
    # contact-sourcing run launched from it. The second half is load-bearing: a
    # company being sourced usually has *no* contact in the workspace yet --
    # that is the entire reason it is being sourced -- so a lead-only join
    # would empty the queue exactly when it matters.
    rows = conn.execute(
        """SELECT pc.company_id, pc.url, pc.markdown, pc.content_hash,
                  c.name AS company, c.domain
             FROM company_page_cache pc
             JOIN companies c ON c.id = pc.company_id
            WHERE pc.markdown IS NOT NULL AND TRIM(pc.markdown) != ''
              AND (? = ''
                   OR EXISTS (SELECT 1 FROM leads l
                                JOIN workspace_leads wl ON wl.lead_id = l.id
                               WHERE l.company_id = pc.company_id
                                 AND wl.workspace_id = ?)
                   OR EXISTS (SELECT 1 FROM company_contact_observations o
                               WHERE o.company_id = pc.company_id
                                 AND o.workspace_id = ?))
            ORDER BY pc.fetched_at DESC""",
        (workspace_id or "", workspace_id or "", workspace_id or ""),
    )  # iterated lazily, not fetchall(): every row carries a whole page body.

    already = set()
    if not force:
        already = {
            r["company_id"] for r in conn.execute(
                """SELECT DISTINCT company_id FROM company_contact_observations
                    WHERE extractor = ? AND IFNULL(icp_config_hash, '') = ?""",
                (EXTRACTOR_AGENT, icp_hash or ""),
            ).fetchall()
        }

    out: list[dict] = []
    for row in rows:
        if len(out) >= max(1, int(limit)):
            break
        if row["company_id"] in already:
            continue
        candidates = ce.regex_pass(row["markdown"])
        if not ce.needs_agent(candidates, icp):
            continue
        out.append({
            "company_id": row["company_id"],
            "company": row["company"],
            "domain": row["domain"],
            "url": row["url"],
            "markdown": row["markdown"],
            "regex_found": sum(1 for c in candidates if c.title),
            "icp": icp,
        })
    return out


# ── applying a batch ─────────────────────────────────────────────────────────

def _stable_identities(
    name: str,
    *,
    domain: Optional[str],
    company: Optional[str],
    email: Optional[str],
) -> list[tuple[str, str]]:
    """The identity list a sourced contact is matched on, twice-run-safe.

    Explicitly *not* `build_import_identities()`: that helper prefers
    `name_company_domain_title` whenever a title is present, so re-scraping a
    person whose title string shifted by one word creates a second lead. Titles
    move; a person at a company does not.
    """
    from pipeline_utils import normalize_company_domain
    from pipeline import normalize_email

    out: list[tuple[str, str]] = []
    email_norm = normalize_email(email) if email else None
    if email_norm:
        out.append(("email", email_norm))

    norm_name = normalize_person_name(name)
    if not norm_name:
        return out
    domain_norm = normalize_company_domain(domain) if domain else None
    if domain_norm:
        out.append(("name_company_domain", f"{norm_name}|{domain_norm}"))
    else:
        ckey = normalize_company_name_key(company) if company else None
        if ckey:
            out.append(("name_company", f"{norm_name}|{ckey}"))
    return out


_COMPOSITE_TYPES = ("name_company_domain", "name_company")


def _existing_lead_id(
    conn: sqlite3.Connection,
    identities: list[tuple[str, str]],
    email: Optional[str],
) -> Optional[int]:
    """The lead this contact already is: by email first, then by the composite.

    Both lookups happen here rather than inside resolve_lead, for two different
    reasons:

      * **Composite:** resolve_lead consults composite identities only when the
        caller has no strong one. The second scrape of a person often carries an
        email the first one didn't, and on that run the composite key written the
        first time would never be read -- so the person is created twice.
      * **Email:** the address is deliberately withheld from resolve_lead (see
        apply_company_contacts), so it cannot do this lookup at all.

    Whatever this returns is passed as `force_lead_id`, which is why email wins:
    an address already on a lead is the strongest statement of identity there is.
    """
    from pipeline import find_lead_by_email, normalize_email
    from workspace_routing import find_lead_by_identity

    if email:
        email_norm = normalize_email(email)
        if email_norm:
            found = find_lead_by_email(conn, email_norm)
            if found:
                return int(found)

    for itype, value in identities:
        if itype in _COMPOSITE_TYPES:
            found = find_lead_by_identity(conn, DEFAULT_ORG_ID, itype, value)
            if found:
                return int(found)
    return None


def _set_primary_email(conn: sqlite3.Connection, lead_id: int, email: str) -> None:
    """Write the scraped address onto a lead that has none.

    Done here because the address is kept out of resolve_lead. Only fills an
    empty column -- an address already on the lead was chosen by something with
    more standing than a staff-page scrape (a verification, a reply, an
    operator), and a page listing a role mailbox must not overwrite it.
    """
    from pipeline import email_domain, normalize_email

    email_norm = normalize_email(email)
    if not email_norm:
        return
    conn.execute(
        """UPDATE leads SET email = ?, email_domain = ?, updated_at = datetime('now')
            WHERE id = ? AND IFNULL(TRIM(email), '') = ''""",
        (email_norm, email_domain(email_norm), lead_id),
    )


def _company_row(conn: sqlite3.Connection, company_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, name, domain FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
    if not row:
        raise ContactReviewError(f"company not found: {company_id}")
    return row


def _clean(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def apply_company_contacts(
    conn: sqlite3.Connection,
    company_id: int,
    contacts: Iterable[dict],
    *,
    workspace_id: Optional[str] = None,
    icp: Optional[dict] = None,
    icp_hash: Optional[str] = None,
    url: Optional[str] = None,
    extractor: str = EXTRACTOR_AGENT,
    dry_run: bool = False,
    record: bool = True,
) -> dict:
    """Attach one company's extracted contacts. Returns what happened, per contact.

    The blocklist is enforced here even though the agent was handed the profile.
    It is the one ICP rule that says "never contact this person", and a rule
    with that consequence should not depend on a model having honoured it. The
    whitelist is *not* enforced: an adjacent title the agent judged worth
    keeping is exactly the judgement the agent was asked for, and it is recorded
    in the result either way.
    """
    from pipeline import resolve_lead

    company = _company_row(conn, company_id)
    blocklist = (icp or {}).get("blocklist") or []

    applied: list[dict] = []
    rejected: list[dict] = []
    seen: set[str] = set()

    for raw in contacts or []:
        if not isinstance(raw, dict):
            rejected.append({"contact": raw, "reason": "not_an_object"})
            continue
        name = _clean(raw.get("name"))
        if not name:
            rejected.append({"contact": raw, "reason": "no_name"})
            continue
        title = _clean(raw.get("title"))
        email = _clean(raw.get("email"))
        phone = _clean(raw.get("phone"))

        # Within-batch dedup, before the database sees it: a staff page listing
        # the same person under two departments would otherwise race itself.
        key = (normalize_person_name(name) or name.lower())
        if key in seen:
            rejected.append({"name": name, "reason": "duplicate_in_batch"})
            continue
        seen.add(key)

        blocked = ce._longest_match(blocklist, ce._norm(title or ""))
        if blocked:
            rejected.append({"name": name, "title": title,
                             "reason": "blocklist", "matched": blocked})
            continue

        verdict = None
        if icp:
            scored = ce.score_against_icp(
                [ce.ContactCandidate(name=name, title=title)], icp,
                company_name=company["name"])
            verdict = scored[0].reason

        if dry_run:
            applied.append({"name": name, "title": title, "lead_id": None,
                            "created": None, "icp": verdict})
            continue

        identities = _stable_identities(
            name, domain=company["domain"], company=company["name"], email=email)
        result = resolve_lead(
            name=name,
            title=title,
            # The address is deliberately withheld. resolve_lead derives a
            # company from the email domain and re-links the new lead to it,
            # creating that company if needed (pipeline.py, the
            # `domain_from_email != effective_domain` relink). That is right for
            # a CSV import, where the address is the best evidence of who
            # employs someone -- and wrong here, where the staff page we just
            # read *is* that evidence. A dealer group publishing group-wide
            # addresses otherwise scatters one dealership's roster across
            # freshly minted duplicate companies: a real 10-company run put 42
            # contacts onto 12 companies, 5 of them invented.
            #
            # The address is not lost: it is in `identities` (so dedup matches
            # on it), it is what _existing_lead_id resolves first, and
            # _set_primary_email writes it below.
            company=company["name"],
            company_domain=company["domain"],
            source=SOURCE,
            source_detail=url,
            source_platform=PROVIDER,
            identities=identities,
            force_lead_id=_existing_lead_id(conn, identities, email),
            allow_weak_identity=True,
            conn=conn,
        )
        if result.get("status") == "error":
            rejected.append({"name": name, "reason": result.get("error")})
            continue

        lead_id = int(result["id"])
        # Pinned, not filled-if-empty. The staff page is authoritative for who
        # employs this person, so it overrides whatever any earlier import
        # inferred from an address.
        conn.execute(
            "UPDATE leads SET company_id = ? WHERE id = ? AND IFNULL(company_id, -1) != ?",
            (company_id, lead_id, company_id),
        )
        if email:
            _set_primary_email(conn, lead_id, email)
        if workspace_id:
            upsert_workspace_lead(conn, DEFAULT_ORG_ID, workspace_id, lead_id)
        if phone:
            _attach_phone(conn, lead_id, phone)

        applied.append({
            "name": name, "title": title, "lead_id": lead_id,
            "created": result.get("status") == "created", "icp": verdict,
        })

    # `record=False` is for contact_discovery, whose own single-exit _finish()
    # already writes one row per company. Two writers on one code path is how a
    # run reports twice as many attempts as it made.
    if not dry_run and record:
        record_observation(
            conn, company_id,
            outcome="applied" if applied else "no_contacts",
            workspace_id=workspace_id,
            url=url,
            cache_hit=True,
            extractor=extractor,
            regex_found=len(applied) + len(rejected),
            contacts_attached=len(applied),
            contacts_queued=len(rejected),
            icp_config_hash=icp_hash,
        )

    return {
        "company_id": company_id,
        "company": company["name"],
        "attached": len(applied),
        "rejected": len(rejected),
        "contacts": applied,
        "rejections": rejected,
    }


def _attach_phone(conn: sqlite3.Connection, lead_id: int, phone: str) -> None:
    """Best-effort. A staff page prints plenty of numbers that aren't dialable,
    and losing an entire contact over an unparseable phone would be absurd."""
    import phone_numbers

    try:
        phone_numbers.add_phone(
            "lead", lead_id, phone, label="direct", source="staff_page", conn=conn)
    except phone_numbers.PhoneNumberError:
        pass


def apply_batch(
    items: list[dict],
    *,
    workspace: Optional[str] = None,
    icp_name: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """Apply many companies' contacts in one transaction.

    All-or-nothing, like `serper_review.apply_batch`: a subagent posting twenty
    companies at once and getting "some of it worked" back leaves nobody able to
    say which half.
    """
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        workspace_id = None
        if workspace:
            ws = resolve_workspace_identity(conn, workspace)
            if not ws:
                conn.execute("ROLLBACK")
                return {"status": "error", "error": f"workspace not found: {workspace}"}
            workspace_id = ws["id"]

        icp = icp_hash = None
        if workspace_id:
            profile = contact_icp.get_profile(conn, workspace_id, icp_name)
            if profile:
                icp, icp_hash = profile["config"], profile["config_hash"]
            elif icp_name:
                conn.execute("ROLLBACK")
                return {"status": "error",
                        "error": f"no ICP profile named {icp_name!r} in {workspace}"}

        results, errors = [], []
        for item in items or []:
            try:
                if not isinstance(item, dict):
                    raise ContactReviewError("batch item must be an object")
                results.append(apply_company_contacts(
                    conn, int(item["company_id"]), item.get("contacts") or [],
                    workspace_id=workspace_id, icp=icp, icp_hash=icp_hash,
                    url=item.get("url"), dry_run=dry_run,
                ))
            except (ContactReviewError, KeyError, TypeError, ValueError) as exc:
                errors.append({"item": item, "error": str(exc)})

        if errors:
            conn.execute("ROLLBACK")
            return {"status": "error", "attached": 0, "errors": errors}
        conn.execute("ROLLBACK" if dry_run else "COMMIT")
    finally:
        conn.close()

    return {
        "status": "dry_run" if dry_run else "ok",
        "companies": len(results),
        "attached": sum(r["attached"] for r in results),
        "rejected": sum(r["rejected"] for r in results),
        "results": results,
        "errors": [],
    }


# ── CLI entry points ─────────────────────────────────────────────────────────

def cli_extract_pending(
    workspace: Optional[str] = None,
    *,
    icp_name: Optional[str] = None,
    limit: int = DEFAULT_PENDING_LIMIT,
    force: bool = False,
) -> dict:
    conn = get_conn()
    try:
        workspace_id = None
        ws_slug = None
        if workspace:
            ws = resolve_workspace_identity(conn, workspace)
            if not ws:
                raise ContactReviewError(f"workspace not found: {workspace}")
            workspace_id, ws_slug = ws["id"], ws["slug"]

        icp = icp_hash = None
        if workspace_id:
            profile = contact_icp.get_profile(conn, workspace_id, icp_name)
            if profile:
                icp, icp_hash = profile["config"], profile["config_hash"]
            elif icp_name:
                raise ContactReviewError(
                    f"no ICP profile named {icp_name!r} in {workspace}")

        pending = extract_pending(
            conn, workspace_id, icp=icp, icp_hash=icp_hash, limit=limit, force=force)
    finally:
        conn.close()
    return {
        "status": "ok", "workspace": ws_slug, "icp_config_hash": icp_hash,
        "count": len(pending), "pending": pending,
    }


def cli_apply(
    payload: Optional[str] = None,
    *,
    path: Optional[str] = None,
    workspace: Optional[str] = None,
    icp_name: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    if not payload and not path:
        raise ContactReviewError("contact-apply --batch needs --json or --file")
    raw = payload
    if path:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    try:
        items = json.loads(raw or "")
    except json.JSONDecodeError as exc:
        raise ContactReviewError(f"could not parse the batch as JSON: {exc}") from None
    if not isinstance(items, list):
        raise ContactReviewError("the batch must be a JSON array of {company_id, contacts}")
    return apply_batch(items, workspace=workspace, icp_name=icp_name, dry_run=dry_run)
