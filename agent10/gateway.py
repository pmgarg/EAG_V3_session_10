"""
gateway.py — single point of contact with Ollama.

The session writeup talks about a "V9 gateway" — the same one Session 9
introduced. We don't have a V9 gateway in this repo yet, and the
assignment requires "no paid APIs". Ollama running locally satisfies
both: free, fully local, the same model surface (chat + vision).

If a real V9 gateway exists somewhere on disk (Session_9/llm_gatewayV9/
or similar), point GATEWAY_BASE_URL at it. Otherwise we talk to Ollama
directly on http://localhost:11434.

Two public functions:

    chat(model, messages, **opts) -> str
        Text completion via Ollama's /api/chat. Returns the assistant's
        message content. Used by Planner + Judge.

    vision(model, prompt, image_b64, **opts) -> str
        Vision completion. The model must be multimodal (qwen3-vl etc.).
        Returns the assistant's message content. Used by Layer 3.

Both functions surface model latency on the response so the cost
ledger can record per-skill, per-layer wall-clocks.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Optional

import httpx

GATEWAY_BASE_URL = os.environ.get(
    "EAGV3_GATEWAY_URL", "http://localhost:11434"
)


# Default models. Override with EAGV3_PLANNER_MODEL / EAGV3_JUDGE_MODEL /
# EAGV3_VISION_MODEL in the env if you want to swap them out without
# touching code.
DEFAULT_PLANNER_MODEL = os.environ.get("EAGV3_PLANNER_MODEL", "gemma4:12b")
DEFAULT_JUDGE_MODEL = os.environ.get("EAGV3_JUDGE_MODEL", "gemma4:12b")
DEFAULT_VISION_MODEL = os.environ.get("EAGV3_VISION_MODEL", "qwen2.5vl:7b")


class GatewayError(RuntimeError):
    """Raised when Ollama is unreachable, returns a non-2xx, or replies
    with no message content. Recovery layer keys on this to escalate."""


def _post(path: str, body: dict[str, Any], *,
          timeout: float = 120.0) -> dict[str, Any]:
    url = f"{GATEWAY_BASE_URL}{path}"
    try:
        r = httpx.post(url, json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        raise GatewayError(
            f"Ollama returned {e.response.status_code}: "
            f"{e.response.text[:300]}"
        ) from e
    except httpx.RequestError as e:
        raise GatewayError(
            f"Ollama unreachable at {url}: {e}. "
            "Is `ollama serve` running?"
        ) from e


def chat(model: str, messages: list[dict[str, str]], *,
         temperature: float = 0.2,
         max_tokens: int = 1500,
         response_format: Optional[str] = None,
         timeout: float = 300.0) -> dict[str, Any]:
    """Text chat completion. Returns:

        {"content": str, "latency_ms": int, "model": str,
         "input_tokens": int, "output_tokens": int}

    If `response_format == "json"`, Ollama is told to constrain its
    output to a single JSON object via the `format` parameter. Useful
    for the Planner and Judge.
    """
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }
    if response_format == "json":
        body["format"] = "json"
    raw = _post("/api/chat", body, timeout=timeout)
    msg = (raw.get("message") or {}).get("content", "")
    if not msg.strip():
        raise GatewayError(
            f"Ollama returned empty content for model {model}. "
            "Try a larger num_predict — `<think>` tokens may have eaten "
            "the budget."
        )
    return {
        "content": msg,
        "latency_ms": round((raw.get("total_duration") or 0) / 1e6),
        "model": raw.get("model", model),
        "input_tokens": raw.get("prompt_eval_count", 0),
        "output_tokens": raw.get("eval_count", 0),
    }


def vision(model: str, prompt: str, image_path: str | Path, *,
           temperature: float = 0.1,
           max_tokens: int = 1500) -> dict[str, Any]:
    """Single-image vision completion. The image is base64-encoded inline
    into the messages array (Ollama's expected shape). Returns the same
    dict shape as `chat()`.
    """
    image_path = Path(image_path)
    if not image_path.exists():
        raise GatewayError(f"vision image not found: {image_path}")
    b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    body: dict[str, Any] = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": prompt,
            "images": [b64],
        }],
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }
    raw = _post("/api/chat", body, timeout=300.0)
    msg = (raw.get("message") or {}).get("content", "")
    if not msg.strip():
        raise GatewayError(
            f"Vision model {model} returned empty content. "
            "Check that the model name is correct and the image is "
            "<= 5 MB."
        )
    return {
        "content": msg,
        "latency_ms": round((raw.get("total_duration") or 0) / 1e6),
        "model": raw.get("model", model),
        "input_tokens": raw.get("prompt_eval_count", 0),
        "output_tokens": raw.get("eval_count", 0),
    }


def ping() -> bool:
    """Cheap reachability probe used by the startup self-test."""
    try:
        httpx.get(f"{GATEWAY_BASE_URL}/api/tags", timeout=4.0).raise_for_status()
        return True
    except Exception:
        return False


__all__ = [
    "GatewayError",
    "chat",
    "vision",
    "ping",
    "DEFAULT_PLANNER_MODEL",
    "DEFAULT_JUDGE_MODEL",
    "DEFAULT_VISION_MODEL",
]
