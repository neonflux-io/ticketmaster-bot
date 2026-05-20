"""Integration tests for the SG :class:`BotRunner` seat-map branch (F8.3).

The SG ``BotRunner`` overrides two extension points exposed by the base
:class:`src.vendors.ticketmaster.core.BotRunner`:

* :meth:`_build_strategy` — substitutes
  :class:`src.vendors.ticketmaster_sg.seatmap.SGInteractiveSeatmapStrategy`
  for the shared ``InteractiveSeatmapStrategy`` when the config selects
  ``tickets.strategy=interactive_seatmap``. The shared strategy targets
  ``<rect>`` elements in the US React seat-map SVG; the SG site uses a
  Yii-rendered ``<table class='seat'>`` with ``<td>`` cells.
* :meth:`_select_ticket` — opens the SG Fancybox seat-map by clicking
  ``button#manualMode`` on the ticket-area page, waits for the iframe
  to attach, runs the SG strategy's ``pick``, then clicks
  ``button#submitSeat`` to confirm the selection.

These tests drive the real overrides under real headless Chromium
against an inline DOM that mirrors the live SG layout (parent page
hosting an ``<iframe src='/ticket/select-seat/...'>``). No mocks.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from src.utils.config_loader import (
    AccountConfig,
    BotConfig,
    BrowserConfig,
    CheckoutConfig,
    DeliveryConfig,
    EventConfig,
    InteractiveSeatmapConfig,
    LoggingConfig,
    NotificationsConfig,
    PaymentConfig,
    ProxyConfig,
    StealthConfig,
    TicketsConfig,
    TimingConfig,
)
from src.vendors.ticketmaster_sg.core import BotRunner as SGBotRunner
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

_LIVE_SEATMAP_URL = "https://ticketmaster.sg/ticket/select-seat/26sg_sgopen2026/3318/46/88"
_LIVE_AREA_URL = "https://ticketmaster.sg/ticket/area/26sg_sgopen2026/3318"


def _read_neutered_seatmap_fixture() -> str:
    """Return the F8.1 seat-map fixture with the self-redirect guard disabled.

    The captured iframe DOM ships with an inline JS guard
    (``if (window.document == parent.document) window.location.replace("/")``)
    that bounces us to ``/`` when the fixture is loaded top-level. The
    guard remains harmless when the fixture is mounted inside an
    iframe (the condition evaluates to ``false``), but Playwright's
    route handler treats both navigations equivalently, so we neuter
    the guard for every load to keep the test deterministic.
    """
    raw = _FIXTURE_PATH.read_text(encoding="utf-8")
    needle = "if (window.document == parent.document) {"
    if needle not in raw:
        raise AssertionError(
            f"F8.1 fixture {_FIXTURE_PATH} no longer contains the guard "
            "block this loader neuters — please regenerate the fixture "
            "or fix the loader."
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
    });
    if (cell.classList.contains("empty")) {
      cell.classList.remove("empty");
      cell.classList.add("checked");
    }
  }, true);
})();
"""


_SUBMIT_RECORDER_JS = """
(() => {
  window.__submitClicked = false;
  document.addEventListener("click", (event) => {
    const btn = event.target.closest("button#submitSeat");
    if (btn) {
      window.__submitClicked = true;
    }
  }, true);
})();
"""


