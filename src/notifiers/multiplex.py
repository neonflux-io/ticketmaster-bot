"""Fan-out notifier that dispatches events across multiple concrete channels.

``MultiplexNotifier`` is the top-level notifier the bot wires into its run
loop. It owns a mapping of logical channel name -> concrete
:class:`Notifier` instance and a routing table mapping ``event_type`` ->
list of channel names. On every :meth:`notify` call it builds the list of
channels to dispatch to (either the routed subset or *all* channels when no
routing entry matches the event type) and dispatches concurrently. Each
channel runs in its own ``asyncio.Task``; one channel's exception is caught
and logged but does **not** cancel the others.

The class also exposes :meth:`from_config` which constructs the concrete
notifier instances from :mod:`src.registry.notifiers` so callers can wire
everything up directly from the YAML config.

Failure isolation
-----------------
A notifier ``notify`` coroutine may raise (typically
:class:`~src.notifiers.base.NotifierError` from
:class:`~src.notifiers.webhook.WebhookNotifier`) when its retry budget is
exhausted. :class:`MultiplexNotifier` wraps each per-channel coroutine with
a helper that swallows *and logs* the exception. The aggregate
:meth:`notify` therefore never raises - if every single channel fails the
operator still has the log trail, and a sibling channel's success is never
gated on another's failure.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from typing import Any

from ..registry import notifiers as notifier_registry
from .base import Notifier, NotifyEvent

log = logging.getLogger("ticketmaster-bot")


class MultiplexNotifier(Notifier):
    """Dispatch :class:`NotifyEvent` instances to many channels with isolation.

    Parameters
    ----------
    notifiers:
        Mapping of logical channel name -> concrete :class:`Notifier`
        instance. The mapping is copied defensively so external mutations
        do not affect dispatch.
    routing:
        Optional mapping of ``event_type`` -> list of channel names. If
        omitted (or if a particular event type is not present), the
        notifier fans the event out across **all** configured channels.
        Channel names in ``routing`` that are not present in ``notifiers``
        are skipped with a warning rather than raising.
    """

    def __init__(
        self,
        notifiers: Mapping[str, Notifier],
        routing: Mapping[str, Iterable[str]] | None = None,
    ) -> None:
        self.notifiers: dict[str, Notifier] = dict(notifiers)
        self.routing: dict[str, list[str]] = {
            event_type: list(channels) for event_type, channels in (routing or {}).items()
        }

    # ------------------------------------------------------------------
    # construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> MultiplexNotifier:
        """Build a :class:`MultiplexNotifier` from a config dict.

        Expected shape::

            channels: [web_a, discord_main]
            routing:
              cart_success: [web_a, discord_main]
              checkout_failure: [discord_main]
            channel_configs:
              web_a:
                type: webhook
                url: https://...
              discord_main:
                type: discord
                webhook_url: https://discord.com/api/webhooks/...

        Each entry in ``channel_configs`` carries a ``type`` key that is
        looked up in :mod:`src.registry.notifiers`. Every other key is
        forwarded as a keyword argument to the resolved notifier class's
        constructor. Channel names listed in ``channels`` but missing from
        ``channel_configs`` are skipped with a warning - this keeps the
        loader forgiving of partial configs (e.g. a Slack channel listed
        but not yet configured).
        """
        channel_names: list[str] = list(config.get("channels") or [])
        routing_raw = config.get("routing") or {}
        channel_configs: Mapping[str, Mapping[str, Any]] = config.get("channel_configs") or {}

        notifiers: dict[str, Notifier] = {}
        for name in channel_names:
            channel_cfg = channel_configs.get(name)
            if channel_cfg is None:
                log.warning(
                    "[multiplex] channel %r listed in channels but has no entry "
                    "in channel_configs; skipping",
                    name,
                )
                continue
            kwargs = dict(channel_cfg)
            notifier_type = kwargs.pop("type", None)
            if not notifier_type:
                log.warning(
                    "[multiplex] channel %r has no 'type' field; skipping",
                    name,
                )
                continue
            try:
                notifier_cls = notifier_registry.get(str(notifier_type))
            except Exception as exc:  # noqa: BLE001 - registry errors logged + skipped
                log.warning(
                    "[multiplex] channel %r requested unknown notifier type %r: %s",
                    name,
                    notifier_type,
                    exc,
                )
                continue
            try:
                notifiers[name] = notifier_cls(**kwargs)
            except Exception:
                log.exception(
                    "[multiplex] failed to construct notifier %r (type=%r)",
                    name,
                    notifier_type,
                )
                continue

        routing = {
            str(event_type): [str(c) for c in (channels or [])]
            for event_type, channels in routing_raw.items()
        }
        return cls(notifiers=notifiers, routing=routing)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def channels_for(self, event_type: str) -> list[str]:
        """Return the list of channel names that should receive ``event_type``.

        If a routing entry exists, return that list filtered to channels we
        actually have notifier instances for (unknown names are skipped
        with a warning, never raise). Otherwise fall back to *every*
        configured channel.
        """
        if event_type in self.routing:
            channels: list[str] = []
            for name in self.routing[event_type]:
                if name in self.notifiers:
                    channels.append(name)
                else:
                    log.warning(
                        "[multiplex] routing for event %r references unknown channel %r; skipping",
                        event_type,
                        name,
                    )
            return channels
        return list(self.notifiers.keys())

    async def notify(self, event: NotifyEvent) -> None:
        """Dispatch ``event`` to every channel resolved for its event type.

        Each channel's :meth:`Notifier.notify` runs in its own task; an
        exception from any one task is logged and isolated so siblings
        still complete. The method itself never raises.
        """
        channels = self.channels_for(event.event_type)
        if not channels:
            log.debug(
                "[multiplex] no channels resolved for event_type=%r; nothing to do",
                event.event_type,
            )
            return

        tasks = [
            asyncio.create_task(
                self._dispatch_one(name, event),
                name=f"multiplex:{name}",
            )
            for name in channels
        ]
        # Don't use return_exceptions so a CancelledError still propagates,
        # but we already guard each task in ``_dispatch_one``.
        await asyncio.gather(*tasks, return_exceptions=False)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _dispatch_one(self, name: str, event: NotifyEvent) -> None:
        """Run a single channel's notify and swallow non-cancellation errors."""
        notifier = self.notifiers[name]
        try:
            await notifier.notify(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "[multiplex] channel %r failed to deliver event %r",
                name,
                event.event_type,
            )


__all__ = ["MultiplexNotifier"]
