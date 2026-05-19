"""Validation-contract assertion test for ``[dom.selector-registry-resolves]``.

The assertion's exact wording (from ``validation-contract.md``):

  After registry load, the registry's locator helper returns a non-empty
  match (``locator.count() >= 1``) on
  ``tests/fixtures/strategies/quick_picks_basic.html`` for each name in
  ``{"quick_pick_row", "quantity_select", "add_to_cart_button"}``.

This test loads the committed YAML through the real
``src.registry.selectors`` module (which loads it side-effect-fully on
import), opens ``quick_picks_basic.html`` in real headless Chromium via
the session-scoped ``chromium_context`` fixture, and asserts that
``locator(page, <name>).count() >= 1`` for every name in the contract
set.

Because the contract caps the set explicitly to those three names, we
keep the parametrised case-set in lockstep — adding or removing names
here is a deliberate edit, not an oversight.
"""

from __future__ import annotations

import pytest

from src.registry import selectors as selector_registry
from src.registry.selectors import locator, locator_multi, selector_for

# Exact set from the [dom.selector-registry-resolves] assertion.
CONTRACT_NAMES = ("quick_pick_row", "quantity_select", "add_to_cart_button")


# ---------------------------------------------------------------------------
# Registry-load smoke: every contract name must be present in the
# registry after import (no separate ``load()`` call required).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_contract_name_is_registered_after_yaml_load(name: str) -> None:
    fallbacks = selector_registry.get(name)
    assert isinstance(fallbacks, list)
    assert fallbacks, f"{name!r} loaded with an empty fallback list"
    joined = selector_for(name)
    assert joined.strip(), f"{name!r} resolved to an empty selector string"


# ---------------------------------------------------------------------------
# Real-Chromium DOM resolution against quick_picks_basic.html.
# Covers the operative clause of [dom.selector-registry-resolves]:
# locator.count() >= 1 for every contract name on this fixture.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", CONTRACT_NAMES)
async def test_locator_resolves_against_quick_picks_basic(
    chromium_context, fixture_url, name: str
) -> None:
    page = await chromium_context.new_page()
    await page.goto(fixture_url("quick_picks_basic"))
    # ``locator()`` narrows to ``.first``; ``locator_multi()`` keeps the
    # full match set so ``count()`` reflects the true number of matches
    # for the contract's "count() >= 1" wording.
    loc = locator_multi(page, name)
    count = await loc.count()
    assert count >= 1, (
        f"selector registry name {name!r} resolved to {count} elements on "
        "quick_picks_basic.html — expected at least one"
    )

    # And the single-match helper must produce a real Playwright Locator
    # that is also visible on the page (proves the joined fallback
    # selector is well-formed CSS that Chromium can actually evaluate,
    # not just syntactically parsable).
    first_loc = locator(page, name)
    assert await first_loc.count() >= 1
    assert await first_loc.is_visible(timeout=2000), (
        f"selector registry name {name!r} resolved but the first match is not "
        "visible on quick_picks_basic.html"
    )


async def test_all_three_contract_names_share_a_single_page_load(
    chromium_context, fixture_url
) -> None:
    """One page load → all three names resolve. Mirrors the contract's
    "after registry load, ... for each name in {...}" framing where the
    three names share a single fixture page load.
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("quick_picks_basic"))
    counts = {name: await locator_multi(page, name).count() for name in CONTRACT_NAMES}
    missing = [n for n, c in counts.items() if c < 1]
    assert not missing, (
        f"selector registry names with zero matches on quick_picks_basic.html: "
        f"{missing}; full count map: {counts!r}"
    )


# ---------------------------------------------------------------------------
# Sanity: the YAML is shipped as a real file and the registry helper
# is importable (covers the import-time precondition the assertion
# implicitly requires).
# ---------------------------------------------------------------------------


def test_registry_module_exposes_locator_callable() -> None:
    from src.registry.selectors import locator as locator_fn

    assert callable(locator_fn)
    assert callable(locator_multi)
    assert callable(selector_for)
