#!/usr/bin/env python3
"""
Lead Enrich — Person research helper for Hermes / Cursor / Claude Code.

Auto-detects outreachmagic, loads config, runs dedup checks against the local
SQLite database, and formats Serper.dev results for model extraction.

Usage:
    enrich.py config                          # Show loaded config
    enrich.py check "Jane Doe" "Acme Corp"    # Dedup check (0 credits)
    enrich.py check --force "Jane Doe" "Acme" # Ignore existing DB match
    enrich.py batch-check input.json          # Batch dedup from JSON
    enrich.py normalize input.json            # Normalize input data
    enrich.py serper-queries person.json      # Print Serper query pack
    enrich.py serper-search --query "..."     # Run one Serper search (stdlib)
    enrich.py serper-format results.json      # Format Serper results for model
    enrich.py map-to-om research.json         # Map to outreachmagic import format
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

# ── Constants ────────────────────────────────────────────────────────────────

SKILL_NAME = "lead-enrich"
OUTREACHMAGIC_NAME = "outreachmagic"

# Common install paths across platforms
SKILL_SEARCH_PATHS = [
    Path.home() / ".hermes" / "skills",
    Path.home() / ".cursor" / "skills",
    Path.home() / ".claude" / "skills",
]


# ── Config ───────────────────────────────────────────────────────────────────

_HERMES_ENV_LOADED = False


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()


def _parse_dotenv_line(line: str) -> Optional[tuple[str, str]]:
    """Parse one KEY=VALUE line (stdlib; supports quotes and export prefix)."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[7:].lstrip()
    if "=" not in line:
        return None
    key, _, value = line.partition("=")
    key = key.strip()
    if not key:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return key, value


def _load_dotenv_file(path: Path) -> None:
    """Merge KEY=VALUE pairs into os.environ (do not override existing)."""
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        try:
            text = path.read_text(encoding="latin-1")
        except OSError:
            return
    for line in text.splitlines():
        parsed = _parse_dotenv_line(line)
        if not parsed:
            continue
        key, value = parsed
        if key not in os.environ:
            os.environ[key] = value


def ensure_hermes_env_loaded() -> None:
    """Load Hermes secrets from ~/.hermes/.env (and default.env fallback)."""
    global _HERMES_ENV_LOADED
    if _HERMES_ENV_LOADED:
        return
    home = _hermes_home()
    for name in (".env", "default.env"):
        _load_dotenv_file(home / name)
    _HERMES_ENV_LOADED = True


def _subprocess_env() -> dict[str, str]:
    """Environment for outreachmagic pipeline subprocesses."""
    ensure_hermes_env_loaded()
    return {**os.environ, "PYTHONUNBUFFERED": "1"}


def _find_skill_dir() -> Path:
    """Find this skill's directory (where SKILL.md lives)."""
    return Path(__file__).resolve().parent.parent


def load_config() -> dict[str, Any]:
    """Load config.json, falling back to Hermes .env, env vars, and defaults."""
    ensure_hermes_env_loaded()
    skill_dir = _find_skill_dir()
    cfg_path = skill_dir / "config.json"

    cfg: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    # Env var overrides (shell + ~/.hermes/.env via ensure_hermes_env_loaded)
    serper = os.environ.get("SERPER_API_KEY", "").strip()
    if serper:
        cfg["serper_api_key"] = serper
    if os.environ.get("OUTREACHMAGIC_HOME"):
        cfg["outreachmagic_home"] = os.environ["OUTREACHMAGIC_HOME"]

    # Defaults
    cfg.setdefault("serper_endpoint", "https://google.serper.dev/search")
    cfg.setdefault("serper_num_results", 10)
    cfg.setdefault("serper_gl", "us")
    cfg.setdefault("serper_hl", "en")
    cfg.setdefault("max_people_per_run", 50)
    cfg.setdefault("dedup_before_search", True)
    cfg.setdefault("outreachmagic_home", "")

    return cfg


# ── Path resolution ──────────────────────────────────────────────────────────

