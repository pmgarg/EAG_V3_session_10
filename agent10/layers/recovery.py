"""
layers/recovery.py — classify failures and pick the next path.

Three input cases that look the same on the surface but need very
different responses:

  - **transient** (network blip, slow daemon spawn, brief flicker of
    cache_miss while UI reflows): same step, retry once with a short
    backoff. If it succeeds, the run continues silently.
  - **escalate** (AX path keeps failing, judge keeps saying "no target",
    perception keeps coming back empty): move to the next layer in the
    cascade. Layer 2a → 2b → 3.
  - **blocked** (permission denied, daemon dead, app crashed, user
    closed the window): cannot continue. Surface to the user.

The classifier is keyword-based, mirroring Session 8's recovery.py. It
keys on the `code` field of DriverError and GatewayError plus the
verdict the Judge or Vision skill returned. Unit-test friendly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional


Decision = Literal["retry", "escalate", "blocked"]


@dataclass
class RecoveryDecision:
    decision: Decision
    rationale: str
    new_layer: Optional[str] = None     # set when decision == "escalate"


# Driver error codes that mean "transient — try the same thing again".
_TRANSIENT_CODES = {"timeout", "json_parse"}

# Driver error codes that mean "the user has to do something".
_BLOCKED_CODES = {"permission_blocked", "not_installed", "daemon_down"}

# Driver error codes that mean "the cascade needs to move up a layer".
_ESCALATE_CODES = {"element_count_zero", "cache_miss", "tool_error"}


_CASCADE_ORDER = ["2a", "2b", "3"]


def _next_layer(current: str) -> Optional[str]:
    """Return the next layer in the cascade, or None if we're at the top."""
    try:
        i = _CASCADE_ORDER.index(current)
    except ValueError:
        return None
    if i + 1 >= len(_CASCADE_ORDER):
        return None
    return _CASCADE_ORDER[i + 1]


def classify(*,
             current_layer: str,
             error_code: Optional[str] = None,
             judge_verdict: Optional[str] = None,
             retry_count: int = 0,
             max_retries: int = 1) -> RecoveryDecision:
    """Pick the next move given the layer that just failed and how.

    `current_layer` is one of "2a", "2b", "3".
    `error_code` is a DriverError.code or GatewayError-like string.
    `judge_verdict` is the Judge's verdict string when it said "escalate".

    Examples:
      - Layer 2b, error_code="cache_miss", retry=0  →  retry (UI reflowed)
      - Layer 2b, error_code="cache_miss", retry=1  →  escalate to 3
      - Layer 2b, judge_verdict="escalate"          →  escalate to 3
      - Layer 2a, error_code=None, judge_verdict=None, hotkeys ran clean
        →  caller doesn't invoke us at all.
      - Any layer, error_code="permission_blocked" →  blocked.
    """
    if error_code in _BLOCKED_CODES:
        return RecoveryDecision(
            decision="blocked",
            rationale=f"{error_code}: cannot proceed without user action",
        )

    if error_code in _TRANSIENT_CODES and retry_count < max_retries:
        return RecoveryDecision(
            decision="retry",
            rationale=f"transient error ({error_code}); retrying",
        )

    # Judge said "escalate" or driver said "tree empty".
    if judge_verdict == "escalate" or error_code in _ESCALATE_CODES:
        nxt = _next_layer(current_layer)
        if nxt is None:
            return RecoveryDecision(
                decision="blocked",
                rationale="cascade exhausted; no higher layer available",
            )
        return RecoveryDecision(
            decision="escalate",
            rationale=f"layer {current_layer} could not advance; "
                      f"escalating to layer {nxt}",
            new_layer=nxt,
        )

    # cache_miss with no retry budget left, OR an unfamiliar error code.
    if retry_count >= max_retries:
        nxt = _next_layer(current_layer)
        if nxt is None:
            return RecoveryDecision(
                decision="blocked",
                rationale="retry budget exhausted; cascade exhausted",
            )
        return RecoveryDecision(
            decision="escalate",
            rationale=f"retry budget exhausted on layer {current_layer}; "
                      f"escalating to layer {nxt}",
            new_layer=nxt,
        )

    return RecoveryDecision(
        decision="retry",
        rationale="unrecognised error; one retry",
    )
