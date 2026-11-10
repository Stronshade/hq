# Skill: get-annotations

## When to use

Need the classified `doc_type` and confidence for one `message_id`, or inbox-wide counts (how many invoices, reminders, low-confidence `other`, and why).

## Tool

- Name: `get_annotations`
- HTTP: `GET /internal/messages/{message_id}/annotations`
- HTTP (no id): `GET /internal/annotations?doc_type=&status=&limit=`

## Outputs

- One message: `{ found, message_id, annotation: { doc_type, confidence, rationale, model, status, error_detail } }`
- Summary: `by_doc_type`, `by_status`, and `rationale_histogram` for `low_confidence` and `error` (count, confidence, rationale, error_detail).
- With `doc_type` and/or `status`: also `total`, `returned`, `truncated`, and a short sample (`message_id`, subject, confidence, rationale).

Quote `total` and the histogram. Do not relabel low-confidence `other` as invoice or reminder.

## Failure modes

- Invalid UUID → `tool_denied`.
- Not found → `found: false` (not an invented type).
- A low-confidence filter emits one summary `low_confidence` gap, not one gap per row.
