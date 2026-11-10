# Skill: search-messages

## When to use

Chat needs keyword hits across subjects, bodies, vendors, or invoice numbers.

## Tool

- Name: `search_messages`
- HTTP: `GET /internal/messages/search?q=&limit=`

## Inputs

- `q` (string), `limit` (1–40).

## Outputs

- Message summaries + optional annotation confidence.

## Failure modes

- No matches → empty `items` (orchestrator may emit `no_matches`).
