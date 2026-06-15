"""
permissions.py — cross-platform permission prober + prompt flow.

The agent reads two truths at startup:

  1. The OS-level grant status (TCC on macOS, portal on Wayland, UAC on
     Windows). cua-driver's `check_permissions` does this for us on all
     three platforms.

  2. Whether each grant is actually NEEDED for the tasks we're about to
     run. Layer 2a (deterministic hotkeys + AX text reads) needs only
     Accessibility. Layer 2b (any element_index action) needs
     Accessibility. Layer 3 (screenshot + vision) needs Screen
     Recording. So if the run-plan has no vision steps and Screen
     Recording is missing, we just warn — we don't block.

Public API:

    ensure_permissions(need_screen_recording: bool = False)
        Probe, classify, and either (a) confirm we're good, (b) open the
        right Settings panels and wait for the user, or (c) raise
        PermissionDenied if the user opts not to grant.
"""
from __future__ import annotations

import sys
import time

from agent10 import driver, platform_


class PermissionDenied(RuntimeError):
    """Raised when a required permission is missing and the user has
    chosen not to grant it. Caller decides whether to abort or downgrade
    the run plan."""

    def __init__(self, missing: list[str]):
        super().__init__(
            f"Missing required permissions: {', '.join(missing)}. "
            "Open System Settings and grant them to CuaDriver "
            "(com.trycua.driver), then retry."
        )
        self.missing = missing


def probe() -> dict[str, bool]:
    """Return the current grant status as a flat dict.

    On macOS this maps cua-driver's check_permissions output to:

        {"accessibility": bool, "screen_recording": bool}

    On Linux/Windows the same keys are present but their meaning is
    looser: accessibility tracks whether AT-SPI / UIA are reachable,
    screen_recording tracks whether screenshot APIs work without prompt.
    """
    state = driver.check_permissions()
    return {
        "accessibility": bool(state.get("accessibility")),
        "screen_recording": bool(state.get("screen_recording")),
    }


def ensure_permissions(*, need_screen_recording: bool = False,
                      interactive: bool = True) -> dict[str, bool]:
    """Probe permissions and ensure the ones we need are granted.

    `need_screen_recording` is set by the caller based on whether the
    run plan includes any Layer 3 (vision) steps. If False, Screen
    Recording being off is reported as a warning, not an error.

    When `interactive=True` and a required grant is missing, this opens
    the OS Settings panel for the user and polls every 2s for up to 90s
    so the agent can pick up the grant as soon as the user toggles it.
    """
    status = probe()
    missing: list[str] = []
    if not status["accessibility"]:
        missing.append("accessibility")
    if need_screen_recording and not status["screen_recording"]:
        missing.append("screen_recording")

    if not missing:
        if not status["screen_recording"] and not need_screen_recording:
            print(
                "[permissions] OK (Accessibility granted; Screen Recording "
                "off — not needed for this run)"
            )
        else:
            print("[permissions] OK")
        return status

    if not interactive:
        raise PermissionDenied(missing)

    # Open Settings panels and wait. macOS needs a driver restart for the
    # grant to take effect; we handle that automatically.
    print(f"[permissions] missing: {', '.join(missing)}")
    print("[permissions] opening System Settings panels...")
    for kind in missing:
        platform_.open_permissions_settings(kind)
        time.sleep(0.5)

    print(
        "[permissions] In System Settings → Privacy & Security:\n"
        "  - Add CuaDriver (or /Applications/CuaDriver.app) to each list\n"
        "  - Turn the toggle ON\n"
        "  - I'll auto-detect the grant and continue."
    )

    # Some grants (macOS Screen Recording in particular) require the
    # CuaDriver process to be killed and respawned for the grant to
    # take effect. We poll for up to 90s and restart the daemon every
    # 30s as a recovery measure.
    deadline = time.time() + 90
    last_restart = 0
    while time.time() < deadline:
        time.sleep(2)
        status = probe()
        still_missing = [m for m in missing if not status[m]]
        if not still_missing:
            print("[permissions] all required grants now present")
            return status
        # Every 30s try a daemon restart.
        if time.time() - last_restart > 30 and sys.platform == "darwin":
            print("[permissions] restarting CuaDriver daemon to pick up grant...")
            try:
                driver.call("shutdown", {}, timeout=5)
            except Exception:
                pass
            time.sleep(1)
            driver.ensure_daemon()
            last_restart = time.time()

    raise PermissionDenied(missing)
