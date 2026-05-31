# Email Finding — Research & Waterfall

Optional Phase 5 after Serper enrichment saves `company_domain` and `linkedin_url`.
Requires `TRYKITT_API_KEY` for trykitt.ai; other providers are manual fallbacks.

## trykitt.ai (primary — finder + verifier)

Combined email finder and SMTP verifier in one call. Best used when you already
have `fullName`, `domain`, and ideally `linkedinStandardProfileURL` from Phase 2–4.

### API

| | |
|---|---|
| **Endpoint** | `POST https://api.trykitt.ai/job/find_email` |
| **Auth** | Header `x-api-key: $TRYKITT_API_KEY` |
| **Key format** | 28-char alphanumeric (get at https://trykitt.ai — free tier, no card) |

**Request body (required fields in bold):**

```json
{
  "fullName": "Jane Doe",
  "domain": "acme.com",
  "linkedinStandardProfileURL": "https://linkedin.com/in/janedoe",
  "realtime": true
}
```

**Example:**

```bash
curl -s -X POST https://api.trykitt.ai/job/find_email \
  -H "x-api-key: $TRYKITT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"fullName":"Jane Doe","domain":"acme.com","linkedinStandardProfileURL":"https://linkedin.com/in/janedoe","realtime":true}'
```

**Response (success):**

```json
{
  "email": "jane@acme.com",
  "validity": "valid",
  "validSMTP": true,
  "mxDomain": "alt1.aspmx.l.google.com",
  "jobId": "01KS..."
}
```

`validity` values include `valid`, `valid-risky`, and empty when not found.

### Tested performance (NACE award-winners batch, n=59)

| Metric | Result |
|--------|--------|
| Find rate | 38/59 (~65%) |
| SMTP-confirmed valid | 18 |
| valid-risky | 20 |
| Agreement vs MillionVerifier (valid) | ~77% |
| Agreement vs MillionVerifier (risky) | ~81% |

### Rate limits (free tier)

- Throttles at **~10 concurrent** requests → HTTP 500 with message like
  `"free tier API is busy"`.
- **Batch workaround:** sleep **8+ seconds** between requests for 50+ leads,
  or contact trykitt for higher concurrency.
- `/credit` may show `0` while requests still process.

### Credit check endpoint

```bash
curl -s https://api.trykitt.ai/credit -H "x-api-key: $TRYKITT_API_KEY"
```

---

## Waterfall order

Use in this order; stop when a deliverable email is saved to outreachmagic.

| Step | Provider | When |
|------|----------|------|
| 0 | **outreachmagic DB** | Always — skip APIs if lead already has email (unless bounced re-find) |
| 1 | **trykitt.ai** | `TRYKITT_API_KEY` set + `company_domain` known |
| 2 | **Icypeas** | trykitt miss or no key |
| 3 | **LeadMagic** | Icypeas miss |
| 4 | **Findymail** | Last resort |

After trykitt (hit or miss), tag the lead `trykitt_attempted` via
`import-profiles` or `add-tag` so batch re-runs do not burn credits again.

### Bounced email re-find

If the user reports a bounce on a saved email:

1. Clear or note the bad email in outreachmagic.
2. Remove `trykitt_attempted` tag (or use `--force` on check).
3. Re-run Phase 5 with the same domain + LinkedIn context.

---

## Saving found emails

Prefer the email-finder CLI (tags `trykitt_attempted` on hit and miss when `--save` / batch):

```bash
python3 ~/.hermes/skills/email-finder/scripts/email_finder.py find \
  --name "Jane Doe" --domain acme.com \
  --linkedin "https://linkedin.com/in/janedoe" --save --workspace your_workspace
```

Or via outreachmagic directly:

```bash
python3 {outreachmagic_home}/scripts/pipeline.py import-profiles \
  --workspace your_workspace \
  --source-detail "email-finder/trykitt" \
  --json '[{"name":"Jane Doe","company":"Acme Corp","email":"jane@acme.com","linkedin":"linkedin.com/in/janedoe","company_domain":"acme.com","tags":["trykitt_attempted"]}]'
```

Include `validity` / `validSMTP` in `notes` when helpful for downstream sequencing.
