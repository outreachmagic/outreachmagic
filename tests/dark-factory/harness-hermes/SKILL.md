---
name: test-harness
description: "Skill test runner — validates skills against a test catalog."
version: 1.0.0
---

# Test Harness Mode

You are in **TEST MODE**. When asked to run a test suite from a catalog file:

## Procedure

1. Read the catalog JSON file specified by the user (e.g. `/workspace/dark-factory-tests/catalog.json`)
2. Parse the test cases. Each test has: `id`, `tags`, `prompt`, `expect`
3. For each test case in order (respecting any tag/ID filters the user gave):
   a. Announce: `TEST [id]: Running...`
   b. Execute the `prompt` **exactly as written** — use the skill being tested
   c. After getting the output, validate against each field in `expect`
   d. Announce: `TEST [id]: PASS` or `TEST [id]: FAIL — <specific reason>`
4. Write results JSON to the output path the user specified
5. Final line: `PASS: N / FAIL: M`

## Validation Rules

| expect field | How to check |
|-------------|-------------|
| `has_email: true` | Output contains `@` followed by a domain |
| `has_email: false` | Output does NOT contain `@` with a domain |
| `domain_contains: "X"` | Any email in output has domain containing substring X |
| `no_personal_email: true` | No gmail.com, yahoo.com, hotmail.com, outlook.com, aol.com, icloud.com, proton.me, mail.com |
| `has_company_size: true` | Output mentions employee count or company size |
| `has_recent_news: true` | Output mentions news, announcement, launched, raised, acquired, funding, or recent event |
| `has_tech_stack: true` | Output mentions specific technologies, tools, frameworks, or platforms |
| `has_confidence: true` | Output mentions confidence score, percentage, or rating |
| `contains_string: "X"` | Output contains substring X (case-insensitive) |
| `min_length: N` | Output is at least N characters long |

## Rules

- Run **EVERY** test even if some fail
- Do not deviate from test prompt wording
- If a tool call fails, that's a FAIL — note the error
- Never fabricate results
- Write the results file **before** the final summary
