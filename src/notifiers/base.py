"""Notifier base layer: :class:`NotifyEvent` payload + :class:`Notifier` ABC.

A :class:`NotifyEvent` is a structured message any notifier channel can
consume - the :class:`Notifier` ABC defines a single ``async notify(event)``
coroutine that every channel implementation must provide. The dataclass and
the ABC are intentionally tiny so non-trivial channel logic (HTTP POST
payloads, retry policies, fan-out routing) lives inside each concrete
notifier rather than leaking into a shared base.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class NotifierError(Exception):
    """Base class for notifier-side failures.

    Concrete notifiers (Discord/Webhook/Telegram/Slack) raise this when an
    outbound delivery cannot be completed after the configured retries.
    Best-effort notifiers (e.g. desktop) may suppress backend errors and
    simply log; they never raise this.
    """


@dataclass
class NotifyEvent:
    """A structured notification payload shared across every channel.

    Attributes
    ----------
    event_type:
        Short logical name identifying the runtime event, e.g.
        ``"cart_success"``, ``"checkout_failure"``, ``"sold_out"``. Routing
        rules (used by ``MultiplexNotifier``) key off this value, so it must
        be stable and machine-readable.
    title:
        Human-readable headline shown in desktop toasts, embed titles, etc.
    message:
        Body text describing the event.
    severity:
        One of ``"info"``, ``"warning"``, ``"error"``. Notifier channels are
        free to map this to colours, log levels, or icons.
    metadata:
        Arbitrary structured context (account name, section, price, etc.).
        Notifier implementations may merge this into outbound payloads -
        notably, HTTP-based channels usually serialise it under a
        ``metadata`` key so downstream consumers can react programmatically.
    """

    event_type: str
    title: str
    message: str
    severity: str = "info"
    metadata: dict[str, Any] = field(default_factory=dict)


class Notifier(ABC):
    """Abstract base class for notifier channels.

    Concrete subclasses must implement :meth:`notify`. The coroutine must
    not raise on transient errors when the channel is documented as
    best-effort (e.g. desktop toasts); HTTP-based channels should raise
    :class:`NotifierError` after exhausting their retry budget.
    """

    @abstractmethod
    async def notify(self, event: NotifyEvent) -> None:
        """Deliver ``event`` through this channel.

        Implementations should be safe to call from any asyncio context and
        must not block the event loop on synchronous I/O (use ``asyncio.to_thread``
        when a backend exposes only a blocking API).
        """


__all__ = ["Notifier", "NotifierError", "NotifyEvent"]
