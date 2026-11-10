"""OpenAI-compatible chat client.

Works for any host that speaks `/v1/chat/completions` (OpenAI, Groq, Together,
OpenRouter, Ollama, vLLM, LM Studio). When `LLM_PROVIDER=ollama`, requests also
send `num_ctx` so context does not fall back to Ollama's 4k default.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
from openai import AsyncOpenAI, OpenAI

from ap_inbox.chunking import count_tokens
from ap_inbox.config import Settings, get_settings
from ap_inbox.llm.context import account_prompt

logger = logging.getLogger(__name__)


def build_client(settings: Settings | None = None) -> AsyncOpenAI:
    s = settings or get_settings()
    return AsyncOpenAI(api_key=s.effective_api_key, base_url=s.effective_base_url)


def build_sync_client(settings: Settings | None = None, *, timeout: float = 600.0) -> OpenAI:
    s = settings or get_settings()
    return OpenAI(
        api_key=s.effective_api_key,
        base_url=s.effective_base_url,
        timeout=httpx.Timeout(timeout, connect=30.0),
    )


def ollama_extra_body(
    settings: Settings,
    *,
    num_ctx: int | None = None,
    disable_think: bool = False,
) -> dict[str, Any] | None:
    """Ollama-only request extras. Other providers get a plain chat call."""
    if not settings.is_ollama:
        return None
    ctx = settings.llm_num_ctx if num_ctx is None else num_ctx
    body: dict[str, Any] = {"options": {"num_ctx": ctx}}
    if disable_think:
        body["think"] = False
    return body


def _completion_kwargs(
    settings: Settings,
    *,
    messages: list[dict[str, str]],
    temperature: float,
    stream: bool,
    max_tokens: int | None = None,
    json_mode: bool = False,
    num_ctx: int | None = None,
    disable_think: bool = False,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": settings.chat_model,
        "messages": messages,
        "temperature": temperature,
        "stream": stream,
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if json_mode and not settings.is_ollama:
        kwargs["response_format"] = {"type": "json_object"}
    extra = ollama_extra_body(settings, num_ctx=num_ctx, disable_think=disable_think)
    if extra:
        kwargs["extra_body"] = extra
    return kwargs


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
        stream = await client.chat.completions.create(
            **_completion_kwargs(s, messages=messages, temperature=0.2, stream=True)
        )
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
    logger.info(
        "complete_chat account=%s prompt_chars=%s",
        acct.to_dict(),
        sum(len(m.get("content", "")) for m in messages),
    )
    _ = count_tokens  # kept imported for callers / accounting symmetry
    client = build_client(s)
    try:
        resp = await client.chat.completions.create(
            **_completion_kwargs(s, messages=messages, temperature=0.2, stream=False)
        )
        return (resp.choices[0].message.content or "") if resp.choices else ""
    finally:
        await client.close()


def complete_chat_sync(
    *,
    messages: list[dict[str, str]],
    settings: Settings | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
    json_mode: bool = False,
    timeout: float = 600.0,
    num_ctx: int | None = None,
    disable_think: bool = False,
) -> str:
    """Blocking chat completion for DenoteWorker refine."""
    s = settings or get_settings()
    client = build_sync_client(s, timeout=timeout)
    try:
        resp = client.chat.completions.create(
            **_completion_kwargs(
                s,
                messages=messages,
                temperature=temperature,
                stream=False,
                max_tokens=max_tokens,
                json_mode=json_mode,
                num_ctx=num_ctx,
                disable_think=disable_think,
            )
        )
        return (resp.choices[0].message.content or "") if resp.choices else ""
    finally:
        client.close()
