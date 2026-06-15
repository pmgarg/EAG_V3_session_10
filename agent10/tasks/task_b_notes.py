"""
Task B — Notes app via AX tree + cheap text LLM (Layer 2b).

Goal: Open the macOS Notes app, create a new note, type three short
bullet points about today's session, then verify by reading the note's
content back from the AX tree.

This task exists to demonstrate the Layer 2b "workhorse" path the
session writeup calls out: get_window_state → cheap text LLM picks an
element_index → dispatch → re-scan → verify. No deterministic hotkey
shortcut beyond the universal Cmd+N to create a new note.

Why Notes specifically:
  - Native AppKit app — exposes a full AX tree.
  - Has stable hotkeys (Cmd+N for new note, content area is an
    AXTextArea).
  - Side-effect is visible (the note persists, easy to verify).
  - Not Electron, so the AX path is the right path — perfect Layer 2b
    demonstration.

We mix a tiny bit of Layer 2a (Cmd+N to create the new note) with
Layer 2b for the typing — using type_text directly into the focused
AXTextArea once Notes is open.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from agent10 import computer_skill, driver
from agent10.layers.planner import Subgoal


# The content we'll write into the note.
_BULLETS = [
    "Session 10 demo: Computer-Use agent",
    "Cascade: Layer 1 → 2a → 2b → 3",
    "demo recorded via cua-driver start_recording",
]
_NOTE_TITLE = f"Session 10 demo {dt.date.today().isoformat()}"


def _hotkey_plan_for(subgoal: Subgoal) -> list[dict]:
    """Layer 2a hook: Cmd+N opens a new note in Notes deterministically.

    We handle TWO subgoal shapes in one hook:
      (a) "create a new note" → cmd+N + type the title and bullets.
      (b) "type/write the bullets / content / title" → if Notes is
          already showing a fresh note (we got there via (a) or via a
          previous turn), type the body without cmd+N.

    The second case prevents the planner's habit of emitting two
    overlapping subgoals (create-note, then type-content) from
    producing two new notes.
    """
    intent_lc = (subgoal.intent or "").lower()
    body = _NOTE_TITLE + "\n" + "\n".join(f"- {b}" for b in _BULLETS)
    # Case (a)
    if ("new note" in intent_lc or
            ("create" in intent_lc and "note" in intent_lc) or
            "open" in intent_lc and "note" in intent_lc):
        return [
            {"kind": "hotkey", "keys": ["command", "n"]},
            {"kind": "type_text", "text": body},
        ]
    # Case (b): typing content into an already-open note. Don't re-fire
    # cmd+N — that creates yet another empty note.
    if any(kw in intent_lc for kw in (
            "type", "write", "enter", "add", "compose")):
        return [
            {"kind": "type_text", "text": body},
        ]
    return []


def _extract_note(subgoal: Subgoal, state: dict) -> dict[str, Any]:
    """Layer 1 verify: read the note's body via AppleScript and check
    the title + bullets are present.

    macOS 26 Notes renders the content area through a non-AX view, so
    `get_window_state` can only see the title in menu items and the
    sidebar. To verify the body we shell out to AppleScript's `body of
    note` accessor — that path is 100% reliable on Notes.

    We also keep a fallback path that greps the AX tree markdown, in
    case AppleScript is unavailable (e.g. running under a different
    user account).
    """
    import subprocess
    md = state.get("tree_markdown") or ""
    md_lc = md.lower()
    found_title_ax = _NOTE_TITLE.lower() in md_lc
    bullets_in_ax = [b for b in _BULLETS if b.lower() in md_lc]

    # Primary: AppleScript reads note body directly.
    body_text = ""
    body_source = "none"
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "Notes"\n'
             '  set ns to notes whose name contains "Session 10"\n'
             '  if (count of ns) = 0 then return ""\n'
             '  return body of (item 1 of ns)\n'
             'end tell'],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            body_text = r.stdout
            body_source = "applescript"
    except Exception:
        pass

    body_lc = body_text.lower()
    bullets_in_body = [b for b in _BULLETS if b.lower() in body_lc]
    found_title_body = _NOTE_TITLE.lower() in body_lc

    verify_passed = bool(
        (found_title_ax or found_title_body)
        and (len(bullets_in_body) >= 2 or len(bullets_in_ax) >= 2)
    )

    return {
        "title_in_tree": found_title_ax,
        "title_in_body": found_title_body,
        "bullets_in_ax": len(bullets_in_ax),
        "bullets_in_body": len(bullets_in_body),
        "bullets_total": len(_BULLETS),
        "body_source": body_source,
        "verify_passed": verify_passed,
        "title": _NOTE_TITLE,
    }


HOOKS = computer_skill.TaskHooks(
    hotkey_plan=_hotkey_plan_for,
    extract=_extract_note,
)


def run() -> computer_skill.RunResult:
    goal = (
        "Open the macOS Notes app, create a new note, write three short "
        "bullet points about a Session 10 demo run, then verify the note "
        "contains the title and at least two bullets by reading it back."
    )
    return computer_skill.run(
        goal,
        label="taskB-notes",
        task_hooks=HOOKS,
        need_screen_recording=False,
    )
