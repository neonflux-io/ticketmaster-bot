"""Best-effort desktop notifications backed by ``plyer``.

``DesktopNotifier`` is intentionally fault-tolerant: plyer's native backend
is unavailable on headless CI machines and inside some macOS sandboxes, so
every backend error is logged at debug level and swallowed. The notifier
always emits an info-level log line containing the notification title so the
operator has a single source of truth for what was sent regardless of
whether the OS toast actually rendered.

This module is the new home of the historical ``src.utils.notifier.notify``
behaviour, now wrapped behind the :class:`~src.notifiers.base.Notifier`
abstraction.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from rich.console import Console

from .base import Notifier, NotifyEvent

if TYPE_CHECKING:
    pass

log = logging.getLogger("ticketmaster-bot")
_console = Console()


class DesktopNotifier(Notifier):
    """Cross-platform desktop toast + console bell.

    Parameters
    ----------
    desktop:
        When ``True`` (default), attempt to dispatch a native OS toast via
        ``plyer.notification.notify``. When ``False`` the notifier still
        emits its info log line but skips the OS-toast call entirely - this
        is the headless / CI-friendly mode.
    sound:
        When ``True`` (default), ring the terminal bell via the shared rich
        console. Safe on terminals that don't support it.
    app_name:
        Name passed through to plyer as the originating application.
    timeout:
        Toast display time in seconds passed through to plyer.
    """

    def __init__(
        self,
        *,
        desktop: bool = True,
        sound: bool = True,
        app_name: str = "Ticketmaster Bot",
        timeout: int = 10,
    ) -> None:
        self.desktop = desktop
        self.sound = sound
        self.app_name = app_name
        self.timeout = timeout

    async def notify(self, event: NotifyEvent) -> None:
        """Surface ``event`` as a desktop toast (best-effort) + log line."""
        # Always log first so the operator sees the message even when the
        # native backend silently fails (common in headless CI).
        log.info("[notify] %s: %s", event.title, event.message)

        if self.desktop:
            await asyncio.to_thread(self._show_toast, event)

        if self.sound:
            try:
                _console.bell()
            except Exception as exc:  # noqa: BLE001 - bell is best-effort
                log.debug("Console bell failed: %s", exc)

    def _show_toast(self, event: NotifyEvent) -> None:
        """Synchronously dispatch the plyer toast; never raises."""
        try:
            from plyer import notification

            notification.notify(
                title=event.title,
                message=event.message,
                app_name=self.app_name,
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001 - notifications are best-effort
            log.debug("Desktop notification failed: %s", exc)


__all__ = ["DesktopNotifier"]