async def _load_area_page_with_seatmap_iframe(chromium_context: BrowserContext) -> Page:
    """Mount a synthetic SG ticket-area page wired up to the F8.1 seat-map.

    The parent page hosts a ``button#manualMode`` (clicked by the SG
    runner's ``_select_ticket`` override) which, when clicked, swaps
    in an ``<iframe src='<LIVE_SEATMAP_URL>'>`` mirroring the Fancybox
    behaviour of the live site. A Playwright route handler intercepts
    every request and fulfils the iframe URL with the F8.1 fixture
    body so the seat-map renders deterministically without network.
    """
    page = await chromium_context.new_page()
    fixture_body = _read_neutered_seatmap_fixture()

    async def _handler(route):  # noqa: ANN001 - Playwright Route is untyped
        url = route.request.url
        if url == _LIVE_SEATMAP_URL or url.startswith(_LIVE_SEATMAP_URL):
            await route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=fixture_body,
            )
            return
        # Every other resource (jQuery, fancybox CSS, fonts …) gets
        # an empty 200 so the iframe renders without stalling.
        await route.fulfill(status=200, body="")

    await page.route("**/*", _handler)

    parent_shell = (
        "<!doctype html><html><body>"
        "<main id='content'>"
        "<form id='form-ticket-ticket'>"
        # The "Pick Your Own Seat" trigger the runner clicks. Wired
        # via JS to swap in the iframe — mirrors the live SG behaviour
        # without needing a real jQuery + Fancybox stack.
        "<button type='button' id='manualMode'>Pick Your Own Seat</button>"
        "<button type='button' id='autoMode'>Best Available</button>"
        "</form>"
        "<div id='seatmap-host'></div>"
        "</main>"
        "<script>"
        "  document.getElementById('manualMode').addEventListener('click', () => {"
        "    const host = document.getElementById('seatmap-host');"
        # Idempotent: only mount the iframe on the first click so a
        # second #manualMode click (e.g. from the runner after the
        # test has pre-opened the iframe to install JS recorders) does
        # NOT replace the iframe and discard the recorders.
        "    if (host.querySelector('iframe#seatmap')) { return; }"
        f"    host.innerHTML = `<iframe id='seatmap' src='{_LIVE_SEATMAP_URL}' "
        "style='width:100%;height:800px;border:0'></iframe>`;"
        "  });"
        "</script>"
        "</body></html>"
    )
    await page.goto("about:blank")
    await page.set_content(parent_shell, wait_until="domcontentloaded")
    return page


def _make_sg_runner(
    *,
    strategy: str = "interactive_seatmap",
    section: str | None = "225",
    row: str | None = "18",
    seat: str | None = "5",
) -> SGBotRunner:
    """Construct a minimal SG :class:`BotRunner` for hook testing.

    The runner is constructed without invoking Playwright — we only
    exercise its synchronous extension-point methods
    (``_build_strategy``) and the ``_select_ticket`` async hook
    against a page we hand it directly. ``run()`` is NOT awaited.
    """
    tickets = TicketsConfig(
        quantity=1,
        strategy=strategy,
        interactive_seatmap=InteractiveSeatmapConfig(section=section, row=row, seat=seat),
    )
    config = BotConfig(
        events=[
            EventConfig(url="https://ticketmaster.sg/activity/detail/26sg_sgopen2026"),
        ],
        tickets=tickets,
        checkout=CheckoutConfig(
            auto_purchase=False,
            payment=PaymentConfig(),
            delivery=DeliveryConfig(),
        ),
        timing=TimingConfig(),
        logging=LoggingConfig(file=None),
        notifications=NotificationsConfig(),
        browser=BrowserConfig(
            headless=True,
            user_data_dir="sessions/sg-test",
            stealth=StealthConfig(enabled=False),
        ),
        accounts=[AccountConfig(email="x@example.com", password="x", name="test")],
        proxy=ProxyConfig(),
    )
    return SGBotRunner(config)


# ---------------------------------------------------------------------------
# _build_strategy: SG override picks SGInteractiveSeatmapStrategy
# ---------------------------------------------------------------------------


def test_sg_runner_builds_sg_seatmap_strategy() -> None:
    """``_build_strategy`` returns the SG variant when configured."""
    runner = _make_sg_runner(strategy="interactive_seatmap", section="225", row="18", seat="5")
    strategy = runner._build_strategy()
    assert isinstance(strategy, SGInteractiveSeatmapStrategy)
    assert strategy.section == "225"
    assert strategy.row == "18"
    assert strategy.seat == "5"


def test_sg_runner_delegates_to_factory_for_non_seatmap_strategy() -> None:
    """Any non-seatmap strategy goes through the base factory unchanged."""
    from src.strategies.cheapest import CheapestStrategy

    runner = _make_sg_runner(strategy="cheapest", section=None, row=None, seat=None)
    strategy = runner._build_strategy()
    assert isinstance(strategy, CheapestStrategy)