def find_outreachmagic(config: dict[str, Any]) -> Optional[Path]:
    """Find the outreachmagic skill directory.

    Checks:
      1. Config `outreachmagic_home` / OUTREACHMAGIC_HOME env
      2. Sibling under same skills parent (e.g. ~/.hermes/skills/outreachmagic)
      3. Standard install paths (~/.hermes|cursor|claude/skills/outreachmagic)
    """
    candidates: list[Path] = []

    if config.get("outreachmagic_home"):
        candidates.append(Path(config["outreachmagic_home"]).expanduser())

    # Sibling: .../skills/lead-enrich and .../skills/outreachmagic
    candidates.append(_find_skill_dir().parent / OUTREACHMAGIC_NAME)

    for skills_dir in SKILL_SEARCH_PATHS:
        candidates.append(skills_dir / OUTREACHMAGIC_NAME)

    seen: set[str] = set()
    for p in candidates:
        key = str(p.resolve())
        if key in seen:
            continue
        seen.add(key)
        if (p / "scripts" / "pipeline.py").exists():
            return p

    return None


def get_pipeline_path(om_dir: Path) -> Path:
    """Return path to pipeline.py."""
    return om_dir / "scripts" / "pipeline.py"


# ── Company matching (dedup) ─────────────────────────────────────────────────

_COMPANY_STOPWORDS = frozenset({
    "inc", "llc", "ltd", "corp", "corporation", "company", "co", "the", "and",
    "group", "holdings", "international", "intl",
})


def normalize_company_name(name: str) -> str:
    """Lowercase company token for fuzzy comparison."""
    text = (name or "").lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    tokens = [t for t in text.split() if t and t not in _COMPANY_STOPWORDS]
    return " ".join(tokens)


def companies_match(expected: str, actual: str) -> bool:
    """True when two company names likely refer to the same organization."""
    a = normalize_company_name(expected)
    b = normalize_company_name(actual)
    if not a or not b:
        return True  # cannot verify — do not block dedup
    if a == b:
        return True
    if a in b or b in a:
        return True
    a_tokens, b_tokens = set(a.split()), set(b.split())
    if not a_tokens or not b_tokens:
        return True
    overlap = len(a_tokens & b_tokens) / min(len(a_tokens), len(b_tokens))
    return overlap >= 0.5


def lead_company_display(lead: dict[str, Any]) -> str:
    return (lead.get("company_display") or lead.get("company") or "").strip()


# ── Dedup check ──────────────────────────────────────────────────────────────

