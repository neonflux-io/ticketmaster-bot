"""Discord webhook notifier.

``DiscordNotifier`` POSTs a Discord-shaped JSON payload to a Discord
incoming-webhook URL. The payload encodes the :class:`NotifyEvent` as both
plain text (``content``) and a rich embed (``embeds[0]``) carrying a title,
description, severity-mapped colour, and an ISO-8601 timestamp.

Retry semantics are inherited verbatim from :class:`WebhookNotifier`:
3 attempts, exponential backoff, retry only on
``httpx.TimeoutException``/``httpx.NetworkError`` and ``5xx`` responses, and
never on ``4xx``. Discord's standard success status is 204 No Content; any
2xx is treated as success.

Colour map (24-bit RGB, encoded as int per Discord's webhook API):

* ``"info"``    → cyan  (``0x00FFFF``)
* ``"success"`` → green (``0x00FF00``)
* ``"warning"`` → amber (``0xFFA500``)
* ``"error"``   → red   (``0xFF0000``)
* anything else → Discord-neutral grey (``0x99AAB5``)
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .base import NotifyEvent
from .webhook import WebhookNotifier

# Discord embed colours are 24-bit integers (0xRRGGBB).
INFO_COLOR: int = 0x00FFFF
SUCCESS_COLOR: int = 0x00FF00
WARNING_COLOR: int = 0xFFA500
ERROR_COLOR: int = 0xFF0000
DEFAULT_COLOR: int = 0x99AAB5

SEVERITY_COLORS: dict[str, int] = {
    "info": INFO_COLOR,
    "success": SUCCESS_COLOR,
    "warning": WARNING_COLOR,
    "error": ERROR_COLOR,
}


class DiscordNotifier(WebhookNotifier):
    """POST a :class:`NotifyEvent` to a Discord webhook.

    Inherits the retry loop from :class:`WebhookNotifier` and overrides
    :meth:`build_payload` to emit Discord's expected schema.

    Parameters
    ----------
    webhook_url:
        Fully-qualified Discord webhook URL (typically
        ``https://discord.com/api/webhooks/<id>/<token>``).
    username:
        Optional override displayed as the bot's username in the channel.
        Defaults to whatever the webhook was configured with on Discord's
        side when ``None``.
    avatar_url:
        Optional avatar override URL passed through to Discord.
    headers, timeout, max_attempts, backoff_base, backoff_max:
        Forwarded to :class:`WebhookNotifier` - see that class for retry
        semantics.
    """

    def __init__(
        self,
        webhook_url: str,
        *,
        username: str | None = None,
        avatar_url: str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 10.0,
        max_attempts: int = 3,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
    ) -> None:
        super().__init__(
            url=webhook_url,
            headers=headers,
            timeout=timeout,
            max_attempts=max_attempts,
            backoff_base=backoff_base,
            backoff_max=backoff_max,
        )
        self.username = username
        self.avatar_url = avatar_url

    @property
    def webhook_url(self) -> str:
        """Alias for ``self.url`` matching the public constructor name."""
        return self.url

    # ------------------------------------------------------------------
    # payload
    # ------------------------------------------------------------------

    def build_payload(self, event: NotifyEvent) -> dict[str, Any]:
        """Return the Discord-shaped webhook body for ``event``.

        Shape::

            {
              "content": "<title>: <message>",
              "embeds": [
                {
                  "title": <title>,
                  "description": <message>,
                  "color": <int>,
                  "timestamp": <ISO-8601 UTC>,
                  "fields": [ ... ]  # only when metadata is non-empty
                }
              ]
            }

        The plain-text ``content`` mirrors the embed so the message is
        readable on clients that don't render embeds (mobile push, etc.).
        """
        color = self._color_for_severity(event.severity)
        embed: dict[str, Any] = {
            "title": event.title,
            "description": event.message,
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        fields = self._metadata_to_fields(event.metadata)
        if fields:
            embed["fields"] = fields

        payload: dict[str, Any] = {
            "content": f"{event.title}: {event.message}",
            "embeds": [embed],
        }
        if self.username is not None:
            payload["username"] = self.username
        if self.avatar_url is not None:
            payload["avatar_url"] = self.avatar_url
        return payload

    @staticmethod
    def _color_for_severity(severity: str) -> int:
        """Map a severity token to the Discord embed colour integer."""
        return SEVERITY_COLORS.get(severity, DEFAULT_COLOR)

    @staticmethod
    def _metadata_to_fields(metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Render arbitrary metadata as Discord embed ``fields``.

        Discord caps field values at 1024 characters; long values are
        truncated with a trailing ellipsis. Field names are coerced to
        strings since Discord requires a non-empty ``name``.
        """
        fields: list[dict[str, Any]] = []
        for key, value in metadata.items():
            name = str(key) or "field"
            text = str(value)
            if len(text) > 1024:
                text = text[:1021] + "..."
            fields.append({"name": name, "value": text, "inline": True})
        return fields


__all__ = [
    "DEFAULT_COLOR",
    "ERROR_COLOR",
    "INFO_COLOR",
    "SEVERITY_COLORS",
    "SUCCESS_COLOR",
    "WARNING_COLOR",
    "DiscordNotifier",
]
