"""Fan-out + failure-isolation tests for :class:`MultiplexNotifier`.

Drives real :class:`~src.notifiers.webhook.WebhookNotifier` instances against
locally booted FastAPI test servers on ephemeral ports. No mocks - every
assertion is grounded in real socket traffic observed by the test endpoints.

Covers:

* ``[io.multiplex-fanout-routing]`` - routing maps an event type to a subset
  of channels and only those channels receive the POST.
* ``[io.multiplex-isolated-failures]`` - one channel that always 500s does
  not prevent the other channels from receiving their POSTs and the
  ``notify`` coroutine completes without raising.

Also covers a couple of additional behaviours that fall out of the feature
description:

* Missing routing for an event type defaults to fan-out across **all**
  configured channels.
* :meth:`MultiplexNotifier.from_config` constructs concrete notifier
  instances from the ``NotifierRegistry`` and behaves identically to the
  direct-mapping constructor.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response

from src.notifiers.base import NotifyEvent
from src.notifiers.multiplex import MultiplexNotifier
from src.notifiers.webhook import WebhookNotifier


def _free_port() -> int:
    """Bind to port 0 and return the kernel-assigned port number."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _RecordingServer:
    """FastAPI app that records every POST it receives.

    The pre-programmed ``status`` is returned on every request. ``requests``
    accumulates the captured headers/body so tests can assert on hit counts
    and payload contents.
    """

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[dict[str, object]] = []
        self.app = FastAPI()

        @self.app.post("/hook")
        async def hook(request: Request) -> Response:
            body = await request.body()
            self.requests.append(
                {
                    "headers": {k.lower(): v for k, v in request.headers.items()},
                    "body": body,
                }
            )
            return Response(status_code=self.status)

    @property
    def hits(self) -> int:
        return len(self.requests)


@asynccontextmanager
async def _serve(server: _RecordingServer) -> AsyncIterator[str]:
    """Boot ``server`` on an ephemeral port and yield its base URL."""
    port = _free_port()
    config = uvicorn.Config(
        app=server.app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        lifespan="off",
        access_log=False,
    )
    uvi = uvicorn.Server(config)
    task = asyncio.create_task(uvi.serve())

    base = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(timeout=2.0) as probe:
        for _ in range(50):
            if uvi.started:
                break
            await asyncio.sleep(0.05)
        else:  # pragma: no cover - defensive
            raise RuntimeError("uvicorn never started")
        for _ in range(50):
            try:
                await probe.get(base + "/__nope__")
                break
            except httpx.HTTPError:
                await asyncio.sleep(0.05)

    try:
        yield base
    finally:
        uvi.should_exit = True
        await asyncio.wait_for(task, timeout=5.0)


def _make_webhook(base: str) -> WebhookNotifier:
    """Build a WebhookNotifier with single-attempt no-backoff tuning."""
    return WebhookNotifier(
        url=f"{base}/hook",
        timeout=2.0,
        max_attempts=1,
        backoff_base=0.01,
    )


def _make_event(event_type: str = "cart_success") -> NotifyEvent:
    return NotifyEvent(
        event_type=event_type,
        title="Cart ready",
        message="Tickets reserved",
        severity="info",
        metadata={"account": "alice"},
    )


# ---------------------------------------------------------------------------
# [io.multiplex-fanout-routing]
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiplex_routes_event_to_subset_of_channels() -> None:
    """Routing maps cart_success -> [a, b]; only a and b get one POST."""
    srv_a = _RecordingServer(status=200)
    srv_b = _RecordingServer(status=200)
    srv_c = _RecordingServer(status=200)

    async with AsyncExitStack() as stack:
        base_a = await stack.enter_async_context(_serve(srv_a))
        base_b = await stack.enter_async_context(_serve(srv_b))
        base_c = await stack.enter_async_context(_serve(srv_c))

        notifier = MultiplexNotifier(
            notifiers={
                "a": _make_webhook(base_a),
                "b": _make_webhook(base_b),
                "c": _make_webhook(base_c),
            },
            routing={"cart_success": ["a", "b"]},
        )
        await notifier.notify(_make_event("cart_success"))

    assert srv_a.hits == 1, f"channel a expected 1 hit, got {srv_a.hits}"
    assert srv_b.hits == 1, f"channel b expected 1 hit, got {srv_b.hits}"
    assert srv_c.hits == 0, f"channel c must NOT receive a POST, got {srv_c.hits}"


@pytest.mark.asyncio
async def test_multiplex_routes_distinct_event_types_distinctly() -> None:
    """Different event types follow their own routing entries."""
    srv_a = _RecordingServer(status=200)
    srv_b = _RecordingServer(status=200)

    async with AsyncExitStack() as stack:
        base_a = await stack.enter_async_context(_serve(srv_a))
        base_b = await stack.enter_async_context(_serve(srv_b))

        notifier = MultiplexNotifier(
            notifiers={
                "a": _make_webhook(base_a),
                "b": _make_webhook(base_b),
            },
            routing={
                "cart_success": ["a"],
                "checkout_failure": ["b"],
            },
        )
        await notifier.notify(_make_event("cart_success"))
        await notifier.notify(_make_event("checkout_failure"))

    assert srv_a.hits == 1
    assert srv_b.hits == 1


