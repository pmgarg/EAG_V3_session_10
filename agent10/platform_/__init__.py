"""
platform_/ — OS-specific glue. The driver wrapper above us is
platform-agnostic; this package picks the right implementation at
startup based on sys.platform.

Three concrete platforms, one common API:

    activate_app(bundle_id_or_name)    bring app to foreground
    open_permissions_settings(kind)    open the right System Settings panel
    qt_env() -> dict[str, str]         env vars Qt apps need to expose AX

If you wanted to dispatch by skill name in the orchestrator the same
way we do for `computer`, this is the place to do it: the file you read
is determined by the host OS, and the imports below pick exactly one.
"""
from __future__ import annotations

import sys

if sys.platform == "darwin":
    from . import _macos as impl
elif sys.platform.startswith("linux"):
    from . import _linux as impl
elif sys.platform == "win32":
    from . import _windows as impl
else:
    raise RuntimeError(f"Unsupported platform: {sys.platform}")


def name() -> str:
    """Short OS name string, useful for logs."""
    return impl.NAME


def activate_app(bundle_id_or_name: str) -> None:
    """Bring the named app to the foreground. Idempotent.

    On macOS this is the `osascript ... tell app to activate` dance from
    CUA_DRIVER_GUIDE.md §6.1 — necessary because cua-driver's launch_app
    explicitly does NOT steal focus. On Linux this calls wmctrl (X11) or
    portal services (Wayland). On Windows the driver's `bring_to_front`
    works natively; we shell to it there.
    """
    impl.activate_app(bundle_id_or_name)


def open_permissions_settings(kind: str) -> None:
    """Open the OS panel for `kind` in {accessibility, screen_recording}."""
    impl.open_permissions_settings(kind)


def qt_env() -> dict[str, str]:
    """Env vars to set when launching Qt apps so their AX tree is exposed."""
    return impl.qt_env()


__all__ = ["name", "activate_app", "open_permissions_settings", "qt_env"]
