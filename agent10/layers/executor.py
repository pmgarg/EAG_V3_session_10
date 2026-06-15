"""
layers/executor.py — Layer above the substrate: scan → act → verify.

This module is the heart of the computer-use skill. It walks the
planner's subgoal list, and for each subgoal it runs the cascade:

    Layer 2a  — deterministic hotkeys (Calculator)
    Layer 2b  — AX tree + cheap text LLM judgment (Notes, anything native)
    Layer 3   — screenshot + vision LLM (last resort)

The cascade discipline is enforced by `recovery.py`'s classifier: a
layer's failure mode maps to one of {retry, escalate, blocked}, and the
Executor follows that map. Hardcoding "always try Layer 3 second" would
miss the Electron escape hatch and waste vision tokens.

For Electron apps, when `plan.app.electron` is true, we skip Layer 2b
entirely and use the `page` tool (CDP) as Layer 2.5 — the AX tree on
Electron is one opaque AXWebArea, so 2b can never see the DOM. The page
path is treated as a layer 2 variant: cheap, deterministic CSS
selectors, no LLM in the loop.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from agent10 import driver, gateway, platform_
from agent10.layers import (
    cdp_client, perception, recovery, vision as vision_layer,
)
from agent10.layers.planner import Plan, Subgoal


_JUDGE_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "prompts" / "judge.md"
)


@dataclass
class TurnRecord:
    """One entry in the trajectory log."""
    subgoal_id: str
    layer: str
    action: str
    detail: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0
    error: Optional[str] = None


@dataclass
class RunResult:
    success: bool
    final_state: Optional[dict] = None
    turns: list[TurnRecord] = field(default_factory=list)
    error: Optional[str] = None
    extracted: dict[str, Any] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────
# Helpers: launch + activate + scan
# ──────────────────────────────────────────────────────────────────────

def _resolve_window(pid: int, *,
                    deadline_s: float = 5.0,
                    app_name_hint: Optional[str] = None) -> Optional[int]:
    """Wait for the app to have a top-level window after launch.

    macOS often registers MULTIPLE windows for one app — one is the
    real content window, the others are full-screen-width menu bar
    overlays at z_index ~1200. We prefer windows whose title matches
    the app name OR whose dimensions are roughly app-shaped
    (not 30-pixel-tall menu bars).
    """
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        wins = driver.list_windows(pid=pid)
        if wins:
            # Filter to content-like windows: title set, OR height > 60
            # (rules out the 30px menu bar).
            content = [
                w for w in wins
                if w.get("title")
                or (w.get("bounds") or {}).get("height", 0) > 60
            ]
            if app_name_hint:
                # Prefer one whose title contains the app name.
                titled = [
                    w for w in content
                    if app_name_hint.lower() in (w.get("title") or "").lower()
                ]
                if titled:
                    return int(titled[0]["window_id"])
            if content:
                # Among content-like, pick the smallest area (the real
                # window, not the desktop overlay).
                content.sort(key=lambda w: (
                    (w.get("bounds") or {}).get("width", 9999)
                    * (w.get("bounds") or {}).get("height", 9999)
                ))
                return int(content[0]["window_id"])
            # No content-like window yet — try again.
        time.sleep(0.25)
    # Give up: return the first window so the caller can at least try.
    if wins:
        return int(wins[0]["window_id"])
    return None


def launch_and_activate(plan: Plan) -> tuple[int, int]:
    """Launch the planned app (with electron debugging port when
    applicable), activate it so AX returns a real tree, and resolve a
    window_id. Returns (pid, window_id)."""
    qt_env_vars = platform_.qt_env()
    resp = driver.launch_app(
        plan.app.bundle_id,
        electron_debugging_port=plan.app.electron_debugging_port,
        env=qt_env_vars or None,
    )
    pid = int(resp.get("pid"))
    platform_.activate_app(plan.app.bundle_id)
    # Electron apps take significantly longer to boot than native apps
    # (Cursor cold start can be 30+ seconds). Give them a wider window.
    deadline = 30.0 if plan.app.electron else 5.0
    wid = _resolve_window(pid, deadline_s=deadline,
                          app_name_hint=plan.app.name)
    if wid is None:
        raise driver.DriverError(
            f"no window appeared for pid={pid} after launch + activate",
            code="tool_error",
        )
    return pid, wid


# ──────────────────────────────────────────────────────────────────────
# Layer 2b judge call
# ──────────────────────────────────────────────────────────────────────

def _judge(subgoal: Subgoal, tree_md: str, *,
           model: Optional[str] = None) -> dict[str, Any]:
    model = model or gateway.DEFAULT_JUDGE_MODEL
    sys = _JUDGE_PROMPT_PATH.read_text(encoding="utf-8")
    user = (
        f"Subgoal: {subgoal.intent}\n"
        f"Post-condition to verify: {subgoal.verify}\n\n"
        f"AX tree (markdown):\n{tree_md}\n\n"
        "Return the JSON action."
    )
    reply = gateway.chat(
        model=model,
        messages=[
            {"role": "system", "content": sys},
            {"role": "user", "content": user},
        ],
        temperature=0.1,
        max_tokens=1000,
        response_format="json",
    )
    text = reply["content"].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


# ──────────────────────────────────────────────────────────────────────
# Layer dispatchers
# ──────────────────────────────────────────────────────────────────────

def _dispatch_2a_hotkeys(pid: int, window_id: int,
                         hotkey_seq: list[dict],
                         *,
                         plan: Optional[Plan] = None) -> list[TurnRecord]:
    """Layer 2a: deterministic press_key / hotkey / type_text / click_button
    sequence.

    Caller supplies a list of dicts shaped like:
       {"kind": "press_key", "key": "7"}
       {"kind": "hotkey", "keys": ["command", "c"]}
       {"kind": "type_text", "text": "hello"}
       {"kind": "click_button", "button_label": "Multiply"}

    For `click_button` we scan the AX tree once, find the AXButton whose
    label/id matches, and click it by element_index. The scan is reused
    across all click_button steps in the same sequence (Calculator's
    button layout doesn't reflow when you press a digit).

    No LLM in the action loop. The scan + label-match is pure
    deterministic Python; only Layer 2b would involve a judge.
    """
    out: list[TurnRecord] = []
    button_cache: dict[str, int] = {}

    def _resolve_button(label: str) -> int:
        """Find the element_index for a button by its label or id.

        macOS sometimes returns just the menu bar for the AX tree when
        the target app isn't strictly frontmost. We re-activate and
        re-scan up to 3 times.
        """
        if label in button_cache:
            return button_cache[label]
        import re as _re
        for attempt in range(3):
            state = driver.get_window_state(pid, window_id, mode="ax")
            md = state.get("tree_markdown", "")
            local_cache: dict[str, int] = {}
            for line in md.splitlines():
                m = _re.search(
                    r"\[(\d+)\]\s+AXButton\s+[\(\"]([^\)\"]+)[\)\"]",
                    line,
                )
                if not m:
                    m = _re.search(
                        r"\[(\d+)\]\s+AXButton.*\bid=([A-Za-z_]+)", line,
                    )
                if not m:
                    continue
                idx, button_label = int(m.group(1)), m.group(2)
                local_cache[button_label] = idx
            if local_cache:
                button_cache.update(local_cache)
                if label in button_cache:
                    return button_cache[label]
            # No AXButton found — likely the menu-bar-only tree case.
            # Re-activate the app and try again. plan is optional;
            # without it we just sleep and retry the scan.
            if plan is not None:
                try:
                    platform_.activate_app(plan.app.bundle_id)
                except Exception:
                    pass
            time.sleep(0.6)
        raise ValueError(
            f"no AXButton labelled {label!r} found in window scan "
            f"after 3 attempts"
        )

    for step in hotkey_seq:
        t0 = time.time()
        kind = step["kind"]
        try:
            if kind == "press_key":
                driver.press_key(pid, step["key"],
                                 modifiers=step.get("modifiers"))
            elif kind == "hotkey":
                driver.hotkey(pid, step["keys"])
            elif kind == "type_text":
                driver.type_text(pid, step["text"])
            elif kind == "click_button":
                idx = _resolve_button(step["button_label"])
                driver.click(pid, window_id, element_index=idx)
            else:
                raise ValueError(f"unknown 2a step kind: {kind}")
            err = None
        except Exception as e:
            err = str(e)
        out.append(TurnRecord(
            subgoal_id="2a",
            layer="2a",
            action=kind,
            detail=step,
            elapsed_ms=round((time.time() - t0) * 1000),
            error=err,
        ))
        if err:
            return out
        # Small inter-key delay so the OS event queue keeps up.
        time.sleep(0.08)
    return out


def _dispatch_2b_turn(pid: int, window_id: int,
                      subgoal: Subgoal) -> tuple[TurnRecord, dict]:
    """Layer 2b: one scan + one judge + one act + one verify scan."""
    t0 = time.time()
    state = driver.get_window_state(pid, window_id, mode="ax")
    pcpt = perception.interpret(state, pid=pid, window_id=window_id)
    if pcpt.is_empty:
        # Recovery classifier will see this as escalate-to-3.
        return TurnRecord(
            subgoal_id=subgoal.id, layer="2b", action="scan",
            detail={"element_count": 0},
            elapsed_ms=round((time.time() - t0) * 1000),
            error="element_count_zero",
        ), state
    verdict = _judge(subgoal, pcpt.markdown)
    if verdict.get("verdict") == "done":
        return TurnRecord(
            subgoal_id=subgoal.id, layer="2b", action="done",
            detail={"rationale": verdict.get("rationale", "")},
            elapsed_ms=round((time.time() - t0) * 1000),
        ), state
    if verdict.get("verdict") == "escalate":
        return TurnRecord(
            subgoal_id=subgoal.id, layer="2b", action="escalate",
            detail={"rationale": verdict.get("rationale", "")},
            elapsed_ms=round((time.time() - t0) * 1000),
            error="judge_escalate",
        ), state
    act = verdict.get("action") or {}
    kind = act.get("kind")
    try:
        if kind == "click":
            if "element_index" not in act:
                raise ValueError("click action missing element_index")
            driver.click(pid, window_id,
                         element_index=int(act["element_index"]))
        elif kind == "type_text":
            text_val = act.get("text") or act.get("value") or ""
            if not text_val:
                raise ValueError("type_text action missing text")
            driver.type_text(pid, text_val)
        elif kind == "press_key":
            if "key" not in act:
                raise ValueError("press_key action missing key")
            driver.press_key(pid, act["key"],
                             modifiers=act.get("modifiers"))
        elif kind == "hotkey":
            if "keys" not in act:
                raise ValueError("hotkey action missing keys")
            driver.hotkey(pid, act["keys"])
        else:
            raise ValueError(f"unknown action kind: {kind!r}")
    except (driver.DriverError, ValueError, KeyError) as e:
        code = getattr(e, "code", "judge_malformed")
        return TurnRecord(
            subgoal_id=subgoal.id, layer="2b", action=kind or "?",
            detail={"verdict": verdict, "error": str(e)},
            elapsed_ms=round((time.time() - t0) * 1000),
            error=code,
        ), state
    return TurnRecord(
        subgoal_id=subgoal.id, layer="2b", action=kind,
        detail={"verdict": verdict, "rationale": verdict.get("rationale", "")},
        elapsed_ms=round((time.time() - t0) * 1000),
    ), state


def _dispatch_3_turn(pid: int, window_id: int,
                     subgoal: Subgoal) -> TurnRecord:
    """Layer 3: capture + vision-judge + pixel click."""
    t0 = time.time()
    try:
        out = vision_layer.attempt(pid, window_id, subgoal.intent)
        err = None if out.get("verdict") == "click_xy" else (
            "no_target" if out.get("verdict") == "no_target" else None
        )
    except (driver.DriverError, gateway.GatewayError) as e:
        out = {"error": str(e)}
        err = getattr(e, "code", "vision_error")
    return TurnRecord(
        subgoal_id=subgoal.id, layer="3", action="vision",
        detail=out,
        elapsed_ms=round((time.time() - t0) * 1000),
        error=err,
    )


# ──────────────────────────────────────────────────────────────────────
# Per-subgoal cascade walker
# ──────────────────────────────────────────────────────────────────────

def run_subgoal(plan: Plan, pid: int, window_id: int,
                subgoal: Subgoal,
                *,
                hotkey_plan: Optional[Callable[[Subgoal],
                                               list[dict]]] = None,
                electron_plan: Optional[Callable[[Subgoal, int], list[dict]]] = None,
                max_turns: int = 8) -> tuple[bool, list[TurnRecord]]:
    """Walk one subgoal through the cascade until it succeeds or the
    cascade is exhausted.

    `hotkey_plan` is a per-task hook: if non-None, it converts a subgoal
    into a deterministic Layer 2a step list. Used by Task A (Calculator).

    `electron_plan` is the matching hook for the Electron `page` path:
    given a subgoal and the pid, it returns a list of page-tool actions
    (CSS selectors, JS evals, waits). Used by Task C (Cursor).
    """
    turns: list[TurnRecord] = []

    # ── Layer 2a opportunity ──
    if hotkey_plan is not None:
        steps = hotkey_plan(subgoal)
        if steps:
            sub_turns = _dispatch_2a_hotkeys(pid, window_id, steps,
                                             plan=plan)
            turns.extend(sub_turns)
            if all(t.error is None for t in sub_turns):
                return True, turns
            # Layer 2a failed; fall through to 2b.

    # ── Electron / CDP page path (Layer 2-electron) ──
    # The task knows whether it can drive the target via the `page` tool;
    # if it supplied an electron_plan hook, we always try the page path
    # first. The planner's `electron` flag is just advisory.
    if electron_plan is not None:
        page_steps = electron_plan(subgoal, pid)
        if page_steps:
            cdp_port = (
                plan.app.electron_debugging_port
                or getattr(plan.app, "cdp_port", None)
            )
            for step in page_steps:
                t0 = time.time()
                action = step["action"]
                try:
                    # CDP-direct path: cua-driver 0.5.3's page tool falls
                    # back to AppleScript when daemon-proxied, which
                    # times out on Chrome even after enabling JS-from-AE.
                    # For execute_javascript and navigate we speak CDP
                    # over websocket ourselves; works on Chrome, Brave,
                    # Edge, and any Electron app launched with a
                    # debugging port.
                    if action == "execute_javascript" and cdp_port:
                        value = cdp_client.eval_js(
                            int(cdp_port), step["javascript"],
                        )
                        resp = {"ok": True, "value": value}
                    elif action == "navigate" and cdp_port:
                        cdp_client.navigate(int(cdp_port), step["url"])
                        resp = {"ok": True}
                    else:
                        # Fall through to cua-driver's page tool — works
                        # for click_element (which animates the cursor)
                        # and get_text.
                        resp = driver.page(
                            pid, action, window_id=window_id,
                            **{k: v for k, v in step.items()
                               if k != "action"},
                        )
                    err = None
                    detail = {"step": step, "response": resp}
                except (driver.DriverError, cdp_client.CDPError) as e:
                    err = getattr(e, "code", "page_error")
                    detail = {"step": step, "error": str(e)}
                turns.append(TurnRecord(
                    subgoal_id=subgoal.id, layer="2-electron",
                    action=step["action"], detail=detail,
                    elapsed_ms=round((time.time() - t0) * 1000),
                    error=err,
                ))
                if err:
                    break
            if all(t.error is None for t in turns[-len(page_steps):]):
                return True, turns

    # ── Layer 2b cascade ──
    current_layer = "2b"
    retries = 0
    for _turn in range(max_turns):
        if current_layer == "2b":
            try:
                trec, _state = _dispatch_2b_turn(pid, window_id, subgoal)
            except (driver.DriverError, gateway.GatewayError) as e:
                trec = TurnRecord(
                    subgoal_id=subgoal.id, layer="2b", action="error",
                    detail={"error": str(e)},
                    elapsed_ms=0, error=getattr(e, "code", "unknown"),
                )
            turns.append(trec)
            if trec.error is None and trec.action == "done":
                return True, turns
            if trec.error is None:
                # An action fired; loop continues to next turn.
                retries = 0
                continue
            judge_verdict = (
                "escalate" if trec.error == "judge_escalate" else None
            )
            dec = recovery.classify(
                current_layer=current_layer,
                error_code=trec.error,
                judge_verdict=judge_verdict,
                retry_count=retries,
            )
            if dec.decision == "retry":
                retries += 1
                time.sleep(0.4)
                continue
            if dec.decision == "escalate":
                current_layer = dec.new_layer or "3"
                retries = 0
                continue
            return False, turns

        if current_layer == "3":
            trec = _dispatch_3_turn(pid, window_id, subgoal)
            turns.append(trec)
            if trec.error is None:
                # Vision fired a click. Re-verify on next loop via 2b.
                current_layer = "2b"
                continue
            dec = recovery.classify(
                current_layer=current_layer,
                error_code=trec.error,
                retry_count=retries,
            )
            if dec.decision == "retry":
                retries += 1
                time.sleep(0.4)
                continue
            return False, turns

    return False, turns
