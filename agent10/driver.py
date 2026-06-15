"""
driver.py — thin shell over `cua-driver call`.

cua-driver has three execution surfaces (in-process / daemon / MCP, see
CUA_DRIVER_GUIDE.md §3). We use the **daemon** surface: every call goes
through `cua-driver call <tool> <json>`, which proxies to the running
`cua-driver serve` daemon over a Unix socket. The daemon holds the
per-window element-index cache that the scan/act/verify loop depends on.

Why a wrapper at all (instead of subprocess.run inline everywhere):

  1. One place to put `ensure_daemon()` — start the daemon if it's not
     up yet. Idempotent.
  2. One place for the call-with-timeout + JSON-parse + error-classify
     pipeline. Layers above just see `result_dict` or a raised
     DriverError.
  3. One place for the "agent cursor visible" overlay that makes the
     YouTube demo readable. Toggled via env var.
  4. One place to swap the binary path if the user's cua-driver lives
     somewhere weird.

This module is intentionally OS-agnostic. The OS-specific bits
(AppleScript activate on macOS, QT_ACCESSIBILITY=1 on Linux, etc.)
live in `platform_/`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional


class DriverError(RuntimeError):
    """Raised when cua-driver returns a non-zero exit code or invalid JSON.

    The `code` attribute carries a short string the recovery layer keys on:
      - "not_installed"          : cua-driver binary not on PATH
      - "daemon_down"            : daemon not running and we couldn't start it
      - "permission_blocked"     : TCC / portal / UAC denied
      - "element_count_zero"     : scan returned empty AX tree
      - "cache_miss"             : element_index not in cache (re-scan needed)
      - "tool_error"             : tool ran but reported an error
      - "timeout"                : subprocess timed out
      - "json_parse"             : driver returned non-JSON
      - "unknown"                : everything else
    """

    def __init__(self, message: str, *, code: str = "unknown",
                 stderr: str = "", returncode: int = -1):
        super().__init__(message)
        self.code = code
        self.stderr = stderr
        self.returncode = returncode


def _find_binary() -> str:
    """Return the cua-driver binary path or raise DriverError(not_installed)."""
    # Try PATH first.
    p = shutil.which("cua-driver")
    if p:
        return p
    # User's local install location (the install script writes here).
    local = Path.home() / ".local" / "bin" / "cua-driver"
    if local.exists():
        return str(local)
    raise DriverError(
        "cua-driver binary not found. Install with:\n"
        "  /bin/bash -c \"$(curl -fsSL "
        "https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/"
        "scripts/install.sh)\"",
        code="not_installed",
    )


_BINARY: Optional[str] = None


def binary() -> str:
    """Memoised binary path."""
    global _BINARY
    if _BINARY is None:
        _BINARY = _find_binary()
    return _BINARY


def is_daemon_up() -> bool:
    """Cheap probe: `cua-driver status` returns 0 iff daemon is alive."""
    try:
        r = subprocess.run(
            [binary(), "status"],
            capture_output=True, text=True, timeout=4,
        )
        return r.returncode == 0
    except (DriverError, subprocess.TimeoutExpired, FileNotFoundError):
        return False


def ensure_daemon() -> None:
    """Start `cua-driver serve` if it's not already running.

    On macOS we launch via `open -n -g -a CuaDriver --args serve` so the
    TCC identity is the CuaDriver bundle (`com.trycua.driver`), not the
    terminal that spawned us. Launching via plain `subprocess.Popen([
    binary, "serve"])` would attach TCC to the parent process and the
    driver would silently report no permissions later.
    """
    if is_daemon_up():
        return
    import sys
    if sys.platform == "darwin":
        subprocess.Popen(
            ["open", "-n", "-g", "-a", "CuaDriver", "--args", "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    else:
        # Linux / Windows: no TCC identity dance needed.
        subprocess.Popen(
            [binary(), "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    # Daemon comes up in 1-3s on macOS.
    for _ in range(20):
        time.sleep(0.25)
        if is_daemon_up():
            return
    raise DriverError(
        "Started `cua-driver serve` but the daemon did not become "
        "reachable within 5 seconds. Check `cua-driver status` manually.",
        code="daemon_down",
    )


def call(tool: str, args: Optional[dict[str, Any]] = None,
         *, timeout: float = 30.0) -> dict[str, Any]:
    """Invoke one cua-driver tool through the daemon.

    Equivalent to `cua-driver call <tool> '<json>'` but with structured
    error handling and a Python-level timeout. The daemon is started
    automatically if it's not running.

    Raises DriverError with a `code` field the recovery layer keys on.
    """
    ensure_daemon()
    body = json.dumps(args or {})
    try:
        r = subprocess.run(
            [binary(), "call", tool, body],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise DriverError(
            f"cua-driver `{tool}` timed out after {timeout}s. "
            "The daemon may be unresponsive — try `cua-driver shutdown`.",
            code="timeout",
        ) from e

    stdout = r.stdout or ""
    stderr = r.stderr or ""

    if r.returncode != 0:
        # Classify common errors so the recovery layer doesn't have to
        # parse stderr strings itself.
        lower = (stderr + stdout).lower()
        code = "tool_error"
        if "not found in cache" in lower or "element index" in lower and "not found" in lower:
            code = "cache_miss"
        elif "permission" in lower or "tcc" in lower or "access denied" in lower:
            code = "permission_blocked"
        elif "daemon" in lower and ("not running" in lower or "no socket" in lower):
            code = "daemon_down"
        raise DriverError(
            f"cua-driver `{tool}` failed (rc={r.returncode}): {stderr.strip()[:500]}",
            code=code, stderr=stderr, returncode=r.returncode,
        )

    if not stdout.strip():
        # Some tools (like start_recording) return empty stdout on success.
        return {}
    text = stdout.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Several tools (press_key, hotkey, type_text, click, ...) return
        # a human-readable status line like "✅ Pressed escape on pid 123."
        # or "Pressed command+c on pid 123." on success — JSON only on
        # error. We got here with a 0 exit code, so treat any of the
        # known success-line shapes as ok.
        lower = text.lower()
        ok_prefixes = (
            "✅", "✓", "ok",
            "pressed ", "performed ", "inserted ", "clicked ",
            "released ", "moved ", "scrolled ", "dragged ",
        )
        if text.startswith(ok_prefixes) or any(
            lower.startswith(p) for p in ok_prefixes
        ):
            return {"ok": True, "message": text}
        # Otherwise this really is unexpected output.
        raise DriverError(
            f"cua-driver `{tool}` returned non-JSON output: {text[:500]}",
            code="json_parse",
        )


# ──────────────────────────────────────────────────────────────────────────
# Convenience helpers used by the scan-act-verify loop.
# ──────────────────────────────────────────────────────────────────────────

def get_window_state(pid: int, window_id: int, *,
                     mode: str = "ax",
                     query: Optional[str] = None) -> dict[str, Any]:
    """Read the AX tree for one window. `mode` is one of ax/som/vision.

    Invariant A from CUA_DRIVER_GUIDE.md §5: this call BUILDS the
    element-index cache. Layers above MUST call this before any
    element-indexed action.

    `mode="ax"` is the cheap path — no Screen Recording grant needed,
    just Accessibility. Use `som` when the judge LLM also wants to see
    the screenshot, `vision` when AX is empty and you're escalating to
    Layer 3.
    """
    args: dict[str, Any] = {"pid": pid, "window_id": window_id,
                            "capture_mode": mode}
    if query:
        args["query"] = query
    return call("get_window_state", args, timeout=45.0)


def click(pid: int, window_id: int, *, element_index: int) -> dict[str, Any]:
    """Element-indexed click. Cache must be primed by a prior scan."""
    return call("click", {"pid": pid, "window_id": window_id,
                          "element_index": element_index})


def click_xy(pid: int, x: int, y: int) -> dict[str, Any]:
    """Pixel-coordinate click. Used by Layer 3 vision."""
    return call("click", {"pid": pid, "x": x, "y": y})


def type_text(pid: int, text: str) -> dict[str, Any]:
    """Insert text via AXSetAttribute. More reliable than press_key for
    long strings (see CUA_DRIVER_GUIDE.md §4 Action table)."""
    return call("type_text", {"pid": pid, "text": text})


def press_key(pid: int, key: str, *,
              modifiers: Optional[list[str]] = None) -> dict[str, Any]:
    """Single key press. Used by Layer 2a deterministic sequences."""
    args: dict[str, Any] = {"pid": pid, "key": key}
    if modifiers:
        args["modifiers"] = modifiers
    return call("press_key", args)


def hotkey(pid: int, keys: list[str]) -> dict[str, Any]:
    """Simultaneous key combination, e.g. ['command', 'shift', 'n']."""
    return call("hotkey", {"pid": pid, "keys": keys})


def launch_app(bundle_id: str, *,
               electron_debugging_port: Optional[int] = None,
               webkit_inspector_port: Optional[int] = None,
               env: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """LaunchServices on macOS. Does NOT bring the app to the foreground —
    see CUA_DRIVER_GUIDE.md §6.1 and platform_/_macos.py::activate_app()."""
    args: dict[str, Any] = {"bundle_id": bundle_id}
    if electron_debugging_port is not None:
        args["electron_debugging_port"] = electron_debugging_port
    if webkit_inspector_port is not None:
        args["webkit_inspector_port"] = webkit_inspector_port
    if env:
        args["env"] = env
    return call("launch_app", args, timeout=20.0)


def list_windows(pid: Optional[int] = None) -> list[dict[str, Any]]:
    """All top-level windows. Filter by pid if given."""
    out = call("list_windows", {})
    wins = out.get("windows", [])
    if pid is not None:
        wins = [w for w in wins if w.get("pid") == pid]
    return wins


def page(pid: int, action: str, *,
         window_id: Optional[int] = None, **kw: Any) -> dict[str, Any]:
    """Electron / Tauri / Chrome / Brave / Edge CDP path. Requires the
    app to have been launched with `electron_debugging_port` (Electron)
    or `--remote-debugging-port` (Chrome). See CUA_DRIVER_GUIDE.md §7.2.

    cua-driver's page tool requires both pid AND window_id — the latter
    selects which browser window to bind the CDP session to when an app
    has multiple windows.
    """
    args: dict[str, Any] = {"pid": pid, "action": action, **kw}
    if window_id is not None:
        args["window_id"] = window_id
    return call("page", args, timeout=30.0)


def start_recording(output_dir: str) -> dict[str, Any]:
    """Start trajectory recording. Every subsequent tool call writes to
    a turn-numbered subdirectory of output_dir."""
    return call("start_recording", {"output_dir": output_dir})


def stop_recording() -> dict[str, Any]:
    return call("stop_recording", {})


def check_permissions() -> dict[str, Any]:
    """TCC status snapshot. {accessibility: bool, screen_recording: bool, ...}"""
    return call("check_permissions", {})
