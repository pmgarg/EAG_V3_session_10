"""
platform_/_macos.py — macOS-specific glue.

Two things matter here:

  1. **activate_app**: cua-driver's launch_app uses LaunchServices and
     does NOT steal focus from the user's foreground app. The response
     field `self_activation_suppressed: true` confirms it. A backgrounded
     app has no AXWindow children in its tree yet, so the first
     get_window_state returns the system menu bar and zero app buttons.
     We fix this with `osascript -e 'tell application "X" to activate'`,
     then a short sleep. CUA_DRIVER_GUIDE.md §6.1 documents this trap.

  2. **open_permissions_settings**: the TCC pref panes have
     `x-apple.systempreferences:` URLs that open directly to the right
     row. Useful for the permission-prompt flow at startup.
"""
from __future__ import annotations

import subprocess
import time
from typing import Optional

NAME = "macOS"


def _bundle_id_to_app_name(bundle_id: str) -> Optional[str]:
    """Look up the user-facing app name for a bundle id.

    Tries three sources in order:
      1. `mdfind` against Spotlight's metadata store. Works if the app's
         been indexed; tends to miss case-variant bundle ids the user
         typed (e.g. com.apple.notes vs com.apple.Notes).
      2. `osascript "id of app"` — AppleScript's own bundle-id-to-name
         resolver. Robust for case variants.
      3. The bundle id's last component, title-cased.
    """
    # 1. Spotlight.
    try:
        r = subprocess.run(
            ["mdfind", f"kMDItemCFBundleIdentifier == '{bundle_id}'"],
            capture_output=True, text=True, timeout=4,
        )
        paths = [p for p in r.stdout.splitlines() if p.endswith(".app")]
        if paths:
            from pathlib import Path
            return Path(paths[0]).stem
    except Exception:
        pass
    # 2. AppleScript: the inverse of "id of app X" doesn't exist
    # directly, but we can try `name of` against the bundle id. Apple's
    # own apps respond to `tell app id "com.apple.Notes"`.
    for candidate in (bundle_id, bundle_id.lower(), bundle_id.title()):
        try:
            r = subprocess.run(
                ["osascript", "-e",
                 f'tell application id "{candidate}" to return name'],
                capture_output=True, text=True, timeout=4,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            continue
    # 3. Title-cased last component.
    tail = bundle_id.rsplit(".", 1)[-1]
    return tail[:1].upper() + tail[1:] if tail else None


def activate_app(bundle_id_or_name: str) -> None:
    """Foreground the named app. Accepts either a bundle id like
    'com.apple.calculator' or a user-facing name like 'Calculator'.
    Idempotent: re-running on an already-foregrounded app is a no-op.

    Three strategies, applied in order:
      1. `open -a <Name> -F` — LaunchServices, non-blocking. Survives
         apps that haven't fully booted yet (Cursor cold start, etc.).
      2. `osascript tell ... to activate` — the canonical path. Used as
         a follow-up to ensure focus actually moved.
      3. Fall through and let the caller's window probe time out cleanly.
    """
    target = bundle_id_or_name
    name: Optional[str] = None
    if "." in bundle_id_or_name:
        name = _bundle_id_to_app_name(bundle_id_or_name)
        if name:
            target = name

    # Step 1: `open -a` is non-blocking and tolerates partially-launched
    # apps. -g keeps the focus story controllable; -j hides the dock
    # bounce for a tidier demo.
    try:
        subprocess.run(
            ["open", "-a", target, "-g"],
            check=False, capture_output=True, timeout=8,
        )
    except subprocess.TimeoutExpired:
        pass

    # Step 2: AppleScript activate. May fail with -1728 / -1712 while the
    # app is mid-launch; we tolerate that and retry once after a sleep.
    for _ in range(2):
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'tell application "{target}" to activate'],
                check=True, capture_output=True, timeout=15,
            )
            break
        except (subprocess.CalledProcessError,
                subprocess.TimeoutExpired):
            time.sleep(1.0)
            continue
    # CUA_DRIVER_GUIDE.md §5: a short sleep gives AppKit time to realise
    # the window subtree.
    time.sleep(0.8)


_TCC_URLS = {
    "accessibility":
        "x-apple.systempreferences:com.apple.preference.security"
        "?Privacy_Accessibility",
    "screen_recording":
        "x-apple.systempreferences:com.apple.preference.security"
        "?Privacy_ScreenCapture",
}


def open_permissions_settings(kind: str) -> None:
    url = _TCC_URLS.get(kind)
    if not url:
        raise ValueError(
            f"unknown permission kind {kind!r}; expected one of "
            f"{list(_TCC_URLS)}"
        )
    subprocess.run(["open", url], check=False, timeout=5)


def qt_env() -> dict[str, str]:
    # Qt apps on macOS auto-expose AX through Cocoa; no env var needed.
    return {}
