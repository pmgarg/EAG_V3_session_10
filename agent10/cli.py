"""
cli.py — single entry point to run the three target tasks.

Usage:

    python -m agent10.cli a                # Calculator (Layer 2a)
    python -m agent10.cli b                # Notes (Layer 2b + AppleScript verify)
    python -m agent10.cli c                # Chrome (CDP + vision verify)
    python -m agent10.cli all              # all three back-to-back
    python -m agent10.cli a "31*42+7"      # Task A with custom expression

The trajectory directory for each run is printed at the end so you can
hand it off to `cua-driver replay_trajectory` for a deterministic
re-execution.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from agent10.tasks import (
    task_a_calculator,
    task_b_notes,
    task_c_cursor,
)


def _print_result(label: str, res) -> None:
    print()
    print("=" * 70)
    print(f" RESULT — {label}")
    print("=" * 70)
    print(f"  success       : {res.success}")
    print(f"  wall_clock    : {res.wall_clock_s:.1f}s")
    print(f"  pid           : {res.pid}")
    print(f"  window_id     : {res.window_id}")
    print(f"  trajectory    : {res.trajectory_dir}")
    if res.extracted:
        print(f"  extracted     : {json.dumps(res.extracted, default=str)[:400]}")
    if res.error:
        print(f"  error         : {res.error[:200]}")
    if res.plan:
        print(f"  plan.app      : {res.plan.app.bundle_id}")
        if res.plan.app.electron:
            print(f"  electron port : {res.plan.app.electron_debugging_port}")
    print()
    print("  per-turn cascade trace:")
    for t in res.turns:
        print(f"    {t.subgoal_id:6} layer={t.layer:13} "
              f"action={t.action:24} elapsed={t.elapsed_ms:5}ms  "
              f"err={str(t.error or '')[:50]}")


def _run_a(args: list[str]):
    expr = args[0] if args else "(47*53)+(101*7)"
    return task_a_calculator.run(expr)


def _run_b(args: list[str]):
    return task_b_notes.run()


def _run_c(args: list[str]):
    return task_c_cursor.run()


_TASKS = {
    "a": ("Calculator (Layer 2a — hotkeys + clipboard, zero LLM/vision)",
          _run_a),
    "b": ("Notes (Layer 2b — AX tree + Cmd+N + AppleScript verify)",
          _run_b),
    "c": ("Chrome (Layer 2-electron CDP + Layer 3 vision verify)",
          _run_c),
}


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("Usage: python -m agent10.cli {a|b|c|all} [task-args]")
        print()
        for key, (desc, _) in _TASKS.items():
            print(f"  {key}    {desc}")
        return 1
    target = args[0].lower()
    rest = args[1:]
    if target == "all":
        results = []
        for key in ("a", "b", "c"):
            desc, runner = _TASKS[key]
            print(f"\n\n[{key}] starting: {desc}")
            res = runner([])
            _print_result(f"task {key.upper()} — {desc}", res)
            results.append((key, res.success))
            time.sleep(2)
        print("\n\n===== SCOREBOARD =====")
        for k, ok in results:
            print(f"  task {k.upper()}: {'PASS' if ok else 'FAIL'}")
        return 0 if all(ok for _, ok in results) else 1
    if target not in _TASKS:
        print(f"unknown task {target!r}; choose one of {list(_TASKS)} or 'all'")
        return 1
    desc, runner = _TASKS[target]
    print(f"\n[{target}] {desc}")
    res = runner(rest)
    _print_result(f"task {target.upper()}", res)
    return 0 if res.success else 1


if __name__ == "__main__":
    sys.exit(main())