@pytest.mark.asyncio
async def test_multiplex_unrouted_event_fans_out_to_all_channels() -> None:
    """When routing has no entry for an event type, every channel fires."""
    srv_a = _RecordingServer(status=200)
    srv_b = _RecordingServer(status=200)
    srv_c = _RecordingServer(status=200)

    async with AsyncExitStack() as stack:
        base_a = await stack.enter_async_context(_serve(srv_a))
        base_b = await stack.enter_async_context(_serve(srv_b))
        base_c = await stack.enter_async_context(_serve(srv_c))

        notifier = MultiplexNotifier(
            notifiers={
                "a": _make_webhook(base_a),
                "b": _make_webhook(base_b),
                "c": _make_webhook(base_c),
            },
            routing={"some_other_event": ["a"]},
        )
        await notifier.notify(_make_event("not_in_routing"))

    assert srv_a.hits == 1
    assert srv_b.hits == 1
    assert srv_c.hits == 1


@pytest.mark.asyncio
async def test_multiplex_empty_routing_fans_out_to_all_channels() -> None:
    """A MultiplexNotifier without any routing fans out to every channel."""
    srv_a = _RecordingServer(status=200)
    srv_b = _RecordingServer(status=200)

    async with AsyncExitStack() as stack:
        base_a = await stack.enter_async_context(_serve(srv_a))
        base_b = await stack.enter_async_context(_serve(srv_b))

        notifier = MultiplexNotifier(
            notifiers={
                "a": _make_webhook(base_a),
                "b": _make_webhook(base_b),
            },
        )
        await notifier.notify(_make_event("anything"))

    assert srv_a.hits == 1
    assert srv_b.hits == 1


@pytest.mark.asyncio
async def test_multiplex_skips_unknown_channel_in_routing() -> None:
    """Routing entries that reference an unknown channel are skipped, not fatal."""
    srv_a = _RecordingServer(status=200)

    async with AsyncExitStack() as stack:
        base_a = await stack.enter_async_context(_serve(srv_a))

        notifier = MultiplexNotifier(
            notifiers={"a": _make_webhook(base_a)},
            routing={"cart_success": ["a", "nonexistent"]},
        )
        # Should not raise even though "nonexistent" is in routing.
        await notifier.notify(_make_event("cart_success"))

    assert srv_a.hits == 1


# ---------------------------------------------------------------------------
# [io.multiplex-isolated-failures]
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiplex_failure_in_one_channel_does_not_cancel_others() -> None:
    """A 500-on-every-retry channel does not stop sibling channels' POSTs."""
    srv_bad = _RecordingServer(status=500)
    srv_good_a = _RecordingServer(status=200)
    srv_good_b = _RecordingServer(status=200)

    async with AsyncExitStack() as stack:
        base_bad = await stack.enter_async_context(_serve(srv_bad))
        base_a = await stack.enter_async_context(_serve(srv_good_a))
        base_b = await stack.enter_async_context(_serve(srv_good_b))

        notifier = MultiplexNotifier(
            notifiers={
                "bad": _make_webhook(base_bad),
                "good_a": _make_webhook(base_a),
                "good_b": _make_webhook(base_b),
            },
            routing={"cart_success": ["bad", "good_a", "good_b"]},
        )
        # Must NOT raise.
        await notifier.notify(_make_event("cart_success"))

    assert srv_bad.hits >= 1
    assert srv_good_a.hits == 1, f"good_a expected 1 hit, got {srv_good_a.hits}"
    assert srv_good_b.hits == 1, f"good_b expected 1 hit, got {srv_good_b.hits}"


@pytest.mark.asyncio
async def test_multiplex_failure_logged_per_channel(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each failed channel produces a log record naming it."""
    import logging

    srv_bad = _RecordingServer(status=500)
    srv_good = _RecordingServer(status=200)

    with caplog.at_level(logging.WARNING, logger="ticketmaster-bot"):
        async with AsyncExitStack() as stack:
            base_bad = await stack.enter_async_context(_serve(srv_bad))
            base_good = await stack.enter_async_context(_serve(srv_good))

            notifier = MultiplexNotifier(
                notifiers={
                    "bad_channel": _make_webhook(base_bad),
                    "good_channel": _make_webhook(base_good),
                },
                routing={"cart_success": ["bad_channel", "good_channel"]},
            )
            await notifier.notify(_make_event("cart_success"))

    assert srv_good.hits == 1
    assert any("bad_channel" in record.getMessage() for record in caplog.records), (
        "expected a log record mentioning the failing channel name"
    )


# ---------------------------------------------------------------------------
# from_config / NotifierRegistry wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiplex_from_config_constructs_via_registry() -> None:
    """``from_config`` uses NotifierRegistry to instantiate each channel."""
    srv_a = _RecordingServer(status=200)
    srv_b = _RecordingServer(status=200)

    async with AsyncExitStack() as stack:
        base_a = await stack.enter_async_context(_serve(srv_a))
        base_b = await stack.enter_async_context(_serve(srv_b))

        notifier = MultiplexNotifier.from_config(
            {
                "channels": ["web_a", "web_b"],
                "routing": {"cart_success": ["web_a", "web_b"]},
                "channel_configs": {
                    "web_a": {
                        "type": "webhook",
                        "url": f"{base_a}/hook",
                        "max_attempts": 1,
                        "timeout": 2.0,
                        "backoff_base": 0.01,
                    },
                    "web_b": {
                        "type": "webhook",
                        "url": f"{base_b}/hook",
                        "max_attempts": 1,
                        "timeout": 2.0,
                        "backoff_base": 0.01,
                    },
                },
            }
        )
        await notifier.notify(_make_event("cart_success"))

    assert srv_a.hits == 1
    assert srv_b.hits == 1


def test_multiplex_notifier_registered() -> None:
    """The multiplex notifier is registered under "multiplex"."""
    from src.registry import notifiers as notifier_registry

    cls = notifier_registry.get("multiplex")
    assert cls is MultiplexNotifier
