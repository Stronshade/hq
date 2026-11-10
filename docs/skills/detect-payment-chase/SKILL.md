# Skill: detect-payment-chase

## When to use

Find vendors chasing payment and how far they went; check whether referenced invoices exist in the inbox.

## How it works

After classify + extract, scan for chase language and stages:

- `reminder` · `escalation` · `final_notice`

Join referenced invoice numbers against known `agent.invoice_fields`.

## Outputs

- `agent.vendor_chase_events`: vendor, stage, referenced invoice numbers, `invoice_present_in_inbox`, confidence, rationale.

## Failure modes

- Ambiguous language → lower confidence or skip.
- Errors written on the event row / pipeline failures list—not dropped.
