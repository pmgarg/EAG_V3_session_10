"""
layers/perception.py — pre-filter the AX tree before it reaches the
Layer 2b Judge LLM.

The raw tree_markdown from get_window_state can be tens of kilobytes
for a real app (Cursor's tree on a typical day is ~40 KB). Sending the
whole thing to the Judge wastes tokens and dilutes the signal. This
module trims the markdown so the Judge sees only what matters for the
current subgoal.

Two filters, applied in order:

  1. Server-side `query` filter — passed to get_window_state. The
     driver does the regex inside its Rust code, so the markdown returned
     is already trimmed. Cheap, but inflexible: the query is a single
     substring and the indices are still preserved across the whole tree
     (CUA_DRIVER_GUIDE.md §6.4).
  2. Client-side line filter — if the markdown is still too big, we drop
     lines that don't contain an `[element_index ...]` tag. The Judge
     can only act on indexed elements anyway.

Empty-tree detection lives here too, so the Executor's error layer can
distinguish "scan returned nothing" from "scan returned plenty but
nothing matched the subgoal".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# Max markdown size in characters that we feel comfortable sending to
# gemma4:12b. Above this we start trimming.
MAX_JUDGE_CHARS = 12000


@dataclass
class PerceptionResult:
    pid: int
    window_id: int
    element_count: int
    markdown: str
    trimmed: bool
    is_empty: bool


def shortlist(state: dict, *, max_chars: int = MAX_JUDGE_CHARS) -> str:
    """Trim the markdown so the Judge LLM sees only indexed lines if the
    full tree is too large to send efficiently."""
    md: str = state.get("tree_markdown", "") or ""
    if len(md) <= max_chars:
        return md
    # Drop non-indexed lines; they're decorative.
    keep: list[str] = []
    chars = 0
    for line in md.splitlines():
        if "[element_index" in line or line.strip().startswith("#"):
            keep.append(line)
            chars += len(line) + 1
            if chars > max_chars:
                keep.append("... (tree truncated; re-run with a more "
                            "specific query if you need more) ...")
                break
    return "\n".join(keep)


def interpret(state: dict, *,
              pid: int, window_id: int,
              max_chars: int = MAX_JUDGE_CHARS) -> PerceptionResult:
    """Wrap a raw get_window_state response with derived flags the
    Executor and Recovery layers key on."""
    md = state.get("tree_markdown", "") or ""
    element_count = int(state.get("element_count", 0))
    trimmed = len(md) > max_chars
    return PerceptionResult(
        pid=pid,
        window_id=window_id,
        element_count=element_count,
        markdown=shortlist(state, max_chars=max_chars) if trimmed else md,
        trimmed=trimmed,
        is_empty=element_count == 0,
    )