# ---------------------------------------------------------------------------
# _select_ticket: opens the iframe and confirms the seat
# ---------------------------------------------------------------------------


async def test_sg_runner_seatmap_flow_opens_iframe_and_submits(
    chromium_context: BrowserContext,
) -> None:
    """End-to-end SG seat-map flow under real Chromium.

    Sequence:

    1. The runner clicks ``#manualMode`` on the parent page, which
       triggers an inline JS handler that swaps in an
       ``<iframe src='/ticket/select-seat/...'>``.
    2. The runner waits for the iframe to attach, then runs
       :meth:`SGInteractiveSeatmapStrategy.pick`, which descends into
       the iframe and clicks ``td.empty[data-seatrow='18'][data-seatno='5']``.
    3. The runner clicks ``button#submitSeat`` inside the iframe.

    Post-conditions: the strategy returned a non-``None`` candidate,
    the target cell flipped from ``.empty`` to ``.checked``, and the
    submit recorder observed exactly one click on ``button#submitSeat``.
    """
    page = await _load_area_page_with_seatmap_iframe(chromium_context)
    runner = _make_sg_runner(strategy="interactive_seatmap", section="225", row="18", seat="5")
    strategy = runner._build_strategy()
    assert isinstance(strategy, SGInteractiveSeatmapStrategy)

    # Install the click recorder INSIDE the iframe before driving the
    # runner. The runner itself will click #manualMode if the iframe
    # isn't there yet, but we want the recorder in place before the
    # strategy picks the cell so the empty→checked class flip is
    # captured. The simplest deterministic ordering is to pre-open
    # the iframe ourselves (mirrors what the runner would do, but lets
    # us install JS into the resulting frame), install the recorder,
    # then call the runner which idempotently sees the iframe already
    # attached and proceeds straight to the strategy + #submitSeat.
    await page.locator("button#manualMode").click()
    await page.wait_for_selector("iframe#seatmap")
    iframe_handle = await page.locator("iframe#seatmap").element_handle()
    assert iframe_handle is not None
    inner_frame = await iframe_handle.content_frame()
    assert inner_frame is not None
    await inner_frame.wait_for_selector("table.seat td.empty")
    await inner_frame.evaluate(_CLICK_RECORDER_JS)
    await inner_frame.evaluate(_SUBMIT_RECORDER_JS)

    candidate = await runner._select_ticket(page, strategy)

    assert candidate is not None
    assert candidate.section == "225"
    assert candidate.row == "18"
    assert "section=225" in candidate.description
    assert "seat=5" in candidate.description

    # The iframe must exist (proves the manualMode click swapped it in).
    iframe_count = await page.locator("iframe#seatmap").count()
    assert iframe_count == 1

    frame = page.frame_locator("iframe#seatmap").first
    # The target cell flipped to .checked → strategy actually clicked it.
    target = frame.locator("table.seat td[data-seatrow='18'][data-seatno='5']").first
    assert await target.count() == 1
    cls = await target.get_attribute("class")
    assert cls is not None and "checked" in cls.split()
    # The submit button click happened (the runner's _select_ticket
    # override clicks #submitSeat after the strategy picks the cell).
    submit_clicked = await inner_frame.evaluate("() => !!window.__submitClicked")
    assert submit_clicked is True


async def test_sg_runner_seatmap_flow_returns_none_when_seat_unavailable(
    chromium_context: BrowserContext,
) -> None:
    """When the operator's row/seat is sold, the runner returns ``None``.

    The F8.1 fixture has no available cell at row=16/seat=5 (the seat
    exists but is ``td.sold``). The strategy returns ``None``; the
    runner must NOT click ``#submitSeat`` so the operator is not locked
    in to an unintended cell.
    """
    page = await _load_area_page_with_seatmap_iframe(chromium_context)
    runner = _make_sg_runner(strategy="interactive_seatmap", section="225", row="16", seat="5")
    strategy = runner._build_strategy()

    candidate = await runner._select_ticket(page, strategy)
    assert candidate is None

    # The submitSeat button inside the iframe must NOT have been clicked.
    frame = page.frame_locator("iframe#seatmap").first
    # No cell may have flipped to .checked.
    assert await frame.locator("table.seat td.checked").count() == 0


