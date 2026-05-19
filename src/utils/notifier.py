"""Desktop notifications using plyer (graceful fallback if unsupported)."""
from __future__ import annotations

import logging

from rich.console import Console

log = logging.getLogger("ticketmaster-bot")
_console = Console()


def notify(title: str, message: str, *, desktop: bool = True, sound: bool = True) -> None:
    """Send a desktop notification + optionally play a beep."""
    if not desktop:
        log.info("[notify] %s: %s", title, message)
        return

    try:
        from plyer import notification  # type: ignore[import-untyped]

        notification.notify(
            title=title,
            message=message,
            app_name="Ticketmaster Bot",
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 - notifications are best-effort
        log.debug("Desktop notification failed: %s", exc)

    if sound:
        try:
            _console.bell()
        except Exception:  # noqa: BLE001
            pass
