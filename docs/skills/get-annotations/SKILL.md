# Skill: get-annotations

## When to use

Need the classified `doc_type` and confidence for a specific `message_id`.

## Tool

- Name: `get_annotations`
- HTTP: `GET /internal/messages/{message_id}/annotations`

## Outputs

- `{ found, message_id, annotation: { doc_type, confidence, rationale, model, status } }`

## Failure modes

- Invalid UUID → `tool_denied`.
- Not found → `found: false` (not an invented type).
