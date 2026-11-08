"""Thin async OpenRouter chat-completions client (streaming + structured JSON)."""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

import httpx

API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-4o-mini"


class OpenRouterError(RuntimeError):
    pass


def model_name() -> str:
    return (os.getenv("CHAT_STREAM_MODEL") or DEFAULT_MODEL).strip()


def api_key() -> str:
    key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    if not key:
        raise OpenRouterError(
            "OPENROUTER_API_KEY is not set. Add it to AISB/.env and restart uvicorn."
        )
    return key


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}


def _error_text(payload) -> str:
    err = payload.get("error") if isinstance(payload, dict) else None
    if not err:
        return str(payload)[:300]
    if isinstance(err, dict):
        return f"{err.get('code', 'error')}: {err.get('message', err)}"
    return str(err)[:300]


async def complete_json(messages: list[dict], name: str, schema: dict, temperature: float = 0.0) -> dict:
    """One non-streaming call that must return JSON matching `schema` (strict)."""
    body = {
        "model": model_name(),
        "messages": messages,
        "temperature": temperature,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": name, "strict": True, "schema": schema},
        },
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15)) as client:
        resp = await client.post(API_URL, headers=_headers(), json=body)
    try:
        data = resp.json()
    except ValueError:
        raise OpenRouterError(f"{resp.status_code}: non-JSON response from OpenRouter")
    if resp.status_code >= 400 or (isinstance(data, dict) and data.get("error")):
        raise OpenRouterError(_error_text(data))
    try:
        return json.loads(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise OpenRouterError(f"unexpected structured response: {exc}")


async def stream_chat(messages: list[dict], temperature: float = 0.7) -> AsyncIterator[str]:
    """Yield assistant text deltas as they arrive."""
    body = {"model": model_name(), "messages": messages, "temperature": temperature, "stream": True}
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15)) as client:
        async with client.stream("POST", API_URL, headers=_headers(), json=body) as resp:
            if resp.status_code >= 400:
                raw = (await resp.aread()).decode("utf-8", "replace")
                try:
                    raw = _error_text(json.loads(raw))
                except ValueError:
                    raw = raw[:300]
                raise OpenRouterError(f"{resp.status_code}: {raw}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):  # blank keep-alives and ": OPENROUTER PROCESSING" comments
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                if isinstance(chunk, dict) and chunk.get("error"):  # mid-stream failure
                    raise OpenRouterError(_error_text(chunk))
                choices = chunk.get("choices") or []
                delta = (choices[0].get("delta") or {}).get("content") if choices else None
                if delta:
                    yield delta
