# Skill: get-invoice-fields

## When to use

Look up extracted invoice key fields by vendor and/or invoice number.

## Tool

- Name: `get_invoice_fields`
- HTTP: `GET /internal/invoices?vendor=&invoice_number=&limit=`

## Outputs

- List of invoice field rows with confidence and status.

## Failure modes

- Empty list if nothing matches; do not invent amounts.
