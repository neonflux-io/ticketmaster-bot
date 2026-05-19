"""Tests for navigator state detection + naive datetime handling."""
from __future__ import annotations

from datetime import datetime

import pytest

from src.bot.navigator import NavigationError, detect_state, wait_until_on_sale

from .conftest import FakeLocator, FakePage


@pytest.mark.asyncio
async def test_detect_state_queue_by_url():
    page = FakePage(url="https://queue.ticketmaster.com/abc")
    assert await detect_state(page) == "queue"


@pytest.mark.asyncio
async def test_detect_state_sold_out():
    sold = FakeLocator(text="Sold Out", visible=True)
    page = FakePage(
        locators={
            "main, [role='main']": FakeLocator(visible=True),
            "text=/sold out/i": sold,
        }
    )
    assert await detect_state(page) == "sold_out"


@pytest.mark.asyncio
async def test_detect_state_not_on_sale():
    not_on = FakeLocator(text="Sale Starts", visible=True)
    page = FakePage(
        locators={
            "main, [role='main']": FakeLocator(visible=True),
            "text=/sold out/i": FakeLocator(visible=False),
            "text=/no tickets are available/i": FakeLocator(visible=False),
            "[data-bdd='sold-out']": FakeLocator(visible=False),
            "text=/sale starts/i": not_on,
        }
    )
    assert await detect_state(page) == "not_on_sale"


@pytest.mark.asyncio
async def test_detect_state_tickets():
    page = FakePage(
        locators={
            "main, [role='main']": FakeLocator(visible=True),
            "text=/sold out/i": FakeLocator(visible=False),
            "text=/no tickets are available/i": FakeLocator(visible=False),
            "[data-bdd='sold-out']": FakeLocator(visible=False),
            "text=/sale starts/i": FakeLocator(visible=False),
            "text=/on sale .* at/i": FakeLocator(visible=False),
            "text=/presale/i": FakeLocator(visible=False),
            "[data-bdd='event-onsale-time']": FakeLocator(visible=False),
            "[data-bdd='quick-picks-list']": FakeLocator(visible=True),
        }
    )
    assert await detect_state(page) == "tickets"


@pytest.mark.asyncio
async def test_detect_state_unknown():
    page = FakePage(
        locators={
            "main, [role='main']": FakeLocator(visible=True),
        }
    )
    assert await detect_state(page) == "unknown"


@pytest.mark.asyncio
async def test_wait_until_on_sale_rejects_naive():
    naive = datetime(2026, 6, 1, 10, 0, 0)
    with pytest.raises(NavigationError, match="timezone-aware"):
        await wait_until_on_sale(FakePage(), naive)
