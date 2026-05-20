"""ticketmaster.sg checkout module — delivery, payment, review, place-order.

The four public surfaces match the F7.4 brief and the
:mod:`src.vendors.ticketmaster.checkout` shape:

* :func:`select_delivery` picks a preferred SG delivery option
  (mobile entry / e-ticket / venue collection) by label keyword,
  with a configurable allow-any-fallback.
* :func:`select_saved_card` ticks a saved-card radio by the last-four
  digits — handles both the ``Visa ending in 4242`` and
  ``Mastercard **** 4242`` formats the SG card list renders.
* :func:`verify_cart_matches` reads the SG order-summary panel and
  validates quantity / section / SGD price against expectations. The
  SGD parser (:func:`src.vendors.ticketmaster_sg.price.parse_sgd_price`)
  understands the four SG dollar shapes (bare ``$``, ``S$``, ``SGD ``,
  ``SGD$``) and still falls back to the shared US ``$1,234.56``
  extractor for compatibility.
* :func:`run_checkout` orchestrates the steps and stops at the review
  stage when ``auto_purchase=false`` — identical contract to the US
  flow per the F7.4 brief.

Every selector lookup goes through
:mod:`src.vendors.ticketmaster_sg.selectors` so the inline-selector
grep gate stays at zero matches.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from ...utils import purchase_guard
from ...utils.retry import random_human_delay
from . import auth as sg_auth
from . import cart as sg_cart
from . import selectors as sg_selectors
from .price import parse_sgd_price

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ...strategies.base import TicketCandidate

log = logging.getLogger("ticketmaster-bot")

# Default delivery preference list — SG-friendly order. The brief calls
# out "mobile entry, e-ticket, venue collection" as the three SG
# options; the bot prefers digital (mobile) over PDF (e-ticket) over
# at-venue (collection) because that ordering minimises post-purchase
# touchpoints.
SG_DEFAULT_DELIVERY_PREFERENCE: tuple[str, ...] = (
    "mobile entry",
    "mobile ticket",
    "e-ticket",
    "eticket",
    "venue collection",
)


class CheckoutError(Exception):
    """Raised when SG checkout cannot proceed."""


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


async def _click_radio_or_label(page: Page, label_locator) -> bool:  # noqa: ANN001
    """Click a delivery / payment label and tick the linked radio if any.

    SG's Yii/Bootstrap form pattern wires the click on the ``<label>``
    via the ``for=...`` attribute. Clicking the label is sufficient,
    but if for some reason that misses (e.g. the label wraps the
    radio), we follow up by explicitly checking any nearby radio.
    """
    try:
        await label_locator.click()
    except Exception as exc:  # noqa: BLE001
        log.debug("Label click failed: %s", exc)
        return False

    # Best-effort: also tick the sibling radio so headless tests can
    # read the checked state via the radio rather than the label.
    try:
        for_attr = await label_locator.get_attribute("for", timeout=300)
    except Exception:  # noqa: BLE001
        for_attr = None
    if for_attr:
        try:
            radio = page.locator(f"input#{for_attr}").first
            if await radio.is_visible(timeout=300) and not await radio.is_checked():
                await radio.check()
        except Exception:  # noqa: BLE001
            pass
    return True


async def _select_delivery_option_in_native_select(
    page: Page, keyword: str
) -> bool:
    """Try to pick a delivery option in the SG ``<select>`` shipment list.

    The live SG ``/ticket/checkout`` page (F7.6 capture) renders
    delivery options as ``<option>``s inside ``select#checkoutform-shipmentid``
    rather than as a radio-button group. This helper looks up the
    select via the SG registry, scans its options' visible text for a
    case-insensitive substring match against ``keyword``, and calls
    ``select_option`` with the matched ``value=``. Returns True when
    an option matched and was selected; False otherwise.
    """
    keyword_lc = keyword.lower()
    for sel in sg_selectors.get("checkout_delivery_select"):
        try:
            select = page.locator(sel).first
            if not await select.is_visible(timeout=400):
                continue
            options = select.locator("option")
            count = await options.count()
            for i in range(count):
                opt = options.nth(i)
                try:
                    text = (await opt.inner_text(timeout=300)) or ""
                    value = await opt.get_attribute("value", timeout=300)
                except Exception:  # noqa: BLE001
                    continue
                if not value:
                    continue
                if keyword_lc in text.lower():
                    await select.select_option(value=value)
                    log.info(
                        "Selected SG delivery option %r (value=%s) via native select",
                        text.strip(),
                        value,
                    )
                    return True
        except Exception as exc:  # noqa: BLE001
            log.debug("Native delivery select scan via %s failed: %s", sel, exc)
            continue
    return False


async def _select_first_delivery_option_in_native_select(page: Page) -> bool:
    """Pick the first non-empty option in the SG shipment ``<select>``."""
    for sel in sg_selectors.get("checkout_delivery_select"):
        try:
            select = page.locator(sel).first
            if not await select.is_visible(timeout=400):
                continue
            options = select.locator("option")
            count = await options.count()
            for i in range(count):
                opt = options.nth(i)
                try:
                    value = await opt.get_attribute("value", timeout=300)
                    text = (await opt.inner_text(timeout=300)) or ""
                except Exception:  # noqa: BLE001
                    continue
                if not value:
                    # First option is the "Please Select" placeholder; skip.
                    continue
                await select.select_option(value=value)
                log.warning(
                    "No preferred SG delivery match - falling back to first "
                    "shipment option %r (value=%s)",
                    text.strip(),
                    value,
                )
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def select_delivery(
    page: Page,
    *,
    preferred: list[str],
    allow_any: bool,
) -> bool:
    """Select an SG delivery option by label keyword.

    ``preferred`` is an ordered list of keywords to try in turn
    (case-insensitive substring match against the visible label /
    option text). The function handles two SG DOM shapes:

    1. The live ``/ticket/checkout`` page renders delivery options as
       ``<option>``s in ``select#checkoutform-shipmentid``; we pick
       the matching ``value`` via ``select_option``.
    2. Earlier / hand-built fixtures rendered delivery options as a
       radio-button group; we click the matching label and tick the
       linked radio for compatibility.

    If neither shape yields a match and ``allow_any`` is ``False`` the
    function leaves delivery untouched and returns ``False``; if
    ``allow_any`` is ``True`` it falls back to the first delivery
    control it can find (last-resort policy — only enabled by
    explicit caller opt-in).
    """
    for keyword in preferred:
        try:
            # Native <select> path first (live SG DOM).
            if await _select_delivery_option_in_native_select(page, keyword):
                return True
            # Legacy radio-label path (hand-built / older fixtures).
            label = sg_selectors.locator_template(
                page, "checkout_delivery_option_label_template", keyword=keyword
            )
            if await label.is_visible(timeout=800):
                if await _click_radio_or_label(page, label):
                    log.info("Selected SG delivery option matching %r", keyword)
                    return True
        except KeyError as exc:
            # Selector YAML missing the template — surface clearly.
            raise CheckoutError(
                f"Delivery selector template not registered: {exc.args[0]!r}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            log.debug("Delivery label %r match failed: %s", keyword, exc)
            continue

    if not allow_any:
        log.warning(
            "No preferred SG delivery option matched %s and allow_any=False; "
            "leaving delivery untouched",
            preferred,
        )
        return False

    if await _select_first_delivery_option_in_native_select(page):
        return True
    for sel in sg_selectors.get("checkout_delivery_radio"):
        try:
            radio = page.locator(sel).first
            if await radio.is_visible(timeout=800):
                await radio.check()
                log.warning(
                    "No preferred SG delivery match - falling back to first option (%s)",
                    sel,
                )
                return True
        except Exception:  # noqa: BLE001
            continue
    log.debug("No SG delivery selection needed (or unable to detect)")
    return False


# ---------------------------------------------------------------------------
# Saved card
# ---------------------------------------------------------------------------


async def select_saved_card(page: Page, last_four: str | None) -> bool:
    """Select a saved card by last-four digits. Returns True if ticked.

    The SG card list renders both ``Visa ending in 4242`` and
    ``Mastercard **** 4242`` style labels; the YAML template covers
    both via comma-joined fallbacks. ``last_four`` must be exactly
    four ASCII digits — non-digit input returns ``False`` without
    touching the DOM.
    """
    if not last_four:
        return False
    if not (len(last_four) == 4 and last_four.isdigit()):
        log.warning("select_saved_card: last_four must be 4 digits, got %r — ignoring", last_four)
        return False

    try:
        label = sg_selectors.locator_template(
            page, "checkout_saved_card_label_template", last_four=last_four
        )
        if await label.is_visible(timeout=2000):
            if await _click_radio_or_label(page, label):
                log.info("Selected SG saved card ending in %s", last_four)
                return True
    except KeyError as exc:
        raise CheckoutError(
            f"Saved-card selector template not registered: {exc.args[0]!r}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        log.debug("Saved-card selection failed: %s", exc)
    return False


# ---------------------------------------------------------------------------
# Cart-match verification
# ---------------------------------------------------------------------------


def _extract_quantity(text: str) -> int | None:
    """Find an integer ticket count in the SG order-summary text.

    The SG ``/ticket/checkout`` page decorates its order-summary with
    a hex-style anti-scrape token (``<span class="hex_Order_number">``)
    next to the column headers, so the inner_text concatenation can
    end up containing fragments like ``12b3861 Seat Info``. The
    regex therefore requires a word-boundary BEFORE the digit run
    (so ``3861 Seat`` won't be picked from ``12b3861 Seat``) and
    explicitly tightens the unit suffix to the SG forms (``ticket(s)``
    or ``seat(s)``). The dedicated ``span#cartTotalTicket`` node on
    the live SG page contains exactly ``N ticket(s)``; that's what
    this regex is designed to pull.
    """
    match = re.search(
        r"(?<![A-Za-z0-9])(\d+)\s*(?:tickets?|seats?)\b",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


# Greedy SG section extractor. The shared US extractor in
# :mod:`src.strategies.base` is word-bounded on a single token after the
# ``Section`` keyword (so ``Section: GENADM`` works but ``Section: GEN ADM``
# only captures ``GEN``). The SG site renders multi-word labels like
# ``Section: GEN ADM`` and ``Section GENERAL ADMISSION`` so we sweep up the
# rest of the run of uppercase tokens following the keyword here.
#
# The match deliberately stops at the first newline / sentence boundary
# so trailing summary lines (``Quantity 2 tickets``, ``Subtotal …``) do
# not get pulled into the captured section text.
_SG_SECTION_RE = re.compile(
    r"\b(?:section|sec)\b[ \t]*:?[ \t]*([A-Z0-9]+(?:[ \t]+[A-Z0-9]+)*)",
    re.IGNORECASE,
)

# Labels that often appear immediately after the section value on the SG
# order-summary line (``Section GENADM Quantity 2 tickets …``). When the
# extractor's greedy run reaches one of these the value is truncated to
# everything before it.
_SG_SECTION_STOPWORDS = (
    "quantity",
    "subtotal",
    "total",
    "booking",
    "fee",
    "row",
    "delivery",
    "payment",
)


def _extract_sg_sections(text: str) -> list[str]:
    """Return uppercased section tokens, SG-friendly.

    The base extractor (``src.strategies.base._extract_sections``) is the
    canonical single-token form used by every US selector test. The SG cart
    sometimes renders multi-word section labels (``Section: GEN ADM``,
    ``Section GENERAL ADMISSION``) where the base extractor would only
    capture the first token; this SG-side helper accepts a trailing run of
    uppercase tokens so the section comparison matches both compact
    (``GENADM``) and space-separated (``GEN ADM``) forms. The extractor
    stops at newlines and at SG summary-row stop-words so the captured
    value never spills into the ``Quantity ...`` / ``Subtotal ...`` lines.
    """
    out: list[str] = []
    for m in _SG_SECTION_RE.finditer(text):
        value = m.group(1).upper().strip()
        if not value:
            continue
        # Trim at the first stop-word token so the section value never
        # absorbs the next summary line when the inner-text concatenates
        # the cells onto a single space-separated string.
        tokens = value.split()
        kept: list[str] = []
        for tok in tokens:
            if tok.lower() in _SG_SECTION_STOPWORDS:
                break
            kept.append(tok)
        cleaned = " ".join(kept).strip()
        if cleaned:
            out.append(cleaned)
    return out


def _normalise_section(value: str) -> str:
    """Compare-friendly section key: uppercase, whitespace collapsed.

    ``GEN ADM`` and ``GENADM`` are treated as equal because the SG site
    sometimes prints the label with a space and sometimes without; the
    YAML config typically gives the compact form.
    """
    return "".join(value.upper().split())


async def verify_cart_matches(
    page: Page,
    *,
    expected_quantity: int,
    expected_candidate: TicketCandidate | None,
    price_tolerance: float = 0.05,
) -> bool:
    """Read the SG order summary and verify it matches expectations.

    Returns True on match or if we could not read enough text to
    disprove the match (logged as a warning); returns False on a
    definitive quantity / section / price mismatch. Price parsing is
    SG-aware via :func:`parse_sgd_price` and tolerates bare ``$``,
    ``S$``, ``SGD ``, and ``SGD$`` prefixes.
    """
    text: str = ""
    for sel in sg_selectors.get("cart_order_summary"):
        try:
            loc = page.locator(sel).first
            if not await loc.is_visible(timeout=1500):
                continue
            chunk = (await loc.inner_text(timeout=2000)) or ""
            if chunk.strip():
                text = chunk
                break
        except Exception:  # noqa: BLE001
            continue
    if not text:
        log.warning("Could not read SG order summary for verification - proceeding cautiously")
        return True

    # Prefer the canonical ticket-count node when present — the SG
    # order summary inner-text contains anti-scrape hex tokens and
    # alphanumeric section labels (e.g. "EXCL. 230-232 Ticket Info")
    # that confuse a naive ``\d+ tickets`` regex applied to the whole
    # summary. The dedicated ``span#cartTotalTicket`` node carries
    # exactly ``N ticket(s)``.
    found_qty: int | None = None
    try:
        count_node = sg_selectors.locator(page, "cart_ticket_count_node")
        if await count_node.is_visible(timeout=800):
            count_text = (await count_node.inner_text(timeout=1500)) or ""
            found_qty = _extract_quantity(count_text)
    except Exception:  # noqa: BLE001
        found_qty = None
    if found_qty is None:
        lowered = text.lower()
        found_qty = _extract_quantity(lowered)
    if found_qty is not None and found_qty != expected_quantity:
        log.error(
            "SG cart quantity mismatch: expected %d, summary shows %d",
            expected_quantity,
            found_qty,
        )
        return False

    if expected_candidate is None:
        return True

    if expected_candidate.section:
        found_sections: set[str] = {
            _normalise_section(s) for s in _extract_sg_sections(text)
        }
        # The live SG ``/ticket/checkout`` page never prints the
        # literal word "Section:"; per-row section labels live inside
        # ``<div class="ticket-info">`` (first line is the zone/group
        # label, second line is the ticket-type). Read those nodes
        # directly so the section check works on the live capture.
        try:
            row_nodes = sg_selectors.locator_multi(page, "cart_row_ticket_info")
            row_count = await row_nodes.count()
            for i in range(row_count):
                node = row_nodes.nth(i)
                try:
                    if not await node.is_visible(timeout=400):
                        continue
                    raw = (await node.inner_text(timeout=800)) or ""
                except Exception:  # noqa: BLE001
                    continue
                # First non-blank line is the section label
                # ("GEN ADM"); second line is the ticket-type
                # ("Standard $144.00"). Splitting on newlines avoids
                # accidentally swallowing the price/tier text.
                first_line = next(
                    (ln.strip() for ln in raw.splitlines() if ln.strip()),
                    "",
                )
                if first_line:
                    found_sections.add(_normalise_section(first_line))
        except Exception:  # noqa: BLE001
            pass
        expected_norm = _normalise_section(expected_candidate.section)
        if found_sections and expected_norm not in found_sections:
            log.error(
                "SG cart section mismatch: expected %s, summary shows %s",
                expected_candidate.section,
                sorted(found_sections),
            )
            return False

    if expected_candidate.price is not None:
        # Prefer the explicit cart-total node; fall back to the largest
        # price token in the broader summary text.
        found_price: float | None = None
        for sel in sg_selectors.get("cart_order_total"):
            try:
                loc = page.locator(sel).first
                if not await loc.is_visible(timeout=800):
                    continue
                node_text = (await loc.inner_text(timeout=1500)) or ""
                parsed = parse_sgd_price(node_text)
                if parsed is not None:
                    found_price = parsed
                    break
            except Exception:  # noqa: BLE001
                continue
        if found_price is None:
            found_price = parse_sgd_price(text)
        if found_price is not None:
            allowed = expected_candidate.price * expected_quantity
            ratio = found_price / max(allowed, 0.01)
            # The base-price total can grow up to 1.5x with SG fees /
            # taxes; we accept anything within tolerance of [base, 1.5x].
            if not (1.0 - price_tolerance <= ratio <= 1.0 + 0.5 + price_tolerance):
                log.error(
                    "SG cart price mismatch: expected ~$%.2f (qty=%d), summary $%.2f (ratio=%.2f)",
                    allowed,
                    expected_quantity,
                    found_price,
                    ratio,
                )
                return False

    return True


# ---------------------------------------------------------------------------
# Final orchestration
# ---------------------------------------------------------------------------


async def run_checkout(
    page: Page,
    *,
    auto_purchase: bool,
    card_last_four: str | None,
    action_delay: tuple[float, float] = (0.5, 2.0),
    preferred_delivery: list[str] | None = None,
    allow_any_delivery: bool = False,
    expected_quantity: int = 0,
    expected_candidate: TicketCandidate | None = None,
    price_tolerance: float = 0.05,
    captcha_timeout_seconds: float = 300.0,
) -> bool:
    """Execute the SG checkout flow.

    If ``auto_purchase`` is False, stops after selecting delivery /
    saved card so the user can review and click Place Order manually
    (identical halt-at-review contract to the US flow).

    If ``auto_purchase`` is True, additionally:

    1. Calls :func:`verify_cart_matches` and aborts on mismatch.
    2. Accepts terms (the SG checkout step often re-shows them).
    3. Pauses for any captcha re-thrown at the final review step.
    4. Clicks the Place Order button.
    5. Confirms an order-confirmation page rendered.
    """
    preferred = list(preferred_delivery or SG_DEFAULT_DELIVERY_PREFERENCE)

    await random_human_delay(*action_delay)
    await select_delivery(page, preferred=preferred, allow_any=allow_any_delivery)
    await random_human_delay(*action_delay)

    if card_last_four:
        await select_saved_card(page, card_last_four)
        await random_human_delay(*action_delay)

    if not auto_purchase:
        log.info(
            "auto_purchase=false — halting SG checkout at review. "
            "Please review and click Place Order manually."
        )
        return True

    if expected_quantity > 0:
        matched = await verify_cart_matches(
            page,
            expected_quantity=expected_quantity,
            expected_candidate=expected_candidate,
            price_tolerance=price_tolerance,
        )
        if not matched:
            log.error("Aborting SG auto-purchase due to cart mismatch")
            return False

    # SG re-shows the terms checkbox on the final review step.
    await sg_cart.accept_terms_if_needed(page)
    await random_human_delay(*action_delay)

    # SG can throw a final captcha at the last review step.
    await sg_auth.wait_for_human_if_captcha(page, timeout_seconds=captcha_timeout_seconds)

    try:
        btn = sg_selectors.locator(page, "checkout_place_order_button")
        await btn.wait_for(state="visible", timeout=8000)
    except Exception as exc:  # noqa: BLE001
        log.error("Could not find SG 'Place Order' button: %s", exc)
        return False

    # Mission-wide kill switch: refuses to click Place Order unless the
    # operator has explicitly set the override env var. Treat as a soft
    # failure so the runner doesn't crash — the SG cart/checkout review
    # state is preserved for manual completion.
    try:
        purchase_guard.gate("place_order")
    except purchase_guard.PurchaseBlocked as exc:
        log.error("SG Place Order blocked by purchase guard: %s", exc)
        return False

    log.info("Clicking SG Place Order")
    try:
        await btn.click()
    except Exception as exc:  # noqa: BLE001
        log.error("SG Place Order click failed: %s", exc)
        return False

    try:
        await page.wait_for_load_state("networkidle", timeout=60_000)
    except Exception:  # noqa: BLE001
        pass
    return await _verify_order_placed(page)


# ---------------------------------------------------------------------------
# Internal: order-confirmation probe
# ---------------------------------------------------------------------------


async def _verify_order_placed(page: Page) -> bool:
    """Return True if the SG order confirmation page appears to have rendered."""
    url = (page.url or "").lower()
    for marker in ("confirmation", "thankyou", "thank-you", "order-complete"):
        if marker in url:
            return True
    for sel in sg_selectors.get("checkout_order_confirmation"):
        try:
            if await page.locator(sel).first.is_visible(timeout=3000):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


__all__ = [
    "CheckoutError",
    "SG_DEFAULT_DELIVERY_PREFERENCE",
    "run_checkout",
    "select_delivery",
    "select_saved_card",
    "verify_cart_matches",
]
