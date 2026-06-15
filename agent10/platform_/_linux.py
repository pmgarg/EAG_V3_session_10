"""
platform_/_linux.py — Linux-specific glue.

Notes from the session writeup:

  * X11 sessions need no special grants. Apps expose AT-SPI by default.
  * Wayland needs portal grants per session — interactive, not persistent.
    Most production setups still default to X11 until the portal flow
    matures.
  * Qt apps need `QT_ACCESSIBILITY=1` in their environment at launch time
    for AT-SPI to be exposed. Pass this through to driver.launch_app
    via its `env=` kwarg.

This implementation is best-effort. The agent should work for native
GTK/Qt apps under X11; Wayland portal flows and exotic toolkits are
left as gaps to fill if/when this runs on Linux in anger.
"""
from __future__ import annotations

import os
import shutil
import subprocess

NAME = "Linux"


def _running_under_wayland() -> bool:
    return bool(
        os.environ.get("WAYLAND_DISPLAY")
        or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
    )


def activate_app(bundle_id_or_name: str) -> None:
    """Best-effort: wmctrl on X11. On Wayland we just sleep — most
    compositors don't allow programmatic activation without portal
    consent, and `cua-driver` should still find the window by name."""
    if _running_under_wayland():
        # No reliable cross-compositor activation on Wayland.
        return
    wmctrl = shutil.which("wmctrl")
    if wmctrl:
        # `-a` matches by window title substring.
        try:
            subprocess.run(
                [wmctrl, "-a", bundle_id_or_name],
                check=False, timeout=4,
            )
        except subprocess.TimeoutExpired:
            pass


def open_permissions_settings(kind: str) -> None:
    """Linux permissions are per-session portal grants on Wayland and
    nothing on X11. Best-effort: print a hint."""
    print(
        f"[platform/linux] {kind} grants are per-session on Wayland and "
        "not required on X11. If get_window_state returns empty under "
        "Wayland, accept the portal dialog when it appears."
    )


def qt_env() -> dict[str, str]:
    """Critical: Qt apps need this for AT-SPI to expose their tree."""
    return {"QT_ACCESSIBILITY": "1"}
