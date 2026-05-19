"""Verified-Fan-aware wrapper strategy.

``VFanAwareStrategy`` is a decorator: it fills the page's
``vfan_code_input`` field with the configured code and clicks the
``vfan_submit_button`` (both names resolved through the selector
registry), then delegates to an inner
:class:`~src.strategies.base.SelectionStrategy`. When the input is
absent (e.g. the user lands on an event that no longer requires a code)
the wrapper logs a warning and still runs the inner pick so the run
isn't aborted needlessly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..registry.selectors import locator, selector_for
from .base import SelectionStrategy, TicketCandidate

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger("ticketmaster-bot")


class VFanAwareStrategy(SelectionStrategy):
    """Fill a Verified-Fan code then delegate to ``inner``.

    Parameters
    ----------
    inner:
        Strategy whose ``pick`` is invoked after the gate has been
        attempted.
    code:
        Non-empty Verified-Fan code typed into the vfan-code input.
    """

    def __init__(self, inner: SelectionStrategy, code: str) -> None:
        if not code or not code.strip():
            raise ValueError("VFanAwareStrategy.code must be a non-empty string")
        self.inner = inner
        self.code = code

    async def pick(self, page: Page) -> TicketCandidate | None:
        await _try_submit_vfan_code(page, self.code)
        return await self.inner.pick(page)


async def _try_submit_vfan_code(page: Page, code: str) -> None:
    """Fill the vfan code input and click the submit button if present.

    Best-effort: any failure (input missing, fill timeout, click timeout)
    is logged as a warning and absorbed so the inner strategy can still
    run. The contract is documented by [dom.vfan-aware-no-input].
    """
    input_locator = locator(page, "vfan_code_input")
    try:
        present = await input_locator.count() > 0
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "VFanAwareStrategy: failed to probe %r (%s); skipping vfan step",
            selector_for("vfan_code_input"),
            exc,
        )
        return
    if not present:
        log.warning(
            "VFanAwareStrategy: vfan-code input not present on page "
            "(selector=%r); skipping vfan step",
            selector_for("vfan_code_input"),
        )
        return

    try:
        await input_locator.fill(code, timeout=4000)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "VFanAwareStrategy: failed to fill vfan-code input (%s); "
            "continuing to inner strategy without a submit",
            exc,
        )
        return

    submit_locator = locator(page, "vfan_submit_button")
    try:
        submit_present = await submit_locator.count() > 0
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "VFanAwareStrategy: failed to probe vfan submit button (%s); "
            "code was filled but not submitted",
            exc,
        )
        return
    if not submit_present:
        log.warning(
            "VFanAwareStrategy: vfan submit button not present (selector=%r); "
            "code was filled but not submitted",
            selector_for("vfan_submit_button"),
        )
        return

    try:
        await submit_locator.click(timeout=4000)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "VFanAwareStrategy: failed to click vfan submit button (%s); "
            "code was filled but submit click did not land",
            exc,
        )
        return

    log.info("VFanAwareStrategy: submitted vfan code (len=%d)", len(code))