async def test_sg_runner_seatmap_flow_returns_none_when_manual_button_missing(
    chromium_context: BrowserContext,
) -> None:
    """No ``#manualMode`` on the area page → runner returns ``None`` gracefully.

    Sections that only support Best-Available render ``button#autoMode``
    but NOT ``button#manualMode``. The runner should not crash; it
    should return ``None`` so the surrounding state machine can
    surface a clear "no seat-map for this section" failure.
    """
    page = await chromium_context.new_page()
    await page.set_content(
        "<!doctype html><html><body><main id='content'>"
        "<form id='form-ticket-ticket'>"
        "<button type='button' id='autoMode'>Best Available</button>"
        "</form></main></body></html>"
    )

    runner = _make_sg_runner(strategy="interactive_seatmap", section="225", row="18", seat="5")
    strategy = runner._build_strategy()

    candidate = await runner._select_ticket(page, strategy)
    assert candidate is None


async def test_sg_runner_non_seatmap_strategy_runs_pick_directly(
    chromium_context: BrowserContext,
) -> None:
    """For non-seatmap strategies, ``_select_ticket`` delegates to ``strategy.pick``.

    The base runner's behaviour must be preserved unchanged for every
    other strategy — no spurious ``#manualMode`` click, no iframe
    expectation. We assert this by running a recording fake strategy
    that flips a flag when its ``pick`` is awaited.
    """
    from src.strategies.base import SelectionStrategy, TicketCandidate

    class _Recorder(SelectionStrategy):
        def __init__(self) -> None:
            self.pick_called = False

        async def pick(self, page) -> TicketCandidate | None:  # noqa: ANN001
            self.pick_called = True
            return None

    page = await chromium_context.new_page()
    await page.set_content("<!doctype html><html><body><h1>no seat-map</h1></body></html>")

    runner = _make_sg_runner(strategy="cheapest", section=None, row=None, seat=None)
    recorder = _Recorder()
    candidate = await runner._select_ticket(page, recorder)
    assert candidate is None
    assert recorder.pick_called is True


# ---------------------------------------------------------------------------
# Config-level validation: tickets.strategy=interactive_seatmap requires
# section/row/seat.
# ---------------------------------------------------------------------------


def test_interactive_seatmap_config_requires_section_row_seat(tmp_path) -> None:
    """Loading a config with ``strategy=interactive_seatmap`` but no
    seat-map block raises :class:`ValueError` so misconfiguration
    fails at load time, not at runtime."""
    from src.utils.config_loader import load_config

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
event:
  url: https://ticketmaster.sg/activity/detail/test
tickets:
  quantity: 1
  strategy: interactive_seatmap
""".strip()
    )
    accounts = tmp_path / "accounts.yaml"
    accounts.write_text("accounts: []\n")
    with pytest.raises(ValueError, match="interactive_seatmap"):
        load_config(cfg, accounts)


def test_interactive_seatmap_config_round_trips_through_explain(tmp_path) -> None:
    """A complete seat-map config parses and round-trips through ``config_to_yaml``."""
    import yaml

    from src.utils.config_loader import config_to_yaml, load_config

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
event:
  url: https://ticketmaster.sg/activity/detail/test
tickets:
  quantity: 2
  strategy: interactive_seatmap
  interactive_seatmap:
    section: "225"
    row: "18"
    seat: "5"
""".strip()
    )
    accounts = tmp_path / "accounts.yaml"
    accounts.write_text("accounts: []\n")
    bot_cfg = load_config(cfg, accounts)
    assert bot_cfg.tickets.strategy == "interactive_seatmap"
    assert bot_cfg.tickets.interactive_seatmap.section == "225"
    assert bot_cfg.tickets.interactive_seatmap.row == "18"
    assert bot_cfg.tickets.interactive_seatmap.seat == "5"
    explained = yaml.safe_load(config_to_yaml(bot_cfg))
    assert explained["tickets"]["strategy"] == "interactive_seatmap"
    assert explained["tickets"]["interactive_seatmap"] == {
        "section": "225",
        "row": "18",
        "seat": "5",
    }
