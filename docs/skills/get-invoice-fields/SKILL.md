# Skill: get-invoice-fields

## When to use

Look up extracted invoice key fields by vendor, invoice number, and/or status. Use `status=unreadable` for PDFs whose text extract was empty.

## Tool

- Name: `get_invoice_fields`
- HTTP: `GET /internal/invoices?vendor=&invoice_number=&status=&limit=`

## Outputs

- `total`, `returned`, `truncated`, and `items`.
- A named number with `total=0` returns `found: false`. Do not invent an amount.
- Digit-only equality (including one leading zero) runs only when the query contains a 6+ digit run. `ZZ-DOES-NOT-EXIST-999` stays a literal and does not match `999`.
- `status=unreadable` adds `by_error_detail` and a few sample rows. Amounts on those rows are null.

If `truncated` is true, say so and do not invent rows past `items`. When amount, dates, and confidence are already present, do not open the PDF.

## Failure modes

- Named number with zero rows → `found: false` and a single `no_matches` gap.
- Unreadable rows → one `unreadable` summary gap quoting `error_detail`.
