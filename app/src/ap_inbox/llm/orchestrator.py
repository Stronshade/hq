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
from langchain_openai import ChatOpenAI

from ap_inbox.chunking import count_tokens
from ap_inbox.config import Settings, get_settings
from ap_inbox.llm.context import account_prompt, clip_chunks_to_budget
from ap_inbox.llm.openai_client import ollama_extra_body
from ap_inbox.llm.preflight import answer_rules_for, fact_lines, plan_preflight
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


SUMMARY_GAP_KINDS = frozenset({"missing_file", "unreadable", "low_confidence", "no_matches"})


def _gap(kind: str, detail: str, *, fil_id: str | None = None, message_id: str | None = None) -> dict[str, Any]:
    return {"kind": kind, "fil_id": fil_id, "message_id": message_id, "detail": detail}


def _extract_gaps_from_tool(name: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """One summary gap per kind. Never one event per row."""
    if payload.get("error"):
        code = str(payload.get("code") or "tool_error")
        if code not in GAP_CODES:
            code = "tool_error"
        return [
            _gap(
                code,
                str(payload.get("detail") or f"{name} failed"),
                fil_id=payload.get("fil_id"),
                message_id=payload.get("message_id"),
            )
        ]

    if name == "list_attachment_meta":
        status = payload.get("status")
        total = int(payload.get("total") or 0)
        if status in {"missing_file", "unreadable", "low_confidence"} and total > 0:
            names = payload.get("by_filename") or []
            top = ""
            if names:
                top = f" Most common filename: {names[0].get('filename')} ({names[0].get('count')})."
            sample = (payload.get("items") or [{}])[0]
            detail = f"{total} attachment(s) with status={status}.{top}"
            if sample.get("error_detail"):
                detail += f" Example: {sample.get('error_detail')}"
            return [_gap(str(status), detail.strip())]
        bad = [
            item
            for item in payload.get("items") or []
            if item.get("status") in {"missing_file", "unreadable", "low_confidence"}
        ]
        if bad:
            first = bad[0]
            return [
                _gap(
                    str(first["status"]),
                    str(first.get("error_detail") or first["status"]),
                    fil_id=first.get("fil_id"),
                    message_id=first.get("message_id"),
                )
            ]
        return []

    if name == "get_invoice_fields":
        if payload.get("found") is False:
            number = payload.get("invoice_number") or "named invoice"
            return [_gap("no_matches", f"{number} matched 0 invoice rows")]
        if payload.get("by_error_detail") and int(payload.get("total") or 0) > 0:
            top = (payload.get("by_error_detail") or [{}])[0]
            detail = (
                f"{payload.get('total')} invoice row(s) with status=unreadable. "
                f"{top.get('error_detail')} ({top.get('count')}). Amounts were not extracted."
            )
            return [_gap("unreadable", detail)]
        return []

    if name == "get_annotations":
        filters = payload.get("filters") or {}
        if filters.get("status") == "low_confidence" and int(payload.get("total") or 0) > 0:
            hist = payload.get("rationale_histogram") or []
            top = hist[0] if hist else {}
            detail = f"{payload.get('total')} annotations with status=low_confidence"
            if filters.get("doc_type"):
                detail += f" and doc_type={filters['doc_type']}"
            if top.get("rationale"):
                detail += (
                    f". Top rationale ({top.get('count')} at confidence {top.get('confidence')}): "
                    f"{top.get('rationale')}"
                )
            return [_gap("low_confidence", detail)]
    return []


def _extract_citations(name: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    cites: list[dict[str, Any]] = []

    def add(message_id: Any, confidence: Any, note: Any) -> None:
        if not message_id or len(cites) >= 12:
            return
        try:
            conf = float(confidence if confidence is not None else 0.5)
        except (TypeError, ValueError):
            conf = 0.5
        cites.append({"message_id": str(message_id), "confidence": conf, "note": str(note or name)[:160]})

    if name in {"search_messages", "get_invoice_fields", "get_chase_events", "search_chunks", "get_annotations"}:
        for item in payload.get("items") or []:
            add(
                item.get("message_id"),
                item.get("annotation_confidence")
                or item.get("overall_confidence")
                or item.get("confidence")
                or item.get("score"),
                item.get("subject") or item.get("vendor") or item.get("invoice_number") or item.get("source_type") or name,
            )
    if name == "get_annotations" and payload.get("found"):
        ann = payload.get("annotation") or {}
        add(payload.get("message_id"), ann.get("confidence"), ann.get("doc_type") or "annotation")
    if name in {"list_attachment_meta", "fetch_attachment_text"}:
        if payload.get("message_id"):
            add(payload.get("message_id"), 0.6, payload.get("filename") or name)
        for item in payload.get("items") or []:
            add(item.get("message_id"), 0.5, item.get("filename") or item.get("status") or name)
    return cites


def _remember_gap(state: OrchestratorState, gap: dict[str, Any]) -> bool:
    """Keep a single summary gap for each demo kind."""
    kind = gap.get("kind")
    if kind in SUMMARY_GAP_KINDS and any(existing.get("kind") == kind for existing in state.gaps):
        return False
    state.gaps.append(gap)
    if kind == "no_matches":
        state.answer_confidence = min(state.answer_confidence, 0.25)
    return True


def _clip_payload_for_prompt(payload: dict[str, Any]) -> dict[str, Any]:
    shown = dict(payload)
    text_value = shown.get("text")
    if isinstance(text_value, str) and len(text_value) > 6000:
        shown["text"] = text_value[:6000]
        shown["text_clipped"] = True
    if isinstance(shown.get("chunks"), list):
        shown.pop("chunks", None)
        shown["chunks_omitted"] = True
    return shown


def _payload_has_evidence(payload: dict[str, Any]) -> bool:
    if payload.get("error") or payload.get("found") is False:
        return False
    if payload.get("summary") or payload.get("by_doc_type") or payload.get("text"):
        return True
    if payload.get("found") is True or payload.get("items") or int(payload.get("total") or 0) > 0:
        return True
    return False


def _build_llm(settings: Settings) -> ChatOpenAI:
    # Any OpenAI-compatible host. Ollama still receives num_ctx (its default is 4k).
    kwargs: dict[str, Any] = {
        "model": settings.chat_model,
        "api_key": settings.effective_api_key,
        "base_url": settings.effective_base_url,
        "temperature": 0.2,
    }
    extra = ollama_extra_body(settings)
    if extra:
        kwargs["extra_body"] = extra
    return ChatOpenAI(**kwargs)


async def run_orchestrator(
    question: str,
    *,
    settings: Settings | None = None,
    prefetched_chunks: list[dict[str, Any]] | None = None,
) -> AsyncIterator[OrchestratorEvent]:
    s = settings or get_settings()
    state = OrchestratorState(run_id=str(uuid.uuid4()))
    t0 = time.perf_counter()

    calls = plan_preflight(question)
    skip_chunks = bool(calls)
    yield OrchestratorEvent(
        "status",
        {
            "stage": "retrieval",
            "detail": "structured preflight" if skip_chunks else "searching chunks + preparing tools",
        },
    )

    client = InternalToolClient(s)
    tools = build_tools_for_role("orchestrator", client)
    tools_by_name = {t.name: t for t in tools}

    async def _call_tool(name: str, args: dict[str, Any]) -> tuple[dict[str, Any], str, int]:
        started = time.perf_counter()
        tool = tools_by_name.get(name)
        if tool is None:
            result = json.dumps({"error": True, "code": "tool_denied", "detail": f"tool not allowed: {name}"})
        else:
            try:
                result = await tool.ainvoke(args)
                if not isinstance(result, str):
                    result = json.dumps(result, default=str)
            except Exception as exc:  # noqa: BLE001
                result = json.dumps({"error": True, "code": "tool_error", "detail": str(exc)})
        payload = _parse_tool_payload(result)
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
        latency_ms = int((time.perf_counter() - started) * 1000)
        return payload, result, latency_ms

    def _record(name: str, args: dict[str, Any], payload: dict[str, Any], latency_ms: int) -> None:
        state.tool_trace.append(
            {
                "tool": name,
                "args": args,
                "latency_ms": latency_ms,
                "error": bool(payload.get("error")),
                "code": payload.get("code"),
            }
        )

    try:
        preflight_notes: list[str] = []
        preflight_answered = False
        if calls:
            yield OrchestratorEvent(
                "status",
                {
                    "stage": "preflight",
                    "detail": ", ".join(call.tool for call in calls),
                },
            )
            for call in calls:
                payload, _raw, latency_ms = await _call_tool(call.tool, dict(call.args))
                _record(call.tool, dict(call.args), payload, latency_ms)
                for gap in _extract_gaps_from_tool(call.tool, payload):
                    if _remember_gap(state, gap):
                        yield OrchestratorEvent("gap", gap)
                for cite in _extract_citations(call.tool, payload):
                    if cite not in state.citations:
                        state.citations.append(cite)
                        yield OrchestratorEvent("citation", cite)
                if _payload_has_evidence(payload):
                    preflight_answered = True
                shown = _clip_payload_for_prompt(payload)
                note = "\n".join(fact_lines(call.tool, shown))
                if shown.get("text"):
                    note += "\n\nAttachment text:\n" + str(shown.get("text"))
                preflight_notes.append(note)
                if call.read_text_if_ok and not payload.get("error"):
                    items = payload.get("items") or []
                    readable = any(item.get("status") == "ok" and not item.get("is_inline") for item in items)
                    if readable:
                        text_args = {"fil_id": call.args.get("fil_id")}
                        text_payload, _text_raw, text_latency = await _call_tool("fetch_attachment_text", text_args)
                        _record("fetch_attachment_text", text_args, text_payload, text_latency)
                        for gap in _extract_gaps_from_tool("fetch_attachment_text", text_payload):
                            if _remember_gap(state, gap):
                                yield OrchestratorEvent("gap", gap)
                        for cite in _extract_citations("fetch_attachment_text", text_payload):
                            if cite not in state.citations:
                                state.citations.append(cite)
                                yield OrchestratorEvent("citation", cite)
                        if _payload_has_evidence(text_payload):
                            preflight_answered = True
                        text_shown = _clip_payload_for_prompt(text_payload)
                        note = "\n".join(fact_lines("fetch_attachment_text", text_shown))
                        if text_shown.get("text"):
                            note += "\n\nAttachment text:\n" + str(text_shown.get("text"))
                        preflight_notes.append(note)

        chunks = None if skip_chunks else prefetched_chunks
        if chunks is None and not skip_chunks:
            # Prefer gateway so attachment text path stays HTTP-only from LLM POV
            raw = await client.search_chunks(question, limit=s.llm_max_chunks_per_turn)
            payload = _parse_tool_payload(raw)
            chunks = list(payload.get("items") or [])
            for gap in _extract_gaps_from_tool("search_chunks", payload):
                if _remember_gap(state, gap):
                    yield OrchestratorEvent("gap", gap)
        elif chunks is None:
            chunks = []

        doc_budget = max(1000, s.llm_soft_prompt_budget - 4000)
        kept, clipped = clip_chunks_to_budget(chunks or [], remaining_tokens=doc_budget)
        if clipped:
            gap = {
                "kind": "context_overflow",
                "fil_id": None,
                "message_id": None,
                "detail": f"truncated retrieved chunks to fit ~{doc_budget} token doc budget",
            }
            if _remember_gap(state, gap):
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
                "detail": (
                    f"preflight tools={len(state.tool_trace)}, gaps={len(state.gaps)}"
                    if skip_chunks
                    else f"retrieved {len(kept)} chunks, {len(state.gaps)} gaps"
                ),
            },
        )

        evidence = "\n\n---\n\n".join(
            f"[{c.get('source_type')} msg={c.get('message_id')} fil={c.get('fil_id')} "
            f"idx={c.get('chunk_index')} score={c.get('score')}]\n{c.get('text')}"
            for c in kept
        ) or "(chunk prefetch skipped — answer from the preflight tool evidence)"

        rules = answer_rules_for(calls) if calls else "- Use tool evidence. Surface gaps. Cite message_id."
        preflight_block = "\n\n".join(preflight_notes) or "(no structured preflight for this question)"

        def _user_message(chunk_evidence: str, note: str = "") -> str:
            return (
                f"Question:\n{question}\n\n"
                f"Answer rules:\n{rules}\n\n"
                f"Preflight tool evidence (authoritative):\n{preflight_block}\n\n"
                f"{note}"
                f"Retrieved evidence chunks:\n{chunk_evidence}\n\n"
                "Write the final answer from the preflight evidence when it is present. "
                "Do not call a tool again for facts already in that evidence. "
                "Do not open PDFs when invoice fields are already present. "
                "Never invent amounts, rows, or file contents."
            )

        user_content = _user_message(evidence)

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
            user_content = _user_message(
                evidence[:2000],
                note=(
                    f"Context note: prompt was {acct.prompt_tokens}/{acct.soft_prompt_budget} tokens. "
                    "Still quote the preflight totals below; do not invent the missing rows.\n\n"
                ),
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
                payload, result, _tool_latency = await _call_tool(name, args)
                _record(name, args, payload, latency_ms)

                for gap in _extract_gaps_from_tool(name, payload):
                    if _remember_gap(state, gap):
                        yield OrchestratorEvent("gap", gap)

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
                    if _remember_gap(state, gap):
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

        if state.gaps and not any(g.get("kind") == "no_matches" for g in state.gaps):
            state.answer_confidence = min(state.answer_confidence, 0.7)
        elif preflight_answered and not state.gaps:
            state.answer_confidence = max(state.answer_confidence, 0.82)
        if not state.citations and not kept and not preflight_answered:
            state.answer_confidence = min(state.answer_confidence, 0.25)
            if not any(g.get("kind") == "no_matches" for g in state.gaps):
                gap = {
                    "kind": "no_matches",
                    "fil_id": None,
                    "message_id": None,
                    "detail": "no messages/chunks matched the question",
                }
                if _remember_gap(state, gap):
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
