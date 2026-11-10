# Skill: search-chunks

## When to use

Retrieve ranked evidence snippets from message bodies / attachment text (hybrid SQL + embeddings when chunks exist).

## Tool

- Name: `search_chunks`
- HTTP: `GET /internal/chunks/search?q=&limit=`

## Note

Full-corpus embeddings require denote **without** `--skip-chunks`. A rules-only demo may have sparse `document_chunks`; tools still fall back to message search / annotations.

## Failure modes

- Empty retrieval → orchestrator should admit limited evidence (`no_matches` / low confidence).
- Structured demo questions skip this prefetch so a chunk that merely mentions “inbox” cannot hide `no_matches` for a named invoice that is not in the extracted fields. `search_chunks` stays available as a follow-up tool.
