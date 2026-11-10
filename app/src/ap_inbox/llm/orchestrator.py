"""LangChain tool-calling orchestrator (no LangGraph).

Chat is read-only. Maps tool progress → SSE status, tool failures → gap, tokens → token.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_ollama import ChatOllama

from ap_inbox.chunking import count_tokens
from ap_inbox.config import Settings, get_settings
from ap_inbox.llm.context import account_prompt, clip_chunks_to_budget
from ap_inbox.llm.prompts import ORCHESTRATOR_SYSTEM_PROMPT
from ap_inbox.llm.tools import InternalToolClient, build_tools_for_role

logger = logging.getLogger(__name__)

GAP_CODES = frozenset(
    {
        "missing_file",
        "unreadable",
        "tool_denied",
        "low_confidence",
        "context_overflow",
        "no_matches",
        "tool_error",
        "http_error",
    }
)


@dataclass
class OrchestratorEvent:
    event: str
    data: dict[str, Any]


@dataclass
class OrchestratorState:
    run_id: str
    gaps: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    answer_confidence: float = 0.45


def _parse_tool_payload(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"raw": data}
    except Exception:  # noqa: BLE001
        return {"raw": raw}


def _extract_gaps_from_tool(name: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    if payload.get("error"):
        code = str(payload.get("code") or "tool_error")
        if code not in GAP_CODES:
            code = "tool_error"
        gaps.append(
            {
                "kind": code,
                "fil_id": payload.get("fil_id"),
                "message_id": payload.get("message_id"),
                "detail": payload.get("detail") or f"{name} failed",
            }
        )
        return gaps

    # Attachment meta statuses
    if name == "list_attachment_meta":
        for item in payload.get("items") or []:
            status = item.get("status")
            if status in {"missing_file", "unreadable", "low_confidence"}:
                gaps.append(
                    {
                        "kind": status,
                        "fil_id": item.get("fil_id"),
                        "message_id": item.get("message_id"),
                        "detail": item.get("error_detail") or status,
                    }
                )
    return gaps


def _extract_citations(name: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    cites: list[dict[str, Any]] = []
    if name in {"search_messages", "get_invoice_fields", "get_chase_events", "search_chunks"}:
        for item in payload.get("items") or []:
            mid = item.get("message_id")
            if not mid:
                continue
            conf = item.get("annotation_confidence") or item.get("overall_confidence") or item.get("score") or 0.5
            note = (
                item.get("subject")
                or item.get("vendor")
                or item.get("invoice_number")
                or item.get("source_type")
                or name
            )
            cites.append({"message_id": str(mid), "confidence": float(conf), "note": str(note)[:160]})
    if name == "get_annotations" and payload.get("found"):
        ann = payload.get("annotation") or {}
        cites.append(
            {
                "message_id": str(payload.get("message_id")),
                "confidence": float(ann.get("confidence") or 0.5),
                "note": ann.get("doc_type") or "annotation",
            }
        )
    return cites


def _build_llm(settings: Settings) -> ChatOllama:
    # ChatOllama passes num_ctx into Ollama options — never rely on ambient defaults.
    return ChatOllama(
        model=settings.chat_model,
        base_url=settings.ollama_host,
        temperature=0.2,
        num_ctx=settings.llm_num_ctx,
    )


async def run_orchestrator(
    question: str,
    *,
    settings: Settings | None = None,
    prefetched_chunks: list[dict[str, Any]] | None = None,
) -> AsyncIterator[OrchestratorEvent]:
    s = settings or get_settings()
    state = OrchestratorState(run_id=str(uuid.uuid4()))
    t0 = time.perf_counter()

    yield OrchestratorEvent("status", {"stage": "retrieval", "detail": "searching chunks + preparing tools"})

    client = InternalToolClient(s)
    tools = build_tools_for_role("orchestrator", client)
    tools_by_name = {t.name: t for t in tools}

    try:
        chunks = prefetched_chunks
        if chunks is None:
            # Prefer gateway so attachment text path stays HTTP-only from LLM POV
            raw = await client.search_chunks(question, limit=s.llm_max_chunks_per_turn)
            payload = _parse_tool_payload(raw)
            chunks = list(payload.get("items") or [])
            for g in _extract_gaps_from_tool("search_chunks", payload):
                state.gaps.append(g)
                yield OrchestratorEvent("gap", g)

        doc_budget = max(1000, s.llm_soft_prompt_budget - 4000)
        kept, clipped = clip_chunks_to_budget(chunks or [], remaining_tokens=doc_budget)
        if clipped:
            gap = {
                "kind": "context_overflow",
                "fil_id": None,
                "message_id": None,
                "detail": f"truncated retrieved chunks to fit ~{doc_budget} token doc budget",
            }
            state.gaps.append(gap)
            yield OrchestratorEvent("gap", gap)

        for ch in kept:
            mid = ch.get("message_id")
            if mid:
                cite = {
                    "message_id": str(mid),
                    "confidence": float(ch.get("score") or 0.5),
                    "note": f"{ch.get('source_type')}#{ch.get('chunk_index')}",
                }
                state.citations.append(cite)
                yield OrchestratorEvent("citation", cite)

        yield OrchestratorEvent(
            "status",
            {
                "stage": "denote_context",
                "detail": f"retrieved {len(kept)} chunks, {len(state.gaps)} gaps",
            },
        )

        evidence = "\n\n---\n\n".join(
            f"[{c.get('source_type')} msg={c.get('message_id')} fil={c.get('fil_id')} "
            f"idx={c.get('chunk_index')} score={c.get('score')}]\n{c.get('text')}"
            for c in kept
        ) or "(no chunk evidence yet — use tools)"

        user_content = (
            f"Question:\n{question}\n\n"
            f"Retrieved evidence chunks (may be incomplete):\n{evidence}\n\n"
            "Use tools if you need invoices, chase events, annotations, or attachment text. "
            "Never invent missing PDFs. Surface gaps explicitly."
        )

        acct = account_prompt(
            [ORCHESTRATOR_SYSTEM_PROMPT, user_content, " ".join(t.name for t in tools)],
            settings=s,
        )
        yield OrchestratorEvent(
            "status",
            {
                "stage": "context_account",
                "detail": acct.to_dict(),
            },
        )
        if acct.truncated:
            gap = {
                "kind": "context_overflow",
                "fil_id": None,
                "message_id": None,
                "detail": acct.detail or "prompt exceeds soft budget",
            }
            state.gaps.append(gap)
            yield OrchestratorEvent("gap", gap)
            # Summarize evidence rather than silent truncate
            user_content = (
                f"Question:\n{question}\n\n"
                f"Evidence was truncated for context budget "
                f"({acct.prompt_tokens}/{acct.soft_prompt_budget} tokens). "
                "Use tools to fetch only the specific facts needed.\n"
                f"Partial evidence:\n{evidence[:4000]}"
            )

        llm = _build_llm(s).bind_tools(tools)
        messages: list[Any] = [
            SystemMessage(content=ORCHESTRATOR_SYSTEM_PROMPT),
            HumanMessage(content=user_content),
        ]

        yield OrchestratorEvent("status", {"stage": "model", "detail": f"tool loop on {s.chat_model}"})

        final_text = ""
        for iteration in range(s.llm_max_tool_iterations):
            iter_t0 = time.perf_counter()
            ai: AIMessage = await llm.ainvoke(messages)
            latency_ms = int((time.perf_counter() - iter_t0) * 1000)
            messages.append(ai)

            tool_calls = getattr(ai, "tool_calls", None) or []
            if not tool_calls:
                final_text = (ai.content or "") if isinstance(ai.content, str) else str(ai.content or "")
                break

            yield OrchestratorEvent(
                "status",
                {
                    "stage": "tools",
                    "detail": f"iteration {iteration + 1}: {[tc.get('name') for tc in tool_calls]}",
                },
            )

            for tc in tool_calls:
                name = tc.get("name") or ""
                args = tc.get("args") or {}
                call_id = tc.get("id") or str(uuid.uuid4())
                tool = tools_by_name.get(name)
                if tool is None:
                    result = json.dumps(
                        {"error": True, "code": "tool_denied", "detail": f"tool not allowed: {name}"}
                    )
                else:
                    try:
                        result = await tool.ainvoke(args)
                        if not isinstance(result, str):
                            result = json.dumps(result)
                    except Exception as exc:  # noqa: BLE001
                        result = json.dumps({"error": True, "code": "tool_error", "detail": str(exc)})

                payload = _parse_tool_payload(result)
                # Guard: empty success is forbidden for attachment text failures
                if name == "fetch_attachment_text" and not payload.get("error") and not (
                    payload.get("text") or payload.get("chunks")
                ):
                    payload = {
                        "error": True,
                        "code": "unreadable",
                        "detail": "attachment text tool returned empty payload",
                        "fil_id": args.get("fil_id"),
                    }
                    result = json.dumps(payload)

                state.tool_trace.append(
                    {
                        "tool": name,
                        "args": args,
                        "latency_ms": latency_ms,
                        "error": bool(payload.get("error")),
                        "code": payload.get("code"),
                    }
                )

                for g in _extract_gaps_from_tool(name, payload):
                    state.gaps.append(g)
                    yield OrchestratorEvent("gap", g)

                for c in _extract_citations(name, payload):
                    if c not in state.citations:
                        state.citations.append(c)
                        yield OrchestratorEvent("citation", c)

                # Context guard on tool result size
                result_tokens = count_tokens(result)
                if result_tokens > 4000:
                    payload = {
                        **payload,
                        "text": (payload.get("text") or "")[:6000],
                        "truncated_for_context": True,
                    }
                    if "chunks" in payload and isinstance(payload["chunks"], list):
                        payload["chunks"] = payload["chunks"][:6]
                    result = json.dumps(payload)
                    gap = {
                        "kind": "context_overflow",
                        "fil_id": payload.get("fil_id"),
                        "message_id": payload.get("message_id"),
                        "detail": f"tool {name} result truncated ({result_tokens} tokens)",
                    }
                    state.gaps.append(gap)
                    yield OrchestratorEvent("gap", gap)

                messages.append(ToolMessage(content=result, tool_call_id=call_id))
        else:
            # Max iterations hit — ask for a final answer without tools
            yield OrchestratorEvent(
                "status",
                {"stage": "model", "detail": "max tool iterations reached; requesting final answer"},
            )
            messages.append(
                HumanMessage(
                    content="Stop calling tools. Provide the best answer you can with evidence so far, "
                    "and list remaining gaps."
                )
            )
            ai = await llm.ainvoke(messages)
            final_text = (ai.content or "") if isinstance(ai.content, str) else str(ai.content or "")

        if not final_text.strip():
            final_text = (
                "I could not produce an answer from the available tools and evidence. "
                "Check gaps for missing or unreadable attachments."
            )
            state.answer_confidence = 0.2

        # Stream tokens in modest chunks for SSE UX (contract: token events)
        chunk_size = 48
        for i in range(0, len(final_text), chunk_size):
            yield OrchestratorEvent("token", {"text": final_text[i : i + chunk_size]})

        if state.gaps:
            state.answer_confidence = min(state.answer_confidence, 0.7)
        if not state.citations and not kept:
            state.answer_confidence = min(state.answer_confidence, 0.25)
            if not any(g.get("kind") == "no_matches" for g in state.gaps):
                gap = {
                    "kind": "no_matches",
                    "fil_id": None,
                    "message_id": None,
                    "detail": "no messages/chunks matched the question",
                }
                state.gaps.append(gap)
                yield OrchestratorEvent("gap", gap)

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "orchestrator_done run_id=%s model=%s num_ctx=%s tools=%s elapsed_ms=%s gaps=%s",
            state.run_id,
            s.chat_model,
            s.llm_num_ctx,
            len(state.tool_trace),
            elapsed_ms,
            len(state.gaps),
        )

        yield OrchestratorEvent(
            "done",
            {
                "answer_confidence": round(state.answer_confidence, 4),
                "run_id": state.run_id,
                "trace": {
                    "model": s.chat_model,
                    "num_ctx": s.llm_num_ctx,
                    "elapsed_ms": elapsed_ms,
                    "tool_calls": state.tool_trace,
                },
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("orchestrator failed")
        yield OrchestratorEvent("error", {"message": str(exc), "recoverable": False})
        yield OrchestratorEvent("done", {"answer_confidence": 0.0, "run_id": state.run_id})
    finally:
        await client.aclose()
