"""
layers/planner.py — Layer above the substrate: goal decomposition.

The Planner is one LLM call (gemma4:12b via Ollama). It reads the user's
natural-language goal and emits:

  - the app to launch (bundle id + display name)
  - the Electron flag + debugging port if relevant
  - 2-5 ordered subgoals with verify post-conditions

The Executor walks the subgoals in order. Each subgoal goes through the
five-layer cascade.

Why a separate Planner instead of letting the Executor improvise per
subgoal: the same decomposition is reusable across replays. A trajectory
that records the Planner output + the dispatched actions reproduces
exactly when the LLM is offline.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agent10 import gateway

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "planner.md"


@dataclass
class AppTarget:
    bundle_id: str
    name: str
    electron: bool = False
    electron_debugging_port: Optional[int] = None


@dataclass
class Subgoal:
    id: str
    intent: str
    verify: str


@dataclass
class Plan:
    rationale: str
    app: AppTarget
    subgoals: list[Subgoal]
    raw: dict


def _system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def plan(user_goal: str, *,
         model: Optional[str] = None) -> Plan:
    """Call the Planner LLM and parse its output into a typed Plan.

    Falls back to a deterministic "open the app and ask the user" plan if
    the LLM returns malformed JSON twice. We don't want a Planner crash
    to take down the whole agent run.
    """
    model = model or gateway.DEFAULT_PLANNER_MODEL
    sys = _system_prompt()
    user = f"User goal: {user_goal}\n\nReturn the JSON object."
    # Up to 3 attempts with progressively larger num_predict. Gemma
    # models sometimes spend thousands of tokens on a `<think>` block
    # before the actual JSON, so we have to give them room. The
    # response_format="json" hint helps but isn't a hard cap.
    last_err: Exception | None = None
    text = ""
    for attempt_budget in (4000, 8000, 16000):
        try:
            reply = gateway.chat(
                model=model,
                messages=[
                    {"role": "system", "content": sys},
                    {"role": "user", "content": user},
                ],
                temperature=0.4,
                max_tokens=attempt_budget,
                response_format="json",
            )
            text = reply["content"].strip()
            if text:
                break
        except gateway.GatewayError as e:
            last_err = e
            text = ""
    if not text:
        raise last_err or gateway.GatewayError(
            "planner: both attempts returned empty content"
        )
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # One retry at lower temp.
        retry = gateway.chat(
            model=model,
            messages=[
                {"role": "system", "content": sys},
                {"role": "user", "content": user},
                {"role": "assistant", "content": text},
                {"role": "user", "content":
                 "That was not valid JSON. Return ONLY the JSON object now."},
            ],
            temperature=0.1,
            max_tokens=2000,
            response_format="json",
        )
        parsed = json.loads(retry["content"].strip())

    app_dict = parsed.get("app") or {}
    subgoal_list: list[Subgoal] = []
    for i, s in enumerate(parsed.get("subgoals") or []):
        if isinstance(s, dict):
            subgoal_list.append(Subgoal(
                id=str(s.get("id", f"s{i+1}")),
                intent=str(s.get("intent", "") or s.get("text", "")),
                verify=str(s.get("verify", "")),
            ))
        elif isinstance(s, str):
            # Model returned a bare list of strings; treat each as an
            # intent with no verify post-condition.
            subgoal_list.append(Subgoal(
                id=f"s{i+1}", intent=s, verify="",
            ))
    return Plan(
        rationale=parsed.get("rationale", ""),
        app=AppTarget(
            bundle_id=app_dict.get("bundle_id", ""),
            name=app_dict.get("name", ""),
            electron=bool(app_dict.get("electron", False)),
            electron_debugging_port=app_dict.get("electron_debugging_port"),
        ),
        subgoals=subgoal_list,
        raw=parsed,
    )
