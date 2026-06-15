"""
platform_/_windows.py — Windows-specific glue.

Notes from the session writeup:

  * Most Windows apps need no special grants.
  * Elevated apps (installers, system settings) require the agent itself
    to run elevated. The driver's bring_to_front works natively on
    Windows — unlike macOS where it's a stub.
"""
from __future__ import annotations

import subprocess

NAME = "Windows"


def activate_app(bundle_id_or_name: str) -> None:
    """Defer to the driver's native bring_to_front on Windows. On macOS
    that's a stub; here it's the real thing."""
    # Lazy import to avoid a cycle when this file is loaded just for
    # type-checking on a non-Windows host.
    from agent10 import driver
    try:
        driver.call("bring_to_front", {"app": bundle_id_or_name},
                    timeout=10.0)
    except Exception:
        # Fall back: best-effort window activation via powershell.
        # AppActivate matches by window title.
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f'$wshell = New-Object -ComObject wscript.shell; '
             f'$wshell.AppActivate("{bundle_id_or_name}")'],
            check=False, timeout=5,
        )


def open_permissions_settings(kind: str) -> None:
    """Best-effort: open the relevant ms-settings: URI."""
    targets = {
        "accessibility": "ms-settings:privacy-general",
        "screen_recording": "ms-settings:privacy-broadfilesystemaccess",
    }
    uri = targets.get(kind, "ms-settings:privacy")
    subprocess.run(["cmd", "/c", "start", uri], check=False, timeout=5)


def qt_env() -> dict[str, str]:
    return {}
