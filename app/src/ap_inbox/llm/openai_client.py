"""OpenAI-compatible async client — works for OpenAI and Ollama (`/v1`).

Always passes num_ctx for Ollama so tool-call quality does not depend on ambient defaults.
Logs a context accounting object per call.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from ap_inbox.chunking import count_tokens
from ap_inbox.config import Settings, get_settings
from ap_inbox.llm.context import account_prompt

logger = logging.getLogger(__name__)


def build_client(settings: Settings | None = None) -> AsyncOpenAI:
    s = settings or get_settings()
    return AsyncOpenAI(api_key=s.effective_api_key, base_url=s.effective_base_url)


def _ollama_extra_body(settings: Settings) -> dict[str, Any] | None:
    if settings.llm_provider.lower() != "ollama":
        return None
    return {"options": {"num_ctx": settings.llm_num_ctx}}


async def stream_chat(
    *,
    messages: list[dict[str, str]],
    settings: Settings | None = None,
) -> AsyncIterator[str]:
    s = settings or get_settings()
    acct = account_prompt([m.get("content", "") for m in messages], settings=s, model=s.chat_model)
    if acct.truncated:
        logger.warning("stream_chat context overflow: %s", acct.to_dict())
    client = build_client(s)
    try:
        kwargs: dict[str, Any] = {
            "model": s.chat_model,
            "messages": messages,
            "stream": True,
            "temperature": 0.2,
        }
        extra = _ollama_extra_body(s)
        if extra:
            kwargs["extra_body"] = extra
        stream = await client.chat.completions.create(**kwargs)
        async for chunk in stream:
            choice = chunk.choices[0] if chunk.choices else None
            if choice is None:
                continue
            delta = choice.delta
            text = getattr(delta, "content", None)
            if text:
                yield text
    finally:
        await client.close()


async def complete_chat(
    *,
    messages: list[dict[str, str]],
    settings: Settings | None = None,
) -> str:
    s = settings or get_settings()
    acct = account_prompt([m.get("content", "") for m in messages], settings=s, model=s.chat_model)
    logger.info("complete_chat account=%s prompt_chars=%s", acct.to_dict(), sum(len(m.get("content", "")) for m in messages))
    _ = count_tokens  # kept imported for callers / accounting symmetry
    client = build_client(s)
    try:
        kwargs: dict[str, Any] = {
            "model": s.chat_model,
            "messages": messages,
            "stream": False,
            "temperature": 0.2,
        }
        extra = _ollama_extra_body(s)
        if extra:
            kwargs["extra_body"] = extra
        resp = await client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "") if resp.choices else ""
    finally:
        await client.close()
