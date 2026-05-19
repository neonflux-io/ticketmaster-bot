"""Slack incoming-webhook notifier.

``SlackNotifier`` POSTs a Slack-shaped JSON payload to a Slack incoming
webhook URL (typically ``https://hooks.slack.com/services/<team>/<chan>/<token>``).

Payload shape (per Slack's `Block Kit reference
<https://api.slack.com/reference/block-kit/blocks#section>`_)::

    {
      "text": "<plaintext fallback>",
      "blocks": [
        {
          "type": "section",
          "text": {
            "type": "mrkdwn",
            "text": "*<title>*\\n<message>"
          }
        }
      ]
    }

The leading ``text`` field is the fallback shown in mobile push
notifications and any client that cannot render Block Kit. ``blocks``
carries the rich rendering used in-channel.

Success protocol
----------------
Slack does **not** use HTTP status codes to indicate webhook errors. The
service routinely returns ``HTTP 200`` with a plain-text body describing
the failure (``invalid_payload``, ``channel_is_archived``,
``no_service``, ...). The shipped client therefore inspects the **body**
on every 2xx and treats anything other than the literal string ``"ok"``
as a notifier failure. Slack's documented success body is exactly
``"ok"`` (no newline, no JSON wrapper); minor whitespace is tolerated.

Retry semantics are inherited verbatim from :class:`WebhookNotifier`:
3 attempts by default, exponential backoff, retry only on
``httpx.TimeoutException``/``httpx.NetworkError`` and ``5xx`` responses,
never on ``4xx``. Slack-side body errors are **terminal**: a malformed
payload will not become well-formed on retry, so :class:`SlackNotifier`
raises :class:`NotifierError` immediately on a non-``ok`` body without
re-sending.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .base import NotifierError, NotifyEvent
from .webhook import WebhookNotifier

#: Slack mrkdwn requires a small set of characters to be HTML-escaped to
#: keep them from being parsed as special tokens (``<...|...>`` link
#: syntax in particular). The list is documented at
#: <https://api.slack.com/reference/surfaces/formatting#escaping>.
MRKDWN_ESCAPES: tuple[tuple[str, str], ...] = (
    ("&", "&amp;"),
    ("<", "&lt;"),
    (">", "&gt;"),
)


def escape_mrkdwn(text: str) -> str:
    """Escape the three characters Slack mrkdwn treats as special.

    Slack only requires ``&``, ``<``, and ``>`` to be HTML-escaped inside
    a ``mrkdwn`` text element; other formatting characters (``*``, ``_``,
    ``~``, `` ` ``) are intentionally NOT escaped because they are still
    valid formatting markers and Slack does not provide a backslash-escape
    syntax for them.

    Parameters
    ----------
    text:
        Raw user-supplied text. May be empty.

    Returns
    -------
    str
        The HTML-escaped string, safe to embed inside a Slack ``mrkdwn``
        text element. ``&`` is replaced first so the replacement entities
        we inject for ``<`` and ``>`` are not double-escaped.
    """
    if not text:
        return text
    out = text
    for ch, repl in MRKDWN_ESCAPES:
        out = out.replace(ch, repl)
    return out


#: Slack's documented success body for an incoming-webhook POST. Anything
#: else (case-sensitive, whitespace stripped) is treated as a failure.
SLACK_OK_BODY: str = "ok"


class SlackNotifier(WebhookNotifier):
    """POST a :class:`NotifyEvent` to a Slack incoming webhook.

    Parameters
    ----------
    webhook_url:
        Fully-qualified Slack incoming-webhook URL.
    headers, timeout, max_attempts, backoff_base, backoff_max:
        Forwarded to :class:`WebhookNotifier` - see that class for retry
        semantics on transport errors and 5xx responses.

    Notes
    -----
    Unlike Discord/Telegram, a 2xx is **not** sufficient evidence of
    success: Slack returns 200 + plain-text on misuse. The body is
    validated by :meth:`notify` after the parent's retry loop returns.
    """

    def __init__(
        self,
        webhook_url: str,
        *,
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

    @property
    def webhook_url(self) -> str:
        """Alias for ``self.url`` matching the public constructor name."""
        return self.url

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    async def notify(self, event: NotifyEvent) -> None:
        """POST ``event`` to Slack and validate the response body.

        The parent :class:`WebhookNotifier`'s retry loop handles transport
        errors and 5xx responses. Once it returns, the captured response
        is on :attr:`last_response`; this override then inspects the body
        and raises :class:`NotifierError` if it is anything other than
        Slack's documented ``"ok"`` success token. A non-``ok`` body is
        terminal - it indicates a malformed request, not a transient
        failure, so we do not retry.
        """
        await super().notify(event)

        response = self.last_response
        # WebhookNotifier.notify guarantees either a 2xx ``last_response``
        # was set, or an exception was raised; the defensive check below
        # keeps mypy happy and surfaces unexpected upstream contract drift.
        if response is None:  # pragma: no cover - defensive
            raise NotifierError(
                f"Slack POST to {self.url} returned no response"
            )

        body = response.text.strip()
        if body != SLACK_OK_BODY:
            raise NotifierError(
                f"Slack POST to {self.url} returned status "
                f"{response.status_code} but body was {body!r} "
                f"(expected {SLACK_OK_BODY!r})"
            )

    # ------------------------------------------------------------------
    # payload
    # ------------------------------------------------------------------

    def build_payload(self, event: NotifyEvent) -> dict[str, Any]:
        """Return the Slack-shaped incoming-webhook body for ``event``.

        Shape::

            {
              "text": "<title>: <message>",
              "blocks": [
                {
                  "type": "section",
                  "text": {
                    "type": "mrkdwn",
                    "text": "*<title>*\\n<message>"
                  }
                }
              ]
            }

        The plain-text ``text`` mirrors ``title: message`` and is what
        Slack mobile push notifications show. The ``blocks`` array carries
        the rich rendering used in-channel; the title is bolded via
        Slack's ``mrkdwn`` ``*bold*`` syntax. User content has its three
        Slack mrkdwn special characters HTML-escaped via
        :func:`escape_mrkdwn` so values like ``<>`` are never mistaken for
        link or user-mention tokens.
        """
        title_escaped = escape_mrkdwn(event.title)
        message_escaped = escape_mrkdwn(event.message)
        block_text = f"*{title_escaped}*\n{message_escaped}"
        return {
            "text": f"{event.title}: {event.message}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": block_text,
                    },
                }
            ],
        }


__all__ = [
    "MRKDWN_ESCAPES",
    "SLACK_OK_BODY",
    "SlackNotifier",
    "escape_mrkdwn",
]
