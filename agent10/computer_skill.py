"""
computer_skill.py — the "computer" skill the assignment asks for.

This is what would drop into the Session 9 catalogue as one yaml entry
plus this Python module. The yaml is in agent_config.yaml; the dispatch
branch in flow.py (Session 8) would add a single

    if skill.name == "computer":
        return computer_skill.run(goal=node.metadata["goal"], **opts)

…and everything below is what runs.

Public API:

    run(goal: str, *, record: bool = True, task_hooks: TaskHooks = None) -> RunResult

`task_hooks` is the per-task plug-in point. For the assignment we have
three concrete tasks (A=Calculator, B=Notes, C=Cursor) each with a tiny
hook that supplies the Layer 2a or Layer 2-electron step list when the
Executor asks. Without a hook the skill runs pure Layer 2b + 3.
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from agent10 import driver, gateway, permissions, platform_
from agent10.layers import planner as planner_mod
from agent10.layers import executor


@dataclass
class TaskHooks:
    """Per-task plug-in. All hooks are optional.

    `hotkey_plan(subgoal) -> list[dict]`
        Return a deterministic Layer 2a step list for this subgoal, or
        an empty list to fall through to Layer 2b. Used by Task A.

    `electron_plan(subgoal, pid) -> list[dict]`
        Return a list of `page` tool actions for this subgoal. Each
        action is a dict like `{"action": "click", "selector": "..."}`.
        Used by Task C.

    `extract(subgoal, state) -> dict`
        After a subgoal is marked done, optionally extract structured
        data from the post-state. Used by Task A to read Calculator's
        display value.

    `amend_plan(plan)`
        Optional hook that fires AFTER the planner produces its plan
        and BEFORE launch. Lets the task force fields the planner
        sometimes omits — Task C uses this to force the CDP port.
    """
    hotkey_plan: Optional[Callable[[Any], list[dict]]] = None
    electron_plan: Optional[Callable[[Any, int], list[dict]]] = None
    extract: Optional[Callable[[Any, Any], dict]] = None
    amend_plan: Optional[Callable[[Any], None]] = None


@dataclass
class RunResult:
    success: bool
    goal: str
    plan: Optional[planner_mod.Plan] = None
    pid: Optional[int] = None
    window_id: Optional[int] = None
    trajectory_dir: Optional[str] = None
    turns: list[executor.TurnRecord] = field(default_factory=list)
    extracted: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    wall_clock_s: float = 0.0


def _trajectory_dir(label: str) -> Path:
    """Where this run's trajectory + per-turn logs live."""
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    here = Path(__file__).resolve().parent.parent
    out = here / "trajectories" / f"{stamp}-{label}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def run(goal: str, *,
        label: str = "run",
        task_hooks: Optional[TaskHooks] = None,
        record: bool = True,
        need_screen_recording: bool = False) -> RunResult:
    """End-to-end: permissions → plan → launch → walk subgoals.

    The label is used for the trajectory directory name. The
    `need_screen_recording` flag tells the permission probe whether to
    block on missing Screen Recording grant; set True only for tasks
    that include vision steps.
    """
    t0 = time.time()
    result = RunResult(success=False, goal=goal)

    # ── 0. permissions ─────────────────────────────────────────────────
    try:
        permissions.ensure_permissions(
            need_screen_recording=need_screen_recording,
            interactive=True,
        )
    except permissions.PermissionDenied as e:
        result.error = str(e)
        result.wall_clock_s = time.time() - t0
        return result

    # ── 1. plan ────────────────────────────────────────────────────────
    try:
        plan = planner_mod.plan(goal)
    except (gateway.GatewayError, ValueError) as e:
        result.error = f"planner failed: {e}"
        result.wall_clock_s = time.time() - t0
        return result
    # Allow the task to amend the plan before launch (e.g. force a
    # specific CDP port the prelaunch set up).
    if task_hooks and getattr(task_hooks, "amend_plan", None):
        task_hooks.amend_plan(plan)
    result.plan = plan

    # ── 2. launch + activate ───────────────────────────────────────────
    try:
        pid, wid = executor.launch_and_activate(plan)
    except driver.DriverError as e:
        result.error = f"launch failed: {e}"
        result.wall_clock_s = time.time() - t0
        return result
    result.pid = pid
    result.window_id = wid

    # ── 3. trajectory recording ────────────────────────────────────────
    trajectory_dir: Optional[Path] = None
    if record:
        trajectory_dir = _trajectory_dir(label)
        try:
            driver.start_recording(str(trajectory_dir))
            result.trajectory_dir = str(trajectory_dir)
        except driver.DriverError as e:
            # Recording is best-effort; don't fail the run for it.
            print(f"[warning] start_recording failed: {e}")

    # ── 4. walk subgoals ───────────────────────────────────────────────
    hooks = task_hooks or TaskHooks()
    all_turns: list[executor.TurnRecord] = []
    success = True

    def _is_launch_subgoal(s) -> bool:
        intent = (s.intent or "").lower()
        return "launch" in intent and (
            "foreground" in intent or "open" in intent or "start" in intent
        )

    def _is_read_subgoal(s) -> bool:
        """Layer 1 extract path: the subgoal just asks to read or verify
        existing state, no action to dispatch. We satisfy this via the
        hook's `extract` function and skip the action cascade entirely."""
        intent = (s.intent or "").lower()
        # Any subgoal whose primary verb is "read" / "verify" / "check"
        # over EXISTING state is a Layer 1 extract.
        read_verbs = (
            "read", "verify", "check", "confirm",
            "tell me", "report", "extract the", "capture the",
        )
        return any(intent.startswith(v) or f" {v} " in f" {intent} "
                   for v in read_verbs)

    try:
        for sub in plan.subgoals:
            if _is_launch_subgoal(sub):
                # Already done above. Record a synthetic turn.
                all_turns.append(executor.TurnRecord(
                    subgoal_id=sub.id, layer="-", action="launch",
                    detail={"pid": pid, "window_id": wid},
                ))
                continue
            if _is_read_subgoal(sub) and hooks.extract:
                # Layer 1 extract: read the AX tree once and pull the
                # value via the per-task hook.
                t0 = time.time()
                try:
                    state = driver.get_window_state(pid, wid, mode="ax")
                    # Inject the pid + window_id so hooks that need
                    # clipboard/pbpaste fallback can find them.
                    state["pid"] = pid
                    state["window_id"] = wid
                    extra = hooks.extract(sub, state)
                except driver.DriverError as e:
                    extra = {}
                    result.error = f"read subgoal failed: {e}"
                all_turns.append(executor.TurnRecord(
                    subgoal_id=sub.id, layer="1", action="extract",
                    detail=extra or {"empty": True},
                    elapsed_ms=round((time.time() - t0) * 1000),
                ))
                if extra:
                    result.extracted.update(extra)
                continue
            ok, turns = executor.run_subgoal(
                plan, pid, wid, sub,
                hotkey_plan=hooks.hotkey_plan,
                electron_plan=hooks.electron_plan,
            )
            all_turns.extend(turns)
            if not ok:
                success = False
                result.error = (
                    f"subgoal {sub.id!r} ({sub.intent[:60]}...) failed "
                    "after cascade exhaustion"
                )
                break
            if hooks.extract:
                try:
                    state = driver.get_window_state(pid, wid, mode="ax")
                    extra = hooks.extract(sub, state)
                    if extra:
                        result.extracted.update(extra)
                except driver.DriverError:
                    pass
    finally:
        if record and trajectory_dir is not None:
            try:
                driver.stop_recording()
            except driver.DriverError:
                pass

    result.turns = all_turns
    result.success = success
    result.wall_clock_s = time.time() - t0
    return result
