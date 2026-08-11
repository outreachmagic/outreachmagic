# Dashboard panes

Every slide-in pane in the local dashboard has a stable id and a printed
breadcrumb. The id here, the key in `PANES` in `dashboard.html`, and the
breadcrumb shown at the top of the pane are the same string — so a pane can be
named in writing or to an agent instead of described ("the thing that opens when
you click a bounce row").

Say **"open the deliverability › sender bounce detail pane"**, or use the id
directly: `deliverability.bounce`.

| Pane id | Breadcrumb | Opens from | Shows |
|---|---|---|---|
| `contact.detail` | Contacts › Contact detail | any contact row, anywhere | stage, identity, tags, emails, phones, enrichment, **source (original + latest)**, company link, event history |
| `contact.merge` | Contacts › Merge contacts | Merge on Contacts, Campaigns → Replies, or a company's contact list | pick the surviving record; server-computed preview of the result |
| `company.detail` | Companies › Company detail | a company row, or a contact's company link | attributes, every domain, public emails, contacts (with **email / verification / LinkedIn**), activity |
| `deliverability.mailbox` | Deliverability › Mailbox detail | a mailbox row, or a "trending down" flag | provider, limits, warmup, and the daily sends/bounces behind the trend verdict |
| `deliverability.domain` | Deliverability › Sending domain detail | a sending-domain row | reseller, provider, cost, sending IP, registration date, mailboxes |
| `deliverability.blacklist` | Deliverability › Blacklist report | the Blacklist cell on a domain row | every DNSBL checked, its verdict, and when — including lists that refused |
| `deliverability.bounce` | Deliverability › Bounce detail | a bounce row in the Bounces card | the full bounce message, what we sent before it, and three sibling sets: **same campaign**, **same sender**, **same sending domain** |
| `campaign.subjects` | Campaigns › Subject lines & copy | a campaign row | the exact copy sent, per subject line |
| `quality.bucket` | Data quality › Bucket detail | a data-quality tile | the rows in that bucket |
| `sync.outbox` | Sync › Outbox item detail | an outbox row on the Sync tab | queue metadata, the live record, and the exact payload that will push |

## Navigation

Panes that link to each other push onto a back stack, so **← Back** returns to
the pane you came from rather than closing everything. This covers lead ↔
company and bounce → bounce (a sibling bounce keeps the one you started on).
The ✕ and the overlay dismiss the whole stack.

## Adding a pane

1. Add its id and breadcrumb to `PANES` in `dashboard.html`.
2. Pass the id as the third argument to `openEditPanel`, or as `pane:` on
   `openDetailDrawer`.
3. Add a row to the table above.
4. If it links to another pane, call `navPush(type, id)` before opening the next
   one, and add `type` to `NAV_OPENERS`.
