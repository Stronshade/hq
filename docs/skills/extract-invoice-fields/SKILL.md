# Skill: extract-invoice-fields

## When to use

Pull key invoice fields from email bodies and readable PDF/text attachments.

## How it works

1. Regex extract: vendor, invoice_number, amount, currency, invoice_date, due_date, po_number.
2. `pypdf` for PDF text; empty/scanned PDFs → `unreadable` (no hallucination).
3. Optional LLM merge when invoice-like and number/amount missing or overall confidence low.

## Inputs

- Message subject/body, vendor hints, attachment paths with `status=ok`.

## Outputs

- Rows in `agent.invoice_fields` with per-field and overall confidence.

## Failure modes

- `unreadable` / `missing_file` / `low_confidence` / `error` with `error_detail`.
