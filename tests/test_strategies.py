"""Tests for ticket-selection strategy modules."""
from __future__ import annotations

import pytest

from src.strategies.base import (
    SelectionStrategy,
    TicketCandidate,
    _extract_price,
    _extract_row,
    _extract_sections,
)
from src.strategies.best_available import BestAvailableStrategy
from src.strategies.cheapest import CheapestStrategy
from src.strategies.section_target import SectionTargetStrategy
from src.utils.config_loader import SectionTargetConfig

from .conftest import FakeLocator, FakePage

# --- regex helpers --------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Section 108  Row C  $89.50", 89.50),
        ("$1,234.56 each", 1234.56),
        ("Total $1,234", 1234.0),
        ("$99 each", 99.0),
        ("no price here", None),
    ],
)
def test_extract_price(text, expected):
    assert _extract_price(text) == expected


def test_extract_sections_multiple():
    assert _extract_sections("Section 108 - Sec 109") == ["108", "109"]


def test_extract_sections_single_uppercased():
    assert _extract_sections("sec a12") == ["A12"]


def test_extract_row():
    assert _extract_row("Section 100 Row B") == "B"
    assert _extract_row("Sec 100") is None


# --- helper: build a fake page with a quick-picks list -------------------


def _page_with_candidates(rows: list[FakeLocator]) -> FakePage:
    container = FakeLocator(children=rows)
    return FakePage(
        locators={
            "[data-bdd='quick-pick-row'], [data-bdd='ticket-list'] li, .quick-picks li": container,
        }
    )


# --- CheapestStrategy ----------------------------------------------------


@pytest.mark.asyncio
async def test_cheapest_picks_lowest_under_max_price():
    rows = [
        FakeLocator(text="Section 100 Row A  $200.00"),
        FakeLocator(text="Section 200 Row C  $100.00"),
        FakeLocator(text="Section 300 Row D  $1,500.00"),
    ]
    page = _page_with_candidates(rows)
    strategy = CheapestStrategy(max_price=300.0)
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 100.0
    assert rows[1].click_count == 1


@pytest.mark.asyncio
async def test_cheapest_rejects_when_over_max_price():
    rows = [FakeLocator(text="Section 100 $500.00")]
    page = _page_with_candidates(rows)
    strategy = CheapestStrategy(max_price=100.0)
    assert await strategy.pick(page) is None
    assert rows[0].click_count == 0


@pytest.mark.asyncio
async def test_cheapest_handles_four_digit_price():
    rows = [
        FakeLocator(text="Section 1 $999.00"),
        FakeLocator(text="Section 2 $1,200.00"),
    ]
    page = _page_with_candidates(rows)
    strategy = CheapestStrategy(max_price=None)
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 999.0


# --- BestAvailableStrategy -----------------------------------------------


@pytest.mark.asyncio
async def test_best_available_uses_dedicated_button_with_price_in_summary():
    page = FakePage(
        locators={
            "button:has-text('Best Available'), [data-bdd='best-available']": FakeLocator(
                text="Best Available"
            ),
            "[data-bdd='order-summary']": FakeLocator(text="Subtotal $250.00"),
        }
    )
    strategy = BestAvailableStrategy(max_price=500.0)
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 250.0


@pytest.mark.asyncio
async def test_best_available_aborts_when_over_max_price():
    page = FakePage(
        locators={
            "button:has-text('Best Available'), [data-bdd='best-available']": FakeLocator(
                text="Best Available"
            ),
            "[data-bdd='order-summary']": FakeLocator(text="Subtotal $999.00"),
        }
    )
    strategy = BestAvailableStrategy(max_price=200.0)
    assert await strategy.pick(page) is None


@pytest.mark.asyncio
async def test_best_available_aborts_when_price_unknown_and_max_set():
    page = FakePage(
        locators={
            "button:has-text('Best Available'), [data-bdd='best-available']": FakeLocator(
                text="Best Available"
            ),
        }
    )
    strategy = BestAvailableStrategy(max_price=100.0)
    assert await strategy.pick(page) is None


@pytest.mark.asyncio
async def test_best_available_falls_back_to_first_row():
    page = FakePage(
        locators={
            "button:has-text('Best Available'), [data-bdd='best-available']": FakeLocator(
                visible=False
            ),
            "[data-bdd='quick-pick-row'], [data-bdd='ticket-list'] li, .quick-picks li": FakeLocator(
                children=[
                    FakeLocator(text="Section 1 $50.00"),
                    FakeLocator(text="Section 2 $60.00"),
                ]
            ),
        }
    )
    strategy = BestAvailableStrategy(max_price=None)
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 50.0


# --- SectionTargetStrategy ------------------------------------------------


@pytest.mark.asyncio
async def test_section_target_matches_section_and_row():
    rows = [
        FakeLocator(text="Section 100 Row Z $80.00"),
        FakeLocator(text="Section 100 Row B $90.00"),
        FakeLocator(text="Section 200 Row A $40.00"),
    ]
    page = _page_with_candidates(rows)
    target = SectionTargetConfig(section="100", row_range=["A", "M"], price_level_id=None)
    strategy = SectionTargetStrategy(target, max_price=None)
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.section == "100"
    assert chosen.row == "B"


@pytest.mark.asyncio
async def test_section_target_filters_by_price_level_id():
    rows = [
        FakeLocator(text="Section 100 $80.00", attrs={"data-price-level-id": "PL1"}),
        FakeLocator(text="Section 100 $90.00", attrs={"data-price-level-id": "PL2"}),
    ]
    page = _page_with_candidates(rows)
    target = SectionTargetConfig(section=None, row_range=None, price_level_id="PL2")
    strategy = SectionTargetStrategy(target, max_price=None)
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 90.0


@pytest.mark.asyncio
async def test_section_target_no_match_returns_none():
    rows = [FakeLocator(text="Section 100 $80.00")]
    page = _page_with_candidates(rows)
    target = SectionTargetConfig(section="999", row_range=None, price_level_id=None)
    assert await SectionTargetStrategy(target, max_price=None).pick(page) is None


# --- click retry ----------------------------------------------------------


@pytest.mark.asyncio
async def test_click_candidate_retries_on_failure():
    class FlakyLocator(FakeLocator):
        def __init__(self, text: str) -> None:
            super().__init__(text=text)
            self._attempts = 0

        async def click(self, timeout=None):  # noqa: ARG002
            self._attempts += 1
            if self._attempts == 1:
                raise RuntimeError("stale")
            self.click_count += 1

    loc = FlakyLocator("Section 100 $50.00")
    page = _page_with_candidates([loc])
    strategy = CheapestStrategy(max_price=None)
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert loc.click_count == 1


# --- abstract surface check (ensures interface stays stable) -------------


def test_selection_strategy_is_abstract():
    with pytest.raises(TypeError):
        SelectionStrategy()  # type: ignore[abstract]


def test_ticket_candidate_repr_ok():
    tc = TicketCandidate(
        locator=FakeLocator(),
        price=50.0,
        section="100",
        row="A",
        description="test",
    )
    assert "price=50.0" in repr(tc)
