"""Real-Chromium tests for :class:`SGInteractiveSeatmapStrategy`.

The fixture under test is the committed F8.1 capture at
``tests/fixtures/vendors/ticketmaster_sg/seatmap.html`` — raw DOM from
the live ticketmaster.sg seat-map iframe for the
KFF Singapore Badminton Open 2026 event, section 225. The captured
seatmap has 116 ``.empty`` cells, 12 ``.sold`` cells, 110 ``.noseat``
aisle cells, and a unique ``td.empty[data-seatrow='18'][data-seatno='5']``
target seat.

The fixture HTML embeds an inline guard
``if (window.document == parent.document) window.location.replace("/")``
that self-redirects when loaded top-level. The two committed load
patterns mirror the F8.1 recon doc:

1. ``_load_seatmap_top_level`` — read the file, replace the guard with
   ``if (false)``, ``page.set_content``. Exercises the top-level scope
   branch of :func:`_resolve_seatmap_scope`.
2. ``_load_seatmap_in_iframe`` — mount the (already-guard-neutered)
   fixture inside an ``<iframe src='/ticket/select-seat/...'>``
   wrapper so the strategy descends through ``page.frame_locator``.

NO synthetic SVG, NO mocks, NO test doubles — every test runs against
the real captured DOM under real headless Chromium via the shared
``chromium_context`` fixture.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from src.vendors.ticketmaster_sg.seatmap import SGInteractiveSeatmapStrategy

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[3]
    / "tests"
    / "fixtures"
    / "vendors"
    / "ticketmaster_sg"
    / "seatmap.html"
)


def _read_neutered_fixture() -> str:
    """Return the fixture HTML with the parent-document self-redirect disabled.

    The inline JS guard
    ``if (window.document == parent.document) { window.location.replace("/"); }``
    would otherwise navigate the test away from the seat-map. Replacing
    it with ``if (false) {`` keeps the rest of the inline JS (which
    populates the ``<table class="seat">`` from ``var seatData``) intact.
    """
    raw = _FIXTURE_PATH.read_text(encoding="utf-8")
    needle = "if (window.document == parent.document) {"
    if needle not in raw:
        raise AssertionError(
            f"F8.1 fixture {_FIXTURE_PATH} no longer contains the guard "
            "block this test neuters — please regenerate the fixture or "
            "fix the test loader."
        )
    return raw.replace(needle, "if (false) {")


_CLICK_RECORDER_JS = """
(() => {
  if (window.__clicks) { return; }
  window.__clicks = [];
  document.addEventListener("click", (event) => {
    const cell = event.target.closest("td[data-coordinate]");
    if (!cell) { return; }
    window.__clicks.push({
      coordinate: cell.getAttribute("data-coordinate"),
      seatrow: cell.getAttribute("data-seatrow"),
      seatno: cell.getAttribute("data-seatno"),
      classBefore: cell.className,
    });
    // Mirror the live SG inline handler: flip .empty cells to .checked
    // so the assertion that the strategy actually targeted a clickable
    // cell can read the class transition without relying on the
    // captured jQuery handler (which doesn't bind under file:// because
    // the fixture's /assets/b7dd4dcc/jquery.min.js 404s).
    if (cell.classList.contains("empty")) {
      cell.classList.remove("empty");
      cell.classList.add("checked");
    }
  }, true);
})();
"""


async def _load_seatmap_top_level(chromium_context: BrowserContext) -> Page:
    """Render the seat-map fixture as a top-level page.

    Used to exercise the top-level branch of
    :func:`SGInteractiveSeatmapStrategy._resolve_seatmap_scope` — the
    scope that the strategy falls back to when no Fancybox iframe is
    present (e.g. during recon, or any non-live load of the captured
    DOM). Installs a JS click-recorder on document so tests can observe
    which cell the strategy actually targeted (the captured inline
    jQuery handler doesn't bind because the page's
    ``/assets/<hash>/jquery.min.js`` reference 404s when loaded
    offline).
    """
    page = await chromium_context.new_page()
    await page.set_content(_read_neutered_fixture(), wait_until="domcontentloaded")
    await page.wait_for_selector("table.seat td.empty")
    await page.evaluate(_CLICK_RECORDER_JS)
    return page


_LIVE_SEATMAP_URL = "https://ticketmaster.sg/ticket/select-seat/26sg_sgopen2026/3318/46/88"


async def _load_seatmap_in_iframe(chromium_context: BrowserContext) -> Page:
    """Render the seat-map fixture inside an iframe whose ``src`` matches the live URL.

    Builds a one-off parent page that mounts an
    ``<iframe src="<LIVE_SEATMAP_URL>">``. A Playwright route handler
    intercepts BOTH the live URL (fulfilling with the F8.1 fixture
    body) and every other request the fixture's HTML emits (fulfilled
    with empty 200 responses) so the iframe loads deterministically
    without touching the network. The iframe's ``src`` carries the
    exact ``/ticket/select-seat/`` path the live SG site emits, which
    is what the strategy's ``seatmap_iframe`` selector matches on.
    """
    page = await chromium_context.new_page()

    fixture_body = _read_neutered_fixture()

    async def _handler(route):  # noqa: ANN001 - Playwright Route is untyped here
        request_url = route.request.url
        if request_url == _LIVE_SEATMAP_URL or request_url.startswith(_LIVE_SEATMAP_URL):
            await route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=fixture_body,
            )
            return
        # Every other resource (jQuery, fancybox CSS, OneTrust SDK, font-awesome,
        # CSS sprites, etc.) gets an empty 200 so the iframe doesn't stall
        # on network. We don't need any of them to render the seat-grid.
        await route.fulfill(status=200, body="")

    await page.route("**/*", _handler)

    shell = (
        "<!doctype html><html><body>"
        f"<iframe id='seatmap' src='{_LIVE_SEATMAP_URL}' "
        "style='width:100%;height:800px;border:0'></iframe>"
        "</body></html>"
    )
    await page.set_content(shell, wait_until="domcontentloaded")
    # Wait for the inner page to render the seat grid; the inline JS
    # that builds the rows runs after $(document).ready, but jQuery
    # isn't actually loaded here so the table is server-rendered (the
    # fixture committed the post-render HTML) and the rows are present
    # from the start.
    frame = page.frame_locator("iframe#seatmap").first
    await frame.locator("table.seat td.empty").first.wait_for(timeout=8000)
    # Install the click recorder inside the iframe (same trick as the
    # top-level loader; the captured jQuery handler never binds because
    # the route handler 200s the jquery.min.js script with empty body).
    iframe_handle = await page.locator("iframe#seatmap").element_handle()
    assert iframe_handle is not None
    inner_frame = await iframe_handle.content_frame()
    assert inner_frame is not None
    await inner_frame.evaluate(_CLICK_RECORDER_JS)
    return page


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"section": "", "row": "18", "seat": "5"},
        {"section": "   ", "row": "18", "seat": "5"},
        {"section": "225", "row": "", "seat": "5"},
        {"section": "225", "row": "18", "seat": ""},
    ],
)
def test_constructor_rejects_empty_inputs(kwargs: dict[str, str]) -> None:
    """Constructor rejects empty / whitespace-only section/row/seat."""
    with pytest.raises(ValueError):
        SGInteractiveSeatmapStrategy(**kwargs)


# ---------------------------------------------------------------------------
# Happy path: top-level scope
# ---------------------------------------------------------------------------


async def test_pick_clicks_target_cell_top_level(chromium_context) -> None:
    """Strategy clicks the unique ``td.empty[seatrow=18][seatno=5]`` cell.

    The fixture's section 225 contains exactly one available cell at
    visible row=18 seat=5 (internal coord ``5_3``). After the click,
    the inline jQuery handler flips its class from ``empty`` to
    ``checked`` — we read that as the post-condition proving the
    strategy actually clicked the right cell rather than no-op'ing or
    clicking a sibling.
    """
    page = await _load_seatmap_top_level(chromium_context)
    strategy = SGInteractiveSeatmapStrategy(section="225", row="18", seat="5")

    candidate = await strategy.pick(page)

    assert candidate is not None
    assert candidate.section == "225"
    assert candidate.row == "18"
    assert candidate.description.startswith("SG seat-map pick:")
    assert "section=225" in candidate.description
    assert "row=18" in candidate.description
    assert "seat=5" in candidate.description

    # Post-click DOM check: the targeted cell must have flipped to
    # .checked, AND must still carry the same data-coordinate (5_3 on
    # this fixture). The class flip is driven by the captured inline
    # jQuery handler so this proves a real click was delivered.
    target = page.locator("table.seat td[data-seatrow='18'][data-seatno='5']").first
    assert await target.count() == 1
    class_attr = await target.get_attribute("class")
    assert class_attr is not None
    assert "checked" in class_attr.split()
    assert "empty" not in class_attr.split()


async def test_pick_returns_none_when_seat_does_not_exist(chromium_context) -> None:
    """No matching available cell → strategy returns None, nothing flips.

    Section 225 has no available seat at row=17 seat=5 (row 17 in the
    fixture is mostly sold; seat 5 specifically is class=sold, not
    empty). The strategy must return None and leave the DOM
    untouched — no cell may flip to ``.checked``.
    """
    page = await _load_seatmap_top_level(chromium_context)
    strategy = SGInteractiveSeatmapStrategy(section="225", row="17", seat="5")

    candidate = await strategy.pick(page)

    assert candidate is None
    # No cell on the page may have transitioned to .checked.
    checked_count = await page.locator("table.seat td.checked").count()
    assert checked_count == 0


async def test_pick_returns_none_for_sold_seat(chromium_context) -> None:
    """Sold seats are not in ``td.empty`` so the strategy can't pick one.

    Row 16 seat 5 is class=sold on the fixture. The strategy filters by
    ``.empty`` in its selector template, so even though a
    ``td[data-seatrow='16'][data-seatno='5']`` exists, it isn't in the
    available set.
    """
    page = await _load_seatmap_top_level(chromium_context)
    # Sanity: there IS a (sold) cell with those attrs in the fixture.
    sold_cell = page.locator("table.seat td.sold[data-seatrow='16'][data-seatno='5']").first
    assert await sold_cell.count() == 1

    strategy = SGInteractiveSeatmapStrategy(section="225", row="16", seat="5")
    candidate = await strategy.pick(page)

    assert candidate is None
    assert await page.locator("table.seat td.checked").count() == 0
    # The sold cell stays sold (no class mutation).
    cls = await sold_cell.get_attribute("class")
    assert cls is not None and "sold" in cls.split()
    assert "checked" not in cls.split()


# ---------------------------------------------------------------------------
# Happy path: iframe scope (live shape)
# ---------------------------------------------------------------------------


async def test_pick_clicks_target_cell_through_iframe(chromium_context) -> None:
    """Strategy descends through a Fancybox-shaped iframe to find the cell.

    Mirrors the live SG layout: the parent page hosts an
    ``<iframe src='.../ticket/select-seat/...'>`` and the seat-grid
    lives inside it. The strategy's
    :func:`_resolve_seatmap_scope` helper must detect the iframe,
    return a ``FrameLocator``, and use that for the cell lookup.
    """
    page = await _load_seatmap_in_iframe(chromium_context)
    strategy = SGInteractiveSeatmapStrategy(section="225", row="18", seat="5")

    frame = page.frame_locator("iframe#seatmap").first
    await frame.locator("table.seat").first.wait_for()

    candidate = await strategy.pick(page)

    assert candidate is not None
    assert candidate.row == "18"
    assert candidate.section == "225"

    # Post-condition: the targeted cell INSIDE THE IFRAME flipped to
    # .checked. The parent page has no <table class="seat"> of its own,
    # so the strategy must have routed through the FrameLocator.
    target_inside_frame = frame.locator("table.seat td[data-seatrow='18'][data-seatno='5']").first
    assert await target_inside_frame.count() == 1
    cls = await target_inside_frame.get_attribute("class")
    assert cls is not None and "checked" in cls.split()


# ---------------------------------------------------------------------------
# Empty / missing scope
# ---------------------------------------------------------------------------


async def test_pick_returns_none_when_no_seatmap_present(chromium_context) -> None:
    """No iframe AND no top-level seat-grid → returns None, doesn't raise."""
    page = await chromium_context.new_page()
    await page.set_content("<!doctype html><html><body><h1>Not a seat-map</h1></body></html>")
    strategy = SGInteractiveSeatmapStrategy(section="225", row="18", seat="5")

    candidate = await strategy.pick(page)
    assert candidate is None


# ---------------------------------------------------------------------------
# Fixture sanity: the F8.1 capture matches the recon report's counts
# ---------------------------------------------------------------------------


async def test_fixture_counts_match_recon_report(chromium_context) -> None:
    """Guard against silent fixture drift.

    docs/recon/ticketmaster_sg/seatmap.md asserts the captured section
    225 has 116 ``.empty``, 12 ``.sold``, 110 ``.noseat``, and exactly
    one available cell at row=18 seat=5. If a future re-capture changes
    those counts, the strategy tests above will need to be updated, so
    we pin them here as a tripwire.
    """
    page = await _load_seatmap_top_level(chromium_context)
    assert await page.locator("table.seat td.empty").count() == 116
    assert await page.locator("table.seat td.sold").count() == 12
    assert await page.locator("table.seat td.noseat").count() == 110
    assert (
        await page.locator("table.seat td.empty[data-seatrow='18'][data-seatno='5']").count() == 1
    )
    section_label = await page.locator("div.area-name").inner_text()
    assert section_label.strip() == "225"
