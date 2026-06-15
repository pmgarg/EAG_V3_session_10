"""
layers/vision.py — Layer 3 fallback.

When the AX tree comes back empty after activation, when the target
element is missing from the tree, or when the subgoal is inherently
visual ("click the button that looks like a triangle"), we escalate
here.

Steps:

  1. capture: get_window_state with capture_mode="vision" returns the
     screenshot path. This requires Screen Recording grant.
  2. ask:    qwen3-vl reads the screenshot + the subgoal text, returns
     pixel coordinates in window-local space.
  3. click:  driver.click_xy(pid, x, y).

This is roughly 10× the per-turn cost of Layer 2b (Axiom session text)
because we send a JPEG instead of a few KB of markdown. Use only when
the lower layers have actually exhausted themselves — the Recovery
classifier decides when to call us.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agent10 import driver, gateway

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "vision.md"


@dataclass
class VisionVerdict:
    verdict: str       # "click_xy" | "done" | "no_target"
    x: int
    y: int
    rationale: str


def _system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def capture_window(pid: int, window_id: int) -> str:
    """Take a fresh screenshot via cua-driver's vision capture mode.
    Returns the path to a PNG on disk.

    Requires Screen Recording TCC grant on macOS. If the grant is
    missing the call raises DriverError(permission_blocked) which the
    Recovery layer maps to `blocked`.

    The cua-driver 0.5.3 response shape (verified empirically):
        screenshot_png_b64   — inline base64 PNG (this is the usual one)
        screenshot_mime_type — e.g. "image/png"
        screenshot_width / screenshot_height — dimensions
    Older driver versions used screenshot_path / image_path / png_path
    or screenshot_b64; we check all of them.
    """
    state = driver.get_window_state(pid, window_id, mode="vision")
    # Path forms (older drivers).
    for key in ("screenshot_path", "image_path", "png_path"):
        if state.get(key):
            return state[key]
    # Inline base64. cua-driver 0.5.3 uses screenshot_png_b64.
    b64 = (
        state.get("screenshot_png_b64")
        or state.get("screenshot_b64")
        or state.get("image_b64")
    )
    if not b64:
        raise driver.DriverError(
            "vision capture returned no screenshot path or b64 payload. "
            f"Response keys: {list(state.keys())[:10]}",
            code="tool_error",
        )
    fd, path = tempfile.mkstemp(suffix=".png", prefix="s10-vision-")
    with os.fdopen(fd, "wb") as f:
        f.write(base64.b64decode(b64))
    return path


def judge_screenshot(image_path: str, subgoal: str, *,
                     model: Optional[str] = None) -> VisionVerdict:
    """Ask qwen3-vl where to click. Returns a structured verdict."""
    model = model or gateway.DEFAULT_VISION_MODEL
    sys = _system_prompt()
    user = (
        f"Subgoal: {subgoal}\n\n"
        "Return the JSON object. Coordinates are window-local pixels."
    )
    full = f"{sys}\n\n{user}"
    reply = gateway.vision(model=model, prompt=full, image_path=image_path)
    text = reply["content"].strip()
    # Strip markdown fences if the model added them despite the prompt.
    if text.startswith("```"):
        text = text.strip("`").lstrip("json").strip()
        if text.endswith("```"):
            text = text[:-3].strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Try to find the first { ... } block in the text.
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            parsed = json.loads(text[start:end + 1])
        else:
            raise gateway.GatewayError(
                f"vision model returned non-JSON: {text[:300]}"
            )
    return VisionVerdict(
        verdict=str(parsed.get("verdict", "no_target")),
        x=int(parsed.get("x", 0)),
        y=int(parsed.get("y", 0)),
        rationale=str(parsed.get("rationale", "")),
    )


def attempt(pid: int, window_id: int, subgoal: str) -> dict:
    """Full Layer 3 turn: capture → judge → click. Returns a dict the
    Executor can fold into its trajectory record."""
    img = capture_window(pid, window_id)
    verdict = judge_screenshot(img, subgoal)
    out: dict = {
        "layer": "3",
        "screenshot_path": img,
        "verdict": verdict.verdict,
        "x": verdict.x,
        "y": verdict.y,
        "rationale": verdict.rationale,
    }
    if verdict.verdict == "click_xy":
        click_resp = driver.click_xy(pid, verdict.x, verdict.y)
        out["click_response"] = click_resp
    return out
