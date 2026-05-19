"""Tests for ``InteractiveSeatmapStrategy`` driven by real headless Chromium.

The fixture-backed tests load committed HTML files
(``seatmap_svg.html`` and ``seatmap_iframe.html``) and exercise the
real strategy against the real DOM. Click side-effects are observed by
reading ``window.__clicks`` (the JS click recorder) and the
``data-click-count`` attribute the recorder maintains on each
``<rect>``.
"""

from __future__ import annotations

import pytest

from src.strategies.interactive_seatmap import InteractiveSeatmapStrategy


async def _rect_click_count(page, section: str, row: str, seat: str) -> int:
    """Return the click counter recorded on a specific seat ``<rect>``.

    The counter is bumped by the in-page recorder script in
    ``seatmap_svg.html`` whenever a click bubbles out of a
    ``rect[data-section]`` element.
    """
    selector = f'rect[data-section="{section}"][data-row="{row}"][data-seat="{seat}"]'
    raw = await page.locator(selector).get_attribute("data-click-count")
    return int(raw or "0")


async def _total_recorded_clicks(page) -> int:
    return int(await page.evaluate("() => (window.__clicks || []).length"))


# --- fixture-backed assertions --------------------------------------------


async def test_interactive_seatmap_happy_path_clicks_matching_rect(chromium_context, fixture_url):
    """[dom.interactive-seatmap-happy]: section=100, row=A, seat=5 must
    click the matching ``<rect>`` exactly once and the JS recorder must
    report ``event.target`` as that rect.
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("seatmap_svg"))

    strategy = InteractiveSeatmapStrategy(section="100", row="A", seat="5")
    chosen = await strategy.pick(page)

    assert chosen is not None
    assert chosen.section == "100"
    assert chosen.row == "A"

    # The recorder pushed exactly one entry, for the target rect.
    clicks = await page.evaluate("() => window.__clicks")
    assert clicks == [{"section": "100", "row": "A", "seat": "5"}]

    # The target rect carries data-click-count="1"; no other rect was clicked.
    assert await _rect_click_count(page, "100", "A", "5") == 1
    # Spot-check three siblings.
    assert await _rect_click_count(page, "100", "A", "1") == 0
    assert await _rect_click_count(page, "100", "E", "5") == 0
    assert await _rect_click_count(page, "100", "C", "3") == 0


async def test_interactive_seatmap_reject_returns_none_no_clicks(chromium_context, fixture_url):
    """[dom.interactive-seatmap-reject]: no matching rect ⇒ ``pick``
    returns ``None`` and zero clicks are recorded.
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("seatmap_svg"))

    strategy = InteractiveSeatmapStrategy(section="999", row="Z", seat="99")
    chosen = await strategy.pick(page)

    assert chosen is None
    assert await _total_recorded_clicks(page) == 0


# --- iframe fallback ------------------------------------------------------


async def test_interactive_seatmap_falls_back_to_iframe(chromium_context, fixture_url):
    """When the seat map lives inside an iframe the strategy must use
    its ``frame_locator`` fallback to locate and click the matching
    rect inside the frame.
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("seatmap_iframe"))

    # Wait until the iframe's seat-map rects are reachable so the test
    # doesn't race the iframe load.
    frame = page.frame_locator("iframe[data-bdd='seatmap-frame']")
    await frame.locator('rect[data-section="100"][data-row="C"][data-seat="3"]').wait_for(
        state="attached", timeout=5000
    )

    strategy = InteractiveSeatmapStrategy(section="100", row="C", seat="3")
    chosen = await strategy.pick(page)

    assert chosen is not None
    assert chosen.section == "100"
    assert chosen.row == "C"

    # The recorder lives in the iframe document; we read it from there.
    frame_handle = next(f for f in page.frames if f.parent_frame is page.main_frame)
    clicks = await frame_handle.evaluate("() => window.__clicks")
    assert clicks == [{"section": "100", "row": "C", "seat": "3"}]


# --- targeted behavior tests ----------------------------------------------


async def test_interactive_seatmap_picks_exact_seat_not_a_neighbor(chromium_context, fixture_url):
    """With a 25-rect grid the strategy must click the *exact* requested
    rect and no neighbor."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("seatmap_svg"))

    strategy = InteractiveSeatmapStrategy(section="100", row="C", seat="3")
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert await _rect_click_count(page, "100", "C", "3") == 1
    # Both axis neighbors stayed silent.
    assert await _rect_click_count(page, "100", "C", "2") == 0
    assert await _rect_click_count(page, "100", "C", "4") == 0
    assert await _rect_click_count(page, "100", "B", "3") == 0
    assert await _rect_click_count(page, "100", "D", "3") == 0


async def test_interactive_seatmap_returns_none_when_only_one_attr_mismatches(
    chromium_context, fixture_url
):
    """Mismatching any one of (section, row, seat) must yield None."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("seatmap_svg"))

    # Wrong section only.
    assert await InteractiveSeatmapStrategy(section="200", row="A", seat="1").pick(page) is None
    # Wrong row only.
    assert await InteractiveSeatmapStrategy(section="100", row="Z", seat="1").pick(page) is None
    # Wrong seat only.
    assert await InteractiveSeatmapStrategy(section="100", row="A", seat="99").pick(page) is None
    assert await _total_recorded_clicks(page) == 0


async def test_interactive_seatmap_returns_candidate_with_input_attrs(
    chromium_context, fixture_url
):
    """The returned ``TicketCandidate`` exposes the requested section/row."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("seatmap_svg"))

    strategy = InteractiveSeatmapStrategy(section="100", row="B", seat="2")
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.section == "100"
    assert chosen.row == "B"
    # ``seat`` shows up in the description so logs/tests can identify it.
    assert "2" in chosen.description
    # No price is known from a seat map, so price stays None.
    assert chosen.price is None


def test_interactive_seatmap_rejects_empty_inputs():
    """Empty ``section``/``row``/``seat`` is a config mistake — surface it."""
    with pytest.raises(ValueError):
        InteractiveSeatmapStrategy(section="", row="A", seat="1")
    with pytest.raises(ValueError):
        InteractiveSeatmapStrategy(section="100", row="", seat="1")
    with pytest.raises(ValueError):
        InteractiveSeatmapStrategy(section="100", row="A", seat="")


# --- factory registration -------------------------------------------------


def test_interactive_seatmap_registered_in_strategy_registry():
    """The strategy must be discoverable by name via the singleton
    ``StrategyRegistry`` so external code can build it generically."""
    # Importing the factory side-effect registers every shipped strategy.
    from src.registry import strategies as strategy_registry
    from src.strategies import factory  # noqa: F401  (import for side effects)

    assert "interactive_seatmap" in strategy_registry.registry
    assert strategy_registry.registry.get("interactive_seatmap") is InteractiveSeatmapStrategy
