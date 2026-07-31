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

### The human queue

`contact-review` is the third surface, and it follows `serper_review.py`'s three
rules exactly: nothing is pre-selected, "none of these" is an answer that gets
recorded, and rejections are kept. Where it differs is that it stores no
candidate blob -- a page's people are a pure function of the cached page, so a
candidate's id is its position in `regex_pass` output, re-derived on demand.
See the section header above `_attached_names` for what that buys and costs.

    pipeline.py contact-review --workspace W --json
    pipeline.py contact-apply --company-id N --contact-ids 3,7
    pipeline.py contact-review --company-id N --none-of-these
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
EXTRACTOR_HUMAN = "human"

DEFAULT_PENDING_LIMIT = 20
DEFAULT_REVIEW_LIMIT = 25

# "This company belongs to the workspace" -- an existing lead there, OR a
# contact-sourcing run launched from it. The second half is load-bearing: a
# company being sourced usually has *no* contact in the workspace yet -- that is
# the entire reason it is being sourced -- so a lead-only join would empty both
# queues exactly when they matter. Takes the workspace id three times; an empty
# string means "every workspace".
_WORKSPACE_SCOPE_SQL = """
    (? = ''
     OR EXISTS (SELECT 1 FROM leads l
                  JOIN workspace_leads wl ON wl.lead_id = l.id
                 WHERE l.company_id = pc.company_id
                   AND wl.workspace_id = ?)
     OR EXISTS (SELECT 1 FROM company_contact_observations o
                 WHERE o.company_id = pc.company_id
                   AND o.workspace_id = ?))
"""


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
    decision: Optional[dict] = None,
) -> int:
    """Append one row to company_contact_observations.

    Called on every path including failure. A page that returned nothing is a
    fact worth keeping -- it is the only thing that stops the next run paying to
    find out the same thing again. `contact_discovery.py` writes through here too.

    `decision` is what a human was offered and what they chose. Only the review
    surface passes it; every machine-written row leaves it null.
    """
    cur = conn.execute(
        """INSERT INTO company_contact_observations (
               company_id, workspace_id, provider, url, cache_hit, fetch_ms,
               http_status, regex_found, extractor, contacts_attached,
               contacts_queued, icp_config_hash, cost_estimate_usd, outcome, error,
               decision_json
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (company_id, workspace_id, provider, url, 1 if cache_hit else 0, fetch_ms,
         http_status, int(regex_found), extractor, int(contacts_attached),
         int(contacts_queued), icp_config_hash, cost_estimate_usd, outcome, error,
         json.dumps(decision, separators=(",", ":")) if decision else None),
    )
    return int(cur.lastrowid)


# ── cached pages ─────────────────────────────────────────────────────────────

def _cached_pages(
    conn: sqlite3.Connection, workspace_id: Optional[str] = None,
) -> sqlite3.Cursor:
    """Every cached staff page in a workspace, newest first.

    Returned as an open cursor and iterated lazily rather than fetchall()'d:
    every row carries a whole page body, and both queues stop as soon as they
    have filled a batch.
    """
    return conn.execute(
        f"""SELECT pc.company_id, pc.url, pc.markdown, pc.content_hash,
                   c.name AS company, c.domain
              FROM company_page_cache pc
              JOIN companies c ON c.id = pc.company_id
             WHERE pc.markdown IS NOT NULL AND TRIM(pc.markdown) != ''
               AND {_WORKSPACE_SCOPE_SQL}
             ORDER BY pc.fetched_at DESC""",
        (workspace_id or "", workspace_id or "", workspace_id or ""),
    )


def _latest_cached_page(
    conn: sqlite3.Connection, company_id: int,
) -> Optional[sqlite3.Row]:
    """The page `contact-review` and `--reparse` both read for one company."""
    return conn.execute(
        """SELECT company_id, url, markdown, content_hash
             FROM company_page_cache
            WHERE company_id = ? AND markdown IS NOT NULL AND TRIM(markdown) != ''
            ORDER BY fetched_at DESC LIMIT 1""",
        (company_id,),
    ).fetchone()


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
    rows = _cached_pages(conn, workspace_id)

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


# ── the human review queue ───────────────────────────────────────────────────
#
# `serper_review.py` is the model, and its three rules hold here unchanged:
# nothing is pre-selected, "none of these" is an answer, and rejections are
# kept. What differs is where the candidates live. Serper stores an extracted
# blob per lead; contact sourcing does not need to, because a page's people are
# a *pure function of the cached page* -- `regex_pass` is deterministic and
# ICP-agnostic, and `score_against_icp` never drops anything. So a candidate's
# id is simply its position in that list, re-derived on demand, and no second
# copy of the page's contents exists to drift from the cache.
#
# The consequence to be honest about: ids move if the page body changes. They
# do *not* move when the ICP changes -- only the verdicts do. The review payload
# therefore carries `content_hash`, and `contact-apply --content-hash` verifies
# it for a caller who wants the guarantee across a re-fetch.

def _attached_names(conn: sqlite3.Connection, company_id: int) -> set[str]:
    """Normalised names already on this company, so review can mark them.

    An already-attached person is still shown -- "the ICP kept someone it
    shouldn't have" is a thing a reviewer needs to see -- but they are labelled,
    so nobody spends judgement re-picking a contact they already have.
    """
    rows = conn.execute(
        "SELECT name FROM leads WHERE company_id = ? AND IFNULL(TRIM(name), '') != ''",
        (company_id,),
    ).fetchall()
    out = set()
    for row in rows:
        key = normalize_person_name(row["name"]) or (row["name"] or "").strip().lower()
        if key:
            out.add(key)
    return out


def _candidates_from_page(
    page: sqlite3.Row,
    company_name: Optional[str],
    icp: Optional[dict],
    attached: set[str],
) -> list[dict]:
    scored = ce.score_against_icp(
        ce.regex_pass(page["markdown"]), icp, company_name=company_name)
    out = []
    for index, item in enumerate(scored):
        key = (normalize_person_name(item.candidate.name)
               or item.candidate.name.strip().lower())
        # Score order is *page* order here, and deliberately so: the id has to
        # mean the same thing on the next call, and a sort key that depends on
        # the ICP would renumber every candidate when the whitelist is edited.
        out.append({"id": index, **item.as_dict(), "attached": key in attached})
    return out


def company_candidates(
    conn: sqlite3.Connection,
    company_id: int,
    *,
    icp: Optional[dict] = None,
) -> dict:
    """Everyone on this company's cached page, with the ICP's verdict and an id."""
    company = _company_row(conn, company_id)
    page = _latest_cached_page(conn, company_id)
    if page is None:
        raise ContactReviewError(f"no cached page for company {company_id}")
    return {
        "company_id": company_id,
        "company": company["name"],
        "domain": company["domain"],
        "url": page["url"],
        "content_hash": page["content_hash"],
        "candidates": _candidates_from_page(
            page, company["name"], icp, _attached_names(conn, company_id)),
    }


def _decided_companies(
    conn: sqlite3.Connection, icp_hash: Optional[str],
) -> set[int]:
    """Companies a human has already ruled on under this ICP version.

    Scoped to the ICP hash for the same reason the agent queue is: editing the
    profile is exactly when re-deciding is worth doing, and until then asking
    again wastes the one resource this queue spends, which is attention.
    """
    return {
        r["company_id"] for r in conn.execute(
            """SELECT DISTINCT company_id FROM company_contact_observations
                WHERE extractor = ? AND IFNULL(icp_config_hash, '') = ?""",
            (EXTRACTOR_HUMAN, icp_hash or ""),
        ).fetchall()
    }


def review_queue(
    conn: sqlite3.Connection,
    workspace_id: Optional[str] = None,
    *,
    icp: Optional[dict] = None,
    icp_hash: Optional[str] = None,
    limit: int = DEFAULT_REVIEW_LIMIT,
    offset: int = 0,
    force: bool = False,
) -> dict:
    """Companies whose cached page has people nobody has ruled on yet.

    A company with nothing left to choose between is not in the queue: every
    candidate already attached is a decision that has effectively been made, and
    a queue that shows it is a queue an operator learns to skim.
    """
    already = set() if force else _decided_companies(conn, icp_hash)
    companies: list[dict] = []
    wanted = max(1, int(limit)) + max(0, int(offset))

    for row in _cached_pages(conn, workspace_id):
        if len(companies) >= wanted:
            break
        if row["company_id"] in already:
            continue
        candidates = _candidates_from_page(
            row, row["company"], icp, _attached_names(conn, row["company_id"]))
        if not any(not c["attached"] for c in candidates):
            continue
        companies.append({
            "company_id": row["company_id"],
            "company": row["company"],
            "domain": row["domain"],
            "url": row["url"],
            "content_hash": row["content_hash"],
            # No `chosen`, no default, no truncation to the ICP's keeps. The
            # rejects are the entire reason a human is looking.
            "candidates": candidates,
        })

    page = companies[offset:offset + max(1, int(limit))]
    return {
        "icp_config_hash": icp_hash,
        "limit": limit, "offset": offset,
        # `count` is what came back, not what exists -- the same meaning it has
        # on `contact-extract-pending`. A true total would mean scoring every
        # cached page in the workspace on every call, and each of those rows
        # carries a whole page body. Page until a call returns fewer than
        # `limit`; that is the end.
        "count": len(page),
        "companies": page,
    }


# ── recording a decision ─────────────────────────────────────────────────────

def _decision_payload(candidates: list[dict], chosen_ids: set[int]) -> dict:
    """What was offered, and what was taken. Both halves are kept."""
    return {
        "chosen": [
            {"id": c["id"], "name": c["name"], "title": c["title"],
             "reason": c["reason"]}
            for c in candidates if c["id"] in chosen_ids
        ],
        "rejected": [
            {"id": c["id"], "name": c["name"], "title": c["title"],
             "reason": c["reason"]}
            for c in candidates if c["id"] not in chosen_ids
        ],
    }


def review_company(
    conn: sqlite3.Connection,
    company_id: int,
    *,
    contact_ids: Optional[Iterable[int]] = None,
    dismissed: bool = False,
    workspace_id: Optional[str] = None,
    icp: Optional[dict] = None,
    icp_hash: Optional[str] = None,
    content_hash: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """Apply a human's picks for one company, or record "none of these".

    Both land as a single observation with `extractor = 'human'`, so the queue
    stops offering the company and the next run can tell a decision from a page
    nobody has looked at. Those are different states, and collapsing them is how
    a review queue silently re-asks.
    """
    view = company_candidates(conn, company_id, icp=icp)
    if content_hash and view["content_hash"] != content_hash:
        raise ContactReviewError(
            f"company {company_id}: the page changed since it was reviewed "
            f"(expected {content_hash}, cached {view['content_hash']}); "
            "re-run contact-review to renumber the candidates")

    candidates = view["candidates"]
    by_id = {c["id"]: c for c in candidates}
    chosen_ids: set[int] = set()
    if not dismissed:
        for raw in contact_ids or ():
            cid = int(raw)
            if cid not in by_id:
                raise ContactReviewError(
                    f"company {company_id}: no candidate with id {cid} "
                    f"(this page offers 0-{len(candidates) - 1})"
                    if candidates else
                    f"company {company_id}: the cached page offers no candidates")
            chosen_ids.add(cid)
        if not chosen_ids:
            raise ContactReviewError(
                "pass --contact-ids, or --none-of-these to record that none fit")

    applied = {"attached": 0, "contacts": [], "rejections": []}
    if chosen_ids:
        applied = apply_company_contacts(
            conn, company_id,
            [{"name": by_id[i]["name"], "title": by_id[i]["title"],
              "email": by_id[i]["email"], "phone": by_id[i]["phone"]}
             for i in sorted(chosen_ids)],
            workspace_id=workspace_id, icp=icp, icp_hash=icp_hash,
            url=view["url"], extractor=EXTRACTOR_HUMAN, dry_run=dry_run,
            # One row per decision, written below with the decision on it. The
            # apply writing its own would report the company reviewed twice.
            record=False,
        )

    decision = _decision_payload(candidates, chosen_ids)
    if not dry_run:
        record_observation(
            conn, company_id,
            outcome="dismissed" if dismissed else "reviewed",
            workspace_id=workspace_id,
            url=view["url"],
            cache_hit=True,
            extractor=EXTRACTOR_HUMAN,
            regex_found=len(candidates),
            contacts_attached=applied["attached"],
            contacts_queued=len(decision["rejected"]),
            icp_config_hash=icp_hash,
            decision=decision,
        )

    return {
        "status": "dry_run" if dry_run else ("dismissed" if dismissed else "reviewed"),
        "company_id": company_id,
        "company": view["company"],
        "url": view["url"],
        "offered": len(candidates),
        "attached": applied["attached"],
        "contacts": applied["contacts"],
        "rejections": applied["rejections"],
        "decision": decision,
    }


# ── CLI entry points ─────────────────────────────────────────────────────────

def _resolve_context(
    conn: sqlite3.Connection,
    workspace: Optional[str],
    icp_name: Optional[str],
) -> tuple[Optional[str], Optional[str], Optional[dict], Optional[str]]:
    """The workspace and the ICP version a command runs under.

    A *named* profile that does not exist is an error rather than a fall-through
    to no ICP: silently running the whole thing unfiltered because of a typo in
    `--icp` is how a queue fills with people nobody wanted.
    """
    workspace_id = ws_slug = None
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
    return workspace_id, ws_slug, icp, icp_hash


def cli_extract_pending(
    workspace: Optional[str] = None,
    *,
    icp_name: Optional[str] = None,
    limit: int = DEFAULT_PENDING_LIMIT,
    force: bool = False,
) -> dict:
    conn = get_conn()
    try:
        workspace_id, ws_slug, icp, icp_hash = _resolve_context(
            conn, workspace, icp_name)
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


def cli_review(
    workspace: Optional[str] = None,
    *,
    company_id: Optional[int] = None,
    icp_name: Optional[str] = None,
    limit: int = DEFAULT_REVIEW_LIMIT,
    offset: int = 0,
    force: bool = False,
    none_of_these: bool = False,
) -> dict:
    """Show the queue, one company from it, or record "none of these"."""
    if none_of_these and not company_id:
        raise ContactReviewError("--none-of-these needs --company-id")

    conn = get_conn()
    try:
        workspace_id, ws_slug, icp, icp_hash = _resolve_context(
            conn, workspace, icp_name)

        if none_of_these:
            conn.execute("BEGIN")
            try:
                result = review_company(
                    conn, int(company_id), dismissed=True,
                    workspace_id=workspace_id, icp=icp, icp_hash=icp_hash)
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return {"status": result["status"], "workspace": ws_slug,
                    "icp_config_hash": icp_hash, **result}

        if company_id:
            view = company_candidates(conn, int(company_id), icp=icp)
            return {"status": "ok", "workspace": ws_slug,
                    "icp_config_hash": icp_hash, "count": 1, "companies": [view]}

        queue = review_queue(
            conn, workspace_id, icp=icp, icp_hash=icp_hash,
            limit=limit, offset=offset, force=force)
        return {"status": "ok", "workspace": ws_slug, **queue}
    finally:
        conn.close()


def cli_apply_ids(
    company_id: int,
    contact_ids: Iterable[int],
    *,
    workspace: Optional[str] = None,
    icp_name: Optional[str] = None,
    content_hash: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """`contact-apply --company-id N --contact-ids 3,7` -- the id-based path."""
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        try:
            workspace_id, ws_slug, icp, icp_hash = _resolve_context(
                conn, workspace, icp_name)
            result = review_company(
                conn, int(company_id), contact_ids=contact_ids,
                workspace_id=workspace_id, icp=icp, icp_hash=icp_hash,
                content_hash=content_hash, dry_run=dry_run)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("ROLLBACK" if dry_run else "COMMIT")
    finally:
        conn.close()
    return {"workspace": ws_slug, "icp_config_hash": icp_hash, **result}


def parse_contact_ids(raw: Optional[str]) -> list[int]:
    """`3,7` / `3 7` -> [3, 7]. Rejects anything that isn't an id.

    A silently-dropped token here attaches the wrong person, so a bad one is an
    error rather than a skip.
    """
    out: list[int] = []
    for token in (raw or "").replace(",", " ").split():
        try:
            out.append(int(token))
        except ValueError:
            raise ContactReviewError(
                f"--contact-ids takes numbers from contact-review; got {token!r}") from None
    if not out:
        raise ContactReviewError("--contact-ids is empty")
    return out
