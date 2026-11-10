# Skill: get-chase-events

## When to use

Answer “who is chasing us?” and “reminder vs escalation vs final notice?” plus whether the invoice is in the inbox.

## Tool

- Name: `get_chase_events`
- HTTP: `GET /internal/chase?vendor=&stage=&present=&limit=`

## Outputs

- `total`, `returned`, `truncated`, `stage_counts` (counted before `LIMIT`), `match_policy`, and `items`.
- Each item includes `subject`, `stage`, `referenced_invoice_numbers`, `invoice_present_in_inbox`, confidence, and `message_id`.
- Stored stages are `reminder`, `escalation`, and `final_notice`. `final` and `final notice` alias to `final_notice`.
- `present` filters `invoice_present_in_inbox`. Present means exact case-insensitive equality against extracted invoice numbers. A junk token can look present. An empty `referenced_invoice_numbers` list means no number was extracted.
- The page keeps at least one row of each matching stage.

If a final-notice subject is a thank-you or a utility shutoff, say it is not an unpaid AP invoice.

## Failure modes

- Empty list if no chase annotations; say so. Do not invent stages.
