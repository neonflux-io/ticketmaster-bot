"""Tests for ``src.humanize.warmer.warm`` driven by real Chromium + FastAPI.

The warmer must open 2-3 unrelated Ticketmaster-style pages in the
caller's :class:`BrowserContext` and wait a randomised total of
``target_seconds`` before returning. The validation contract assertion
``dom.humanize-warmer-multi-page`` requires that a ``framenavigated``
recorder attached to the context observes at least two distinct
top-level URLs before ``warm`` returns.

A local FastAPI app on an ephemeral port serves three small HTML pages
so the tests never touch real Ticketmaster. The shared
``chromium_context`` fixture from ``tests/conftest.py`` provides a fresh
context per test.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from playwright.async_api import BrowserContext, Frame, Page

from src.humanize.warmer import (
    DEFAULT_PAGES,
    DEFAULT_TARGET_SECONDS,
    MAX_VISITS,
    MIN_VISITS,
    warm,
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class _WarmerServer:
    base: str
    urls: list[str]
    server: uvicorn.Server
    task: asyncio.Task[None]


def _build_app() -> FastAPI:
    app = FastAPI()

    def _make_page(label: str) -> str:
        # Keep the body tiny but distinct enough that mistakenly
        # re-visiting one page would not be hidden behind identical
        # markup.
        return (
            "<!doctype html><html><head><title>"
            f"warm-{label}"
            "</title></head><body><main><h1>"
            f"warmer page {label}"
            "</h1><p>fixture content</p></main></body></html>"
        )

    @app.get("/home", response_class=HTMLResponse)
    async def home() -> str:
        return _make_page("home")

    @app.get("/events", response_class=HTMLResponse)
    async def events() -> str:
        return _make_page("events")

    @app.get("/sports", response_class=HTMLResponse)
    async def sports() -> str:
        return _make_page("sports")

    return app


@pytest_asyncio.fixture
async def warmer_server() -> AsyncIterator[_WarmerServer]:
    """Spin up a FastAPI app on an ephemeral port serving 3 HTML pages."""
    port = _free_port()
    app = _build_app()
    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        lifespan="off",
        access_log=False,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    # Wait for the socket to be bound and ready.
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:  # pragma: no cover - defensive
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5.0)
        raise RuntimeError("uvicorn warmer server never started")

    base = f"http://127.0.0.1:{port}"
    urls = [f"{base}/home", f"{base}/events", f"{base}/sports"]
    try:
        yield _WarmerServer(base=base, urls=urls, server=server, task=task)
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:  # pragma: no cover - defensive
            task.cancel()


def _attach_framenavigated_recorder(
    context: BrowserContext,
) -> list[str]:
    """Record every top-level (main-frame) URL that any page in
    ``context`` navigates to. The list grows as the warmer drives pages.
    """
    seen: list[str] = []

    def on_page(page: Page) -> None:
        def on_framenav(frame: Frame) -> None:
            # Only top-level navigations count for the warmer assertion;
            # ignore any subframe / about:blank noise.
            if frame == page.main_frame and frame.url and frame.url != "about:blank":
                seen.append(frame.url)

        page.on("framenavigated", on_framenav)

    context.on("page", on_page)
    return seen


# ---------------------------------------------------------------------------
# [dom.humanize-warmer-multi-page]
# ---------------------------------------------------------------------------


async def test_warmer_visits_at_least_two_distinct_urls(
    chromium_context: BrowserContext, warmer_server: _WarmerServer
) -> None:
    """The framenavigated recorder must see ≥2 distinct top-level URLs."""
    recorded = _attach_framenavigated_recorder(chromium_context)

    visited = await warm(
        chromium_context,
        target_seconds=0.6,
        pages=warmer_server.urls,
        seed=11,
    )

    # The warmer also returns the URLs it loaded; both signals must agree.
    distinct_recorded = {u for u in recorded if u.startswith(warmer_server.base)}
    assert len(distinct_recorded) >= 2, f"expected ≥2 distinct top-level URLs, recorded={recorded}"
    assert len(set(visited)) >= 2, f"warmer returned only {visited}"


async def test_warmer_returns_visited_subset_of_pages(
    chromium_context: BrowserContext, warmer_server: _WarmerServer
) -> None:
    """Every URL warmer claims to have visited must be in the input list."""
    visited = await warm(
        chromium_context,
        target_seconds=0.5,
        pages=warmer_server.urls,
        seed=7,
    )
    assert visited, "warmer reported zero successful visits"
    for url in visited:
        assert url in warmer_server.urls


async def test_warmer_visit_count_between_min_and_max(
    chromium_context: BrowserContext, warmer_server: _WarmerServer
) -> None:
    """Visit count must satisfy 2 ≤ n ≤ 3 (defaults)."""
    visited = await warm(
        chromium_context,
        target_seconds=0.4,
        pages=warmer_server.urls,
        seed=3,
    )
    assert MIN_VISITS <= len(visited) <= MAX_VISITS


async def test_warmer_total_dwell_time_approximates_target(
    chromium_context: BrowserContext, warmer_server: _WarmerServer
) -> None:
    """Wall-clock duration should approximate ``target_seconds``.

    The warmer rescales per-visit dwell times so they sum exactly to
    ``target_seconds``; the actual run will exceed that by some Playwright
    overhead (page open / goto / close) but must not undershoot it.
    """
    target = 0.8
    started = time.perf_counter()
    await warm(
        chromium_context,
        target_seconds=target,
        pages=warmer_server.urls,
        seed=5,
    )
    elapsed = time.perf_counter() - started
    # Allow generous overhead bound: real Playwright open/close per page
    # adds tens of ms but is bounded.
    assert elapsed >= target * 0.9, f"warmer returned too fast: {elapsed:.3f}s"
    assert elapsed < target + 8.0, f"warmer took unexpectedly long: {elapsed:.3f}s"


async def test_warmer_closes_pages_it_opens(
    chromium_context: BrowserContext, warmer_server: _WarmerServer
) -> None:
    """No warmer-created pages may survive into the next test."""
    pre_count = len(chromium_context.pages)
    await warm(
        chromium_context,
        target_seconds=0.4,
        pages=warmer_server.urls,
        seed=9,
    )
    post_count = len(chromium_context.pages)
    assert post_count == pre_count, f"warmer leaked pages: pre={pre_count} post={post_count}"


async def test_warmer_seed_makes_choice_deterministic(
    chromium_context: BrowserContext, warmer_server: _WarmerServer
) -> None:
    """Two seeded runs must visit the same URLs in the same order."""
    visited_a = await warm(
        chromium_context,
        target_seconds=0.3,
        pages=warmer_server.urls,
        seed=42,
    )
    visited_b = await warm(
        chromium_context,
        target_seconds=0.3,
        pages=warmer_server.urls,
        seed=42,
    )
    assert visited_a == visited_b, f"seeded runs diverged: {visited_a} vs {visited_b}"


async def test_warmer_rejects_negative_target_seconds(
    chromium_context: BrowserContext, warmer_server: _WarmerServer
) -> None:
    with pytest.raises(ValueError):
        await warm(
            chromium_context,
            target_seconds=-1.0,
            pages=warmer_server.urls,
        )


async def test_warmer_rejects_empty_pages(
    chromium_context: BrowserContext,
) -> None:
    with pytest.raises(ValueError):
        await warm(chromium_context, target_seconds=0.1, pages=[])


async def test_warmer_handles_single_page_input(
    chromium_context: BrowserContext, warmer_server: _WarmerServer
) -> None:
    """When the input list has only one URL, the warmer visits just it."""
    visited = await warm(
        chromium_context,
        target_seconds=0.3,
        pages=[warmer_server.urls[0]],
        seed=2,
    )
    assert visited == [warmer_server.urls[0]]


# ---------------------------------------------------------------------------
# Module-level invariants.
# ---------------------------------------------------------------------------


def test_warmer_default_pages_are_ticketmaster() -> None:
    """Default URL pool must include Ticketmaster home + /events + /sports."""
    joined = " ".join(DEFAULT_PAGES)
    assert "ticketmaster.com" in joined
    assert any(p.rstrip("/").endswith("/events") for p in DEFAULT_PAGES)
    assert any(p.rstrip("/").endswith("/sports") for p in DEFAULT_PAGES)


def test_warmer_default_target_seconds_is_30() -> None:
    """Description pins ``target_seconds`` default to 30 seconds."""
    assert DEFAULT_TARGET_SECONDS == 30.0
