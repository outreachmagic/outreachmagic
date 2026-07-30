# Agent triage — working the pending-decision queues

Three queues collect judgements the pipeline deliberately refuses to make on its
own. Each one is a question a model can usually answer from evidence, and
occasionally cannot answer at all. This document is the decision rules; the
dashboard's **Copy agent prompt** buttons point here.

Replace `<SKILLS>` with your skills directory (`~/.claude/skills` or
`~/.hermes/skills`). All commands are `python3 <SKILLS>/outreachmagic/scripts/pipeline.py …`.

**The rule that governs all three:** a wrong merge is not undoable from these
tools. Being unsure is a valid outcome — leave the item pending, say so, and ask.
Never guess to clear a queue.

---

## 1. Research decisions — which search result is the right one

Serper research finds candidates, not answers. Nine people share a name; the
picker exists because ranking is not a recommendation.

```bash
# What is waiting. --field is linkedin | title | company_domain
pipeline.py serper-review --field linkedin --limit 25 --json

# Record decisions. All-or-nothing: a partial write cannot happen.
pipeline.py serper-apply --batch --json '[
  {"lead_id": 184145, "field": "linkedin", "value": "https://linkedin.com/in/janedoe"},
  {"lead_id": 184146, "field": "linkedin", "dismissed": true}
]'
```

### Deciding

Each candidate carries the query it came from and the surrounding search result.
Compare it against what the lead record already claims.

**Choose a candidate when at least two of these agree** and none contradict:
- the name matches (including obvious diminutives — Jen/Jennifer, Bob/Robert)
- the company matches the lead's company or its email domain
- the title is consistent with the lead's stated role

**Choose `dismissed: true` ("none of these")** when:
- every candidate is a different person with the same name
- the only match is the name and nothing else corroborates it
- the candidates are all company pages and the field wanted a person

"None of these" is a recorded answer, not the absence of one — it removes the
lead from the queue so a re-run does not ask again. Use it freely; it is cheap
and correct. What is expensive is attaching the wrong LinkedIn profile.

**For `company_domain`:** the domain must be the company's *own* site. Reject
aggregators (LinkedIn, Crunchbase, ZoomInfo, Bloomberg), directory listings, and
shared mail hosts. If the lead's email domain is already a professional domain,
that is usually the answer and the search is corroboration.

**Stop and ask** when the lead is a common name at a large company and the search
returns several plausible people at that company. There is no evidence that
separates them, and picking one is a coin flip recorded as a fact.

---

## 2. Company merges — are these two records one company

```bash
# Highest-confidence first; work HIGH before touching LOW.
pipeline.py company merge-review list --min-confidence HIGH --limit 50
pipeline.py company merge-review list --reason name_only_domain_attach --limit 50

# --keep-id names the survivor. Omitted, existing_company_id survives.
pipeline.py company merge-review approve --id cmc_abc123 --keep-id 80536
pipeline.py company merge-review reject --id cmc_abc123 --note "different companies, same name"
```

Each row gives you both sides flattened: `existing_name` / `existing_domain` /
`existing_leads` and `candidate_name` / `candidate_domain` / `candidate_leads`,
plus `confidence` and `reason`. Lead counts are read live, not from the queued
payload.

### Deciding

**Merge when:**
- the domains share a registrable domain (`acme.com` / `mail.acme.com`) — this is
  what `confidence: HIGH` means and it is close to mechanical
- one side's domain redirects to the other's, or both resolve to the same site
- the names differ only by a legal suffix or punctuation (`Acme Inc` / `Acme, Inc.`)

**Keep separate when:**
- the names match but the domains are unrelated businesses — a generic company
  name is not evidence, and `name_only_domain_attach` is exactly this case
- one is a franchise, chapter, or regional entity of the other and they are sold
  to independently
- either domain is a shared mail host or a parked page

You have web search. Use it: opening both domains settles most of these in one
step, and is the difference between a decision and a guess.

### Choosing the survivor

`--keep-id` picks which record lives. The survivor's own values win field by
field, and it inherits anything it left blank from the record merged away — so
nothing is lost either way, but the survivor's id is what other things reference.

Prefer the record with **more leads attached**, then the one with a **real
primary domain**, then the more complete profile (industry, headcount, HQ).

**Stop and ask** when both sides carry substantial lead counts and the domains
are genuinely different businesses that happen to share a name. Merging those
silently corrupts two accounts at once.

---

## 3. Contact merges — are these two records one person

Queued as `lead_merge_jobs` with `reason` of `identity_conflict` (two records
claimed the same LinkedIn URL or Sales Navigator id) or `email_find_conflict`
(the email finder returned an address that already belongs to another record).

```bash
pipeline.py merge-review list --reason identity_conflict --limit 50
pipeline.py merge-review approve --id merge_abc123
pipeline.py merge-review reject --id merge_abc123 --note "father and son, same name"
```

### Deciding

An `identity_conflict` is strong evidence on its own: a LinkedIn profile URL
belongs to exactly one person, so two leads holding it are almost always one
person recorded twice — typically once from a list import and once from a
sequencer reply.

**Merge when:**
- the conflicting identity is a LinkedIn URL or Sales Navigator id (these are
  unique by construction)
- the names are the same person's (including diminutives and married names) and
  the companies match or one is a former employer

**Keep separate when:**
- the shared identity is a *shared mailbox* (`info@`, `sales@`, `hello@`) — that
  is a company address, not a person
- the names are plainly different people
- one record is a `company_placeholder` and the other is a real contact; link the
  contact to the company instead of merging the two records

`audit_json` on each job carries a summary of both leads. `keep_lead_id` is the
record that already owned the identity, which is usually the older and
better-established one — approve as-is unless the other record has the history.

**Stop and ask** when the two records have conflicting *reply* histories, since
merging collapses two conversations into one timeline.

---

## Reporting back

After working a queue, say:
- how many you decided and which way
- the reasoning for anything that was not mechanical
- **everything you left pending, and what you would need in order to decide it**

The last one is the point. A queue that goes from 2,294 to 40 with 40 real
questions attached is a good outcome; a queue that goes to zero because
everything got a plausible-looking answer is not.