def _history_lookup(
    pipeline: str,
    base_args: list[str],
    extra_args: list[str],
    timeout: int,
) -> Optional[dict[str, Any]]:
    """Run pipeline history --json and return lead dict or None."""
    try:
        proc = subprocess.run(
            ["python3", pipeline, *base_args, *extra_args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_subprocess_env(),
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        data = json.loads(proc.stdout)
        if isinstance(data, dict) and data.get("error"):
            return None
        if isinstance(data, dict) and data.get("lead"):
            return data["lead"]
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None
    return None


def _apply_lead_match(
    result: dict[str, Any],
    lead: dict[str, Any],
    *,
    input_company: Optional[str],
    force: bool,
    raw: Optional[str] = None,
) -> None:
    """Merge lead fields into result and set status (with company validation)."""
    result["lead_id"] = lead.get("id")
    result["name"] = lead.get("name")
    result["company"] = lead_company_display(lead) or lead.get("company")
    result["email"] = lead.get("email")
    result["linkedin_url"] = lead.get("linkedin_url")
    if raw:
        result["raw"] = raw

    db_company = result["company"] or ""
    if (
        not force
        and input_company
        and db_company
        and not companies_match(input_company, db_company)
    ):
        result["status"] = "ambiguous"
        result["ambiguous_lead_id"] = lead.get("id")
        result["matched_company"] = db_company
        result["lead_id"] = None
        result["note"] = (
            f"Name match but company mismatch: expected {input_company!r}, "
            f"DB has {db_company!r}. Run Serper or use --force to override."
        )
        return

    if result["linkedin_url"]:
        result["status"] = "exists_linkedin"
    elif result["email"]:
        result["status"] = "exists_no_linkedin"
        result["has_email"] = True
    else:
        result["status"] = "exists_no_linkedin"


def check_lead_exists(
    om_dir: Path,
    name: str,
    company: Optional[str] = None,
    linkedin: Optional[str] = None,
    workspace: Optional[str] = None,
    timeout: int = 10,
    *,
    force: bool = False,
    dedup_before_search: bool = True,
) -> dict[str, Any]:
    """Check if a lead exists in outreachmagic.

    Returns status:
        not_found, exists_linkedin, exists_no_linkedin, ambiguous, dedup_disabled
    """
    result: dict[str, Any] = {
        "status": "not_found",
        "lead_id": None,
        "linkedin_url": None,
        "email": None,
        "name": None,
        "company": None,
        "raw": None,
    }

    if not dedup_before_search and not force:
        result["status"] = "dedup_disabled"
        result["note"] = "dedup_before_search is false in config — proceed with Serper"
        return result

    if force:
        result["status"] = "not_found"
        result["force"] = True
        result["note"] = "force=true — ignoring existing DB matches"
        return result

    pipeline = str(get_pipeline_path(om_dir))
    hist_cmd = ["history", "--json", "--limit", "0"]
    if workspace:
        hist_cmd.extend(["--workspace", workspace])

    # LinkedIn hint is a strong identity match
    if linkedin:
        slug = linkedin.strip("/").split("/")[-1] if "/" in linkedin else linkedin
        lead = _history_lookup(pipeline, hist_cmd, ["--linkedin", slug], timeout)
        if lead:
            _apply_lead_match(result, lead, input_company=company, force=False)
            return result

    if name:
        lead = _history_lookup(pipeline, hist_cmd, ["--name", name], timeout)
        if lead:
            _apply_lead_match(
                result,
                lead,
                input_company=company,
                force=False,
                raw=json.dumps({"lead": lead}),
            )

    return result


def batch_check(
    om_dir: Path,
    people: list[dict[str, str]],
    workspace: Optional[str] = None,
    timeout: int = 10,
    *,
    dedup_before_search: bool = True,
) -> list[dict[str, Any]]:
    """Run dedup check for multiple people."""
    results = []
    for person in people:
        name = person.get("full_name") or person.get("name", "")
        company = person.get("company_name") or person.get("company", "")
        linkedin = person.get("linkedin_url") or person.get("linkedin", "")
        force = bool(person.get("force", False))
        result = check_lead_exists(
            om_dir,
            name,
            company,
            linkedin,
            workspace,
            timeout,
            force=force,
            dedup_before_search=dedup_before_search,
        )
        result["_input"] = person
        results.append(result)
    return results


# ── Input normalization ──────────────────────────────────────────────────────

def normalize_person(person: dict[str, Any]) -> dict[str, Any]:
    """Normalize a single person input."""
    out: dict[str, Any] = {}

    # full_name
    full = (person.get("full_name") or person.get("name") or "").strip()
    out["full_name"] = re.sub(r"\s+", " ", full)

    # company_name
    co = (person.get("company_name") or person.get("company") or "").strip()
    out["company_name"] = re.sub(r"\s+", " ", co)

    # stated_role
    role = (person.get("stated_role") or person.get("title") or person.get("role") or "").strip()
    out["stated_role"] = re.sub(r"\s+", " ", role) if role else ""

    # linkedin_url (hint — user already has this)
    li = (person.get("linkedin_url") or person.get("linkedin") or "").strip()
    out["linkedin_url"] = li

    # tags
    tags = person.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in re.split(r"[,;\n]+", tags) if t.strip()]
        tags = [t.lower().replace(" ", "_") for t in tags]
    out["tags"] = list(dict.fromkeys(tags))  # dedupe, preserve order

    # Metadata
    out["import_name"] = person.get("import_name", "")
    out["list_source"] = person.get("list_source", "")
    out["workspace"] = person.get("workspace", "")
    out["force"] = bool(person.get("force", False))

    return out


def normalize_input(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize single or batch input into list of people."""
    # Single person
    if "full_name" in data or "name" in data:
        return [normalize_person(data)]

    # Batch
    people = data.get("people", [data])

    # Batch-level metadata — flows down to each person (individual overrides win)
    batch_tags = data.get("tags", [])
    if isinstance(batch_tags, str):
        batch_tags = [t.strip() for t in re.split(r"[,;\n]+", batch_tags) if t.strip()]
        batch_tags = [t.lower().replace(" ", "_") for t in batch_tags]
    batch_import_name = data.get("import_name", "")
    batch_list_source = data.get("list_source", "")
    batch_workspace = data.get("workspace", "")

    normalized = []
    for p in people:
        np = normalize_person(p)
        # Apply batch-level defaults (individual values take precedence)
        if batch_tags and not np.get("tags"):
            np["tags"] = batch_tags
        if batch_import_name and not np.get("import_name"):
            np["import_name"] = batch_import_name
        if batch_list_source and not np.get("list_source"):
            np["list_source"] = batch_list_source
        if batch_workspace and not np.get("workspace"):
            np["workspace"] = batch_workspace
        normalized.append(np)

    # Validate
    for p in normalized:
        if not p["full_name"] or not p["company_name"]:
            raise ValueError(
                f"full_name and company_name are required. "
                f"Got: name={p['full_name']!r}, company={p['company_name']!r}"
            )

    # Cap batch size
    cfg = load_config()
    max_people = cfg.get("max_people_per_run", 50)
    if len(normalized) > max_people:
        raise ValueError(f"Batch size {len(normalized)} exceeds limit of {max_people}")

    return normalized


# ── Serper query building ────────────────────────────────────────────────────

def build_role_fragment(stated_role: str, max_words: int = 5, max_chars: int = 80) -> str:
    """Extract up to max_words from stated_role, capped at max_chars."""
    if not stated_role:
        return ""
    words = stated_role.split()
    frag = ""
    for w in words[:max_words]:
        test = frag + (" " if frag else "") + w
        if len(test) > max_chars:
            break
        frag = test
    return frag


def build_serper_queries(person: dict[str, Any]) -> list[dict[str, str]]:
    """Build the Serper query pack for one person.

    Returns list of {label, query, always, fallback_query}.
    Always-run queries come first, conditional ones after.
    """
    name = person["full_name"]
    company = person["company_name"]
    role = person.get("stated_role", "")
    role_frag = build_role_fragment(role)

    queries: list[dict[str, str]] = []

    # 2a — Company strict (always)
    queries.append({
        "label": "company_discovery_strict",
        "query": f'"{company}" official website',
        "always": True,
        "fallback_query": f"{company} website",
    })

    # 2b — Company broad (conditional placeholder — agent decides)
    queries.append({
        "label": "company_discovery_broad",
        "query": f"{company} official website",
        "always": False,
        "fallback_query": "",
        "condition": "No organic results with http(s) links in strict search",
    })

    # 2c — LinkedIn primary (always)
    primary_li = f"site:linkedin.com/in {name}"
    if role_frag:
        primary_li += f" {role_frag}"
    primary_li += f' "{company}"'
    queries.append({
        "label": "linkedin_profile_strict",
        "query": primary_li,
        "always": True,
        "fallback_query": f"site:linkedin.com/in {name} {company}",
    })

    # 2d — LinkedIn follow-up (conditional)
    followup_li = f"site:linkedin.com/in {name}"
    if role_frag:
        followup_li += f" {role_frag}"
    followup_li += f" {company}"
    queries.append({
        "label": "linkedin_profile_broad",
        "query": followup_li,
        "always": False,
        "fallback_query": "",
        "condition": "No /in/ URLs in strict LinkedIn search, or no title matches name",
    })

    return queries


def build_curl_command(query: str, config: dict[str, Any]) -> str:
    """Build a curl command string for a Serper query (API key via env, not embedded)."""
    endpoint = config["serper_endpoint"]
    num = config.get("serper_num_results", 10)
    gl = config.get("serper_gl", "us")
    hl = config.get("serper_hl", "en")
    body = json.dumps({"q": query, "num": num, "gl": gl, "hl": hl})
    return (
        f"curl -s -X POST {endpoint} "
        f"-H \"X-API-KEY: $SERPER_API_KEY\" "
        f"-H 'Content-Type: application/json' "
        f"-d '{body}'"
    )


def serper_search(query: str, config: dict[str, Any]) -> dict[str, Any]:
    """Run one Serper search via stdlib HTTP. Raises ValueError on missing key or HTTP error."""
    api_key = (config.get("serper_api_key") or "").strip()
    if not api_key:
        raise ValueError(
            "serper_api_key not set — add SERPER_API_KEY to ~/.hermes/.env, "
            "config.json, or export SERPER_API_KEY"
        )

    payload = json.dumps({
        "q": query,
        "num": config.get("serper_num_results", 10),
        "gl": config.get("serper_gl", "us"),
        "hl": config.get("serper_hl", "en"),
    }).encode("utf-8")

    req = urllib.request.Request(
        config["serper_endpoint"],
        data=payload,
        headers={
            "X-API-KEY": api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise ValueError(f"Serper HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise ValueError(f"Serper request failed: {e}") from e


# ── Serper result formatting ─────────────────────────────────────────────────

def format_serper_for_model(
    sections: list[dict[str, Any]],
) -> str:
    """Format Serper results into a prompt-ready text block for the model.

    Each section: {label, query, data: Serper JSON response}.
    """
    parts: list[str] = []

    for section in sections:
        label = section.get("label", "unknown")
        query = section.get("query", "")
        data = section.get("data", {})

        parts.append(f"## {label}")
        parts.append(f"Query: {query}")
        parts.append("")

        # Knowledge graph
        kg = data.get("knowledgeGraph", {})
        if kg:
            parts.append("### Knowledge Graph")
            parts.append(f"- Title: {kg.get('title', 'N/A')}")
            parts.append(f"- Type: {kg.get('type', 'N/A')}")
            parts.append(f"- Website: {kg.get('website', 'N/A')}")
            desc = kg.get("description", "")
            if desc:
                parts.append(f"- Description: {desc}")
            parts.append("")

        # Organic results
        organic = data.get("organic", [])
        if organic:
            parts.append(f"### Organic Results ({len(organic)})")
            for i, row in enumerate(organic, 1):
                parts.append(f"{i}. **{row.get('title', 'Untitled')}**")
                parts.append(f"   Link: {row.get('link', 'N/A')}")
                snippet = row.get("snippet", "")
                if snippet:
                    parts.append(f"   Snippet: {snippet}")
                sitelinks = row.get("sitelinks", [])
                if sitelinks:
                    for sl in sitelinks[:5]:
                        parts.append(f"   → {sl.get('title', '')}: {sl.get('link', '')}")
                parts.append("")

        # People also ask
        paa = data.get("peopleAlsoAsk", [])
        if paa:
            parts.append(f"### People Also Ask ({len(paa)})")
            for item in paa[:5]:
                parts.append(f"- Q: {item.get('question', '')}")
                snippet = item.get("snippet", "")
                if snippet:
                    parts.append(f"  A: {snippet}")
            parts.append("")

        parts.append("---")
        parts.append("")

    return "\n".join(parts)


# ── Outreach Magic field mapping ─────────────────────────────────────────────

def map_to_outreachmagic(
    person: dict[str, Any],
    enrichment: dict[str, Any],
) -> dict[str, Any]:
    """Map research output to outreachmagic import-profiles format.

    enrichment: {company_domain, company_website, linkedin_url, confidence, note}
    """
    notes_parts: list[str] = []

    # Import metadata
    import_name = person.get("import_name", "")
    list_source = person.get("list_source", "")
    if import_name:
        notes_parts.append(f"[import_name: {import_name}]")
    if list_source:
        notes_parts.append(f"[list_source: {list_source}]")

    # Enrichment details
    domain = enrichment.get("company_domain", "")
    website = enrichment.get("company_website", "")
    confidence = enrichment.get("confidence", "")
    note = enrichment.get("note", "")

    if website:
        notes_parts.append(f"website: {website}")
    if confidence:
        notes_parts.append(f"confidence: {confidence}")
    if note:
        notes_parts.append(note)

    notes_str = " | ".join(notes_parts)

    linkedin = enrichment.get("linkedin_url", "") or person.get("linkedin_url", "")
    # Normalize LinkedIn: strip protocol, trailing slash
    if linkedin:
        linkedin = re.sub(r"^https?://(www\.)?", "", linkedin).rstrip("/")

    profile: dict[str, Any] = {
        "name": person.get("full_name", ""),
        "company": person.get("company_name", ""),
    }

    if person.get("stated_role"):
        profile["job_title"] = person["stated_role"]

    if linkedin:
        profile["linkedin"] = linkedin

    if domain:
        profile["company_domain"] = domain

    if notes_str:
        profile["notes"] = notes_str

    tags = person.get("tags", [])
    if tags:
        profile["tags"] = tags if isinstance(tags, list) else [tags]

    can_import = bool(linkedin or profile.get("email"))
    workspace = person.get("workspace", "")
    import_name = person.get("import_name", "")

    return {
        "profile": profile,
        "can_import_via_import_profiles": can_import,
        "linkedin": linkedin,
        "workspace": workspace,
        "import_command": _build_import_command(profile, workspace, import_name) if can_import else None,
    }


def _build_import_command(profile: dict[str, Any], workspace: str, import_name: str = "") -> str:
    """Build the import-profiles shell command."""
    json_str = json.dumps([profile])
    cmd = f"python3 {{outreachmagic_home}}/scripts/pipeline.py import-profiles --json '{json_str}'"

    # Default source-detail stamps every import from this skill
    source_detail = "lead-enrich"
    if import_name:
        source_detail += f"/{import_name}"
    cmd += f" --source-detail \"{source_detail}\""

    if workspace:
        cmd += f" --workspace {workspace}"
    return cmd


# ── Report formatting ────────────────────────────────────────────────────────

def format_report(results: list[dict[str, Any]]) -> str:
    """Format final report for the user."""
    lines: list[str] = []
    lines.append(f"# Lead Enrich Results ({len(results)} people)")
    lines.append("")

    totals = {"skipped": 0, "enriched": 0, "saved": 0, "unsaved": 0, "serper_queries": 0}

    for i, r in enumerate(results, 1):
        person = r.get("_input", {})
        name = person.get("full_name", "Unknown")
        company = person.get("company_name", "Unknown")
        status = r.get("status", "unknown")
        enrichment = r.get("enrichment", {})
        om_result = r.get("outreachmagic", {})

        lines.append(f"## {i}. {name} @ {company}")

        if status == "exists_linkedin":
            lines.append(f"  ⏭️  Already in outreachmagic — skipped (0 Serper credits)")
            totals["skipped"] += 1
            lines.append("")
            continue

        if status == "ambiguous":
            lines.append(f"  ⚠️  Name match, company mismatch — run Serper (see note)")
            if r.get("note"):
                lines.append(f"  📝 {r['note']}")
            totals["enriched"] += 1
            lines.append("")
            continue

        if status == "exists_no_linkedin":
            lines.append(f"  ⚠️  Exists but no LinkedIn — researching LinkedIn only")
        else:
            lines.append(f"  🔍 Researched via Serper")

        domain = enrichment.get("company_domain", "")
        website = enrichment.get("company_website", "")
        linkedin = enrichment.get("linkedin_url", "")
        confidence = enrichment.get("confidence", "low")

        if domain:
            lines.append(f"  ✅ Company domain: {domain}")
        if website:
            lines.append(f"  ✅ Website: {website}")
        if linkedin:
            lines.append(f"  ✅ LinkedIn: {linkedin}")
        else:
            lines.append(f"  ❌ LinkedIn: not found")

        conf_map = {"high": "🟢", "medium": "🟡", "low": "🔴"}
        lines.append(f"  {conf_map.get(confidence, '⚪')} Confidence: {confidence}")

        if enrichment.get("note"):
            lines.append(f"  📝 {enrichment['note']}")

        saved = om_result.get("saved", False)
        if saved:
            lead_id = om_result.get("lead_id", "?")
            lines.append(f"  💾 Saved to outreachmagic (lead #{lead_id})")
            totals["saved"] += 1
        else:
            lines.append(f"  ⚠️  Not saved — no LinkedIn or email to match on")
            totals["unsaved"] += 1

        queries = r.get("serper_queries_run", 0)
        lines.append(f"  🔎 Serper queries: {queries}")
        totals["serper_queries"] += queries
        totals["enriched"] += 1
        lines.append("")

    # Summary
    lines.append("---")
    lines.append(f"**Skipped** (already in DB): {totals['skipped']} | "
                 f"**Enriched**: {totals['enriched']} | "
                 f"**Saved**: {totals['saved']} | "
                 f"**Unsaved**: {totals['unsaved']}")
    lines.append(f"**Total Serper credits used:** {totals['serper_queries']}")

    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────

def cmd_config() -> None:
    """Print loaded config (mask API key)."""
    ensure_hermes_env_loaded()
    cfg = load_config()
    if cfg.get("serper_api_key"):
        key = cfg["serper_api_key"]
        cfg["serper_api_key"] = key[:4] + "..." + key[-4:] if len(key) > 8 else "***"

    om_dir = find_outreachmagic(cfg)
    cfg["_outreachmagic_detected"] = str(om_dir) if om_dir else "NOT FOUND"
    cfg["_hermes_env"] = str(_hermes_home() / ".env")
    cfg["_outreachmagic_agent_key_set"] = bool(
        os.environ.get("OUTREACHMAGIC_AGENT_KEY", "").strip()
    )

    print(json.dumps(cfg, indent=2))


def cmd_check(
    name: str,
    company: str,
    workspace: str = "",
    *,
    force: bool = False,
) -> None:
    """Dedup check for a single person."""
    cfg = load_config()
    om_dir = find_outreachmagic(cfg)
    if not om_dir:
        print(json.dumps({"error": "outreachmagic not found"}))
        sys.exit(1)

    result = check_lead_exists(
        om_dir,
        name,
        company,
        workspace=workspace or None,
        force=force,
        dedup_before_search=bool(cfg.get("dedup_before_search", True)),
    )
    print(json.dumps(result, indent=2))


def cmd_batch_check(input_file: str, workspace: str = "") -> None:
    """Batch dedup check from JSON file."""
    cfg = load_config()
    om_dir = find_outreachmagic(cfg)
    if not om_dir:
        print(json.dumps({"error": "outreachmagic not found"}))
        sys.exit(1)

    data = json.loads(Path(input_file).read_text())
    # Allow CLI workspace to override batch-level workspace
    if workspace:
        data["workspace"] = workspace
    people = normalize_input(data)
    ws = data.get("workspace", "")
    results = batch_check(
        om_dir,
        people,
        workspace=ws or None,
        dedup_before_search=bool(cfg.get("dedup_before_search", True)),
    )
    print(json.dumps(results, indent=2))


def cmd_normalize(input_file: str) -> None:
    """Normalize input data."""
    data = json.loads(Path(input_file).read_text())
    people = normalize_input(data)
    print(json.dumps(people, indent=2))


def cmd_serper_queries(input_file: str) -> None:
    """Print Serper query pack for each person."""
    data = json.loads(Path(input_file).read_text())
    people = normalize_input(data)
    cfg = load_config()

    output = []
    for p in people:
        queries = build_serper_queries(p)
        entry = {
            "person": {"full_name": p["full_name"], "company_name": p["company_name"]},
            "queries": [],
        }
        for q in queries:
            entry["queries"].append({
                "label": q["label"],
                "query": q["query"],
                "always": q["always"],
                "curl": build_curl_command(q["query"], cfg),
                "serper_search_cmd": (
                    f"python3 scripts/enrich.py serper-search --query {json.dumps(q['query'])}"
                ),
                "fallback_query": q.get("fallback_query", ""),
                "fallback_curl": build_curl_command(q["fallback_query"], cfg) if q.get("fallback_query") else "",
                "condition": q.get("condition", ""),
            })
        output.append(entry)

    print(json.dumps(output, indent=2))


def cmd_serper_search(query: str, label: str = "") -> None:
    """Run one Serper search and print JSON."""
    cfg = load_config()
    data = serper_search(query, cfg)
    out: dict[str, Any] = {"query": query, "data": data}
    if label:
        out["label"] = label
    print(json.dumps(out, indent=2))


def cmd_serper_format(input_file: str) -> None:
    """Format Serper results for model extraction."""
    data = json.loads(Path(input_file).read_text())
    sections = data if isinstance(data, list) else [data]
    formatted = format_serper_for_model(sections)
    print(formatted)


def cmd_map_to_om(input_file: str, workspace: str = "") -> None:
    """Map research output to outreachmagic import format."""
    data = json.loads(Path(input_file).read_text())

    # Support single or batch
    items = data if isinstance(data, list) else [data]
    results = []
    for item in items:
        person = item.get("person", {})
        # CLI workspace overrides person-level workspace
        if workspace and not person.get("workspace"):
            person["workspace"] = workspace
        enrichment = item.get("enrichment", {})
        mapped = map_to_outreachmagic(person, enrichment)
        mapped["_person"] = person
        results.append(mapped)

    print(json.dumps(results, indent=2))


def _parse_cli_flags(argv: list[str]) -> tuple[str, bool, list[str]]:
    """Extract --workspace and --force from argv."""
    workspace = ""
    force = False
    remaining: list[str] = []
    skip = False
    for i, arg in enumerate(argv):
        if skip:
            skip = False
            continue
        if arg == "--force":
            force = True
            continue
        if arg == "--workspace" and i + 1 < len(argv):
            workspace = argv[i + 1]
            skip = True
            continue
        if arg.startswith("--workspace="):
            workspace = arg.split("=", 1)[1]
            continue
        remaining.append(arg)
    return workspace, force, remaining


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    workspace, force, argv = _parse_cli_flags(sys.argv[1:])
    cmd = argv[0] if argv else ""

    if cmd == "config":
        cmd_config()
    elif cmd == "check":
        if len(argv) < 3:
            print("Usage: enrich.py check [--workspace W] [--force] 'Name' 'Company'")
            sys.exit(1)
        cmd_check(argv[1], argv[2], workspace, force=force)
    elif cmd == "serper-search":
        query = ""
        label = ""
        args = argv[1:]
        i = 0
        while i < len(args):
            if args[i] == "--query" and i + 1 < len(args):
                query = args[i + 1]
                i += 2
            elif args[i].startswith("--query="):
                query = args[i].split("=", 1)[1]
                i += 1
            elif args[i] == "--label" and i + 1 < len(args):
                label = args[i + 1]
                i += 2
            else:
                i += 1
        if not query:
            print("Usage: enrich.py serper-search --query \"search terms\" [--label NAME]")
            sys.exit(1)
        cmd_serper_search(query, label)
    elif cmd == "batch-check":
        if len(argv) < 2:
            print("Usage: enrich.py batch-check [--workspace W] input.json")
            sys.exit(1)
        cmd_batch_check(argv[1], workspace)
    elif cmd == "normalize":
        if len(argv) < 2:
            print("Usage: enrich.py normalize input.json")
            sys.exit(1)
        cmd_normalize(argv[1])
    elif cmd == "serper-queries":
        if len(argv) < 2:
            print("Usage: enrich.py serper-queries input.json")
            sys.exit(1)
        cmd_serper_queries(argv[1])
    elif cmd == "serper-format":
        if len(argv) < 2:
            print("Usage: enrich.py serper-format results.json")
            sys.exit(1)
        cmd_serper_format(argv[1])
    elif cmd == "map-to-om":
        if len(argv) < 2:
            print("Usage: enrich.py map-to-om [--workspace W] research_output.json")
            sys.exit(1)
        cmd_map_to_om(argv[1], workspace)
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
