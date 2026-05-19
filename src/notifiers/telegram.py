"""Telegram Bot API notifier.

``TelegramNotifier`` POSTs a Telegram ``sendMessage`` payload to
``https://api.telegram.org/bot<token>/sendMessage`` using the Bot API's
``MarkdownV2`` parse mode. The rendered message body is::

    *<title>*\n\n<message>

with every Telegram MarkdownV2 reserved character escaped via
:func:`escape_markdown_v2` so that user-supplied content cannot accidentally
break the surrounding bold formatting.

Retry semantics are inherited verbatim from :class:`WebhookNotifier`:
3 attempts by default, exponential backoff, retry only on
``httpx.TimeoutException``/``httpx.NetworkError`` and ``5xx`` responses,
and never on ``4xx`` (Telegram replies with ``400`` for malformed payloads
such as an unknown ``chat_id`` or invalid escape sequence - those will
not be cured by retrying).

The MarkdownV2 reserved-character set is documented at
<https://core.telegram.org/bots/api#markdownv2-style>.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .base import NotifyEvent
from .webhook import WebhookNotifier

#: Telegram Bot API documents these characters as reserved in MarkdownV2.
#: Any reserved character that appears as literal user content must be
#: preceded by a backslash. The order does not matter for escaping; it is
#: kept stable here so :func:`escape_markdown_v2` is deterministic.
MARKDOWN_V2_SPECIAL_CHARS: tuple[str, ...] = (
    "_",
    "*",
    "[",
    "]",
    "(",
    ")",
    "~",
    "`",
    ">",
    "#",
    "+",
    "-",
    "=",
    "|",
    "{",
    "}",
    ".",
    "!",
)


def escape_markdown_v2(text: str) -> str:
    """Backslash-escape every Telegram MarkdownV2 reserved character.

    The function also escapes literal backslashes so existing ``\\`` in
    user content do not accidentally form escape sequences after the
    Telegram client re-parses the payload.

    Parameters
    ----------
    text:
        Raw user-supplied text (notification title or message). May be empty.

    Returns
    -------
    str
        The escaped string, safe to embed inside a MarkdownV2 payload.
    """
    if not text:
        return text
    # Backslash first so we don't double-escape the backslashes we add for
    # other reserved characters.
    out = text.replace("\\", "\\\\")
    for ch in MARKDOWN_V2_SPECIAL_CHARS:
        out = out.replace(ch, "\\" + ch)
    return out


DEFAULT_API_BASE: str = "https://api.telegram.org"


class TelegramNotifier(WebhookNotifier):
    """POST a :class:`NotifyEvent` to a Telegram bot's ``sendMessage`` endpoint.

    Inherits the retry loop from :class:`WebhookNotifier` and overrides
    :meth:`build_payload` to emit Telegram's ``sendMessage`` schema.

    Parameters
    ----------
    bot_token:
        The ``HTTP-API`` token returned by ``@BotFather`` (looks like
        ``"123456:ABCdefGHIjkl..."``). Used to build the request URL; never
        embedded in the JSON body.
    chat_id:
        The destination chat id (string or numeric; passed through to the
        Bot API verbatim). Channel usernames such as ``"@my_channel"`` are
        accepted by Telegram and forwarded unmodified.
    api_base:
        Override the API base URL. Defaults to ``https://api.telegram.org``.
        Useful for tests that boot a local FastAPI server.
    headers, timeout, max_attempts, backoff_base, backoff_max:
        Forwarded to :class:`WebhookNotifier` - see that class for retry
        semantics.
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        api_base: str = DEFAULT_API_BASE,
        headers: Mapping[str, str] | None = None,
        timeout: float = 10.0,
        max_attempts: int = 3,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
    ) -> None:
        if not bot_token:
            raise ValueError("TelegramNotifier requires a non-empty bot_token")
        if not chat_id:
            raise ValueError("TelegramNotifier requires a non-empty chat_id")

        base = api_base.rstrip("/")
        url = f"{base}/bot{bot_token}/sendMessage"
        super().__init__(
            url=url,
            headers=headers,
            timeout=timeout,
            max_attempts=max_attempts,
            backoff_base=backoff_base,
            backoff_max=backoff_max,
        )
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_base = base

    # ------------------------------------------------------------------
    # payload
    # ------------------------------------------------------------------

    def build_payload(self, event: NotifyEvent) -> dict[str, Any]:
        """Return the Telegram ``sendMessage`` body for ``event``.

        Shape::

            {
              "chat_id": <chat_id>,
              "text": "*<escaped title>*\\n\\n<escaped message>",
              "parse_mode": "MarkdownV2"
            }

        ``chat_id`` is forwarded as the string provided at construction
        time - Telegram accepts both numeric ids and channel usernames in
        the same field.
        """
        text = self._format_text(event.title, event.message)
        return {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "MarkdownV2",
        }

    @staticmethod
    def _format_text(title: str, message: str) -> str:
        """Render the MarkdownV2 message body.

        The bold markers around the title are deliberately *not* passed
        through :func:`escape_markdown_v2` - they are formatting directives,
        not user content. Only the user-supplied title and message text get
        escaped.
        """
        return f"*{escape_markdown_v2(title)}*\n\n{escape_markdown_v2(message)}"


__all__ = [
    "DEFAULT_API_BASE",
    "MARKDOWN_V2_SPECIAL_CHARS",
    "TelegramNotifier",
    "escape_markdown_v2",
]
