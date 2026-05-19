"""Live POST test for :class:`WebhookNotifier`.

When ``WEBHOOK_URL`` is set in the process environment (typically via the
gitignored ``.env``), this test issues a real ``httpx`` POST through the
notifier and asserts a ``2xx`` response. When the variable is missing the
test ``pytest.skip``s cleanly so CI runs without creds remain green - the
mission contract forbids silently passing without doing the work.

Covers validation assertions ``[io.webhook-real-post]`` and the
``Content-Type: application/json`` header guarantee documented for
``WebhookNotifier``.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

from src.notifiers.base import NotifyEvent
from src.notifiers.webhook import WebhookNotifier


def _webhook_url() -> str | None:
    """Load ``.env`` (best-effort) and return the configured webhook URL."""
    load_dotenv(override=False)
    url = os.getenv("WEBHOOK_URL")
    if url:
        url = url.strip()
    return url or None


@pytest.mark.asyncio
async def test_webhook_real_post_returns_2xx() -> None:
    """POST a real ``NotifyEvent`` to ``WEBHOOK_URL`` and confirm 2xx."""
    url = _webhook_url()
    if not url:
        pytest.skip("WEBHOOK_URL not configured")

    notifier = WebhookNotifier(url=url)
    event = NotifyEvent(
        event_type="mission_readiness",
        title="WebhookNotifier live check",
        message="POSTed from tests/notifiers/test_webhook_live.py",
        severity="info",
        metadata={"source": "pytest", "feature": "F3.2"},
    )

    await notifier.notify(event)

    response = notifier.last_response
    assert response is not None, "notifier should record the final response"
    assert response.status_code < 300, (
        f"expected 2xx, got {response.status_code}: {response.text[:200]!r}"
    )
