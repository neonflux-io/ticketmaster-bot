"""F7.2: Lock in ``config/selectors/ticketmaster_sg.yaml`` and the
``ticketmaster.sg`` trusted-host entry.

This module owns the externally-checkable evidence for the two
validation-contract assertion IDs F7.2 fulfills:

* ``sg.selectors-registered`` — the SG selector YAML loads cleanly,
  declares at least the minimum count of logical names mandated by the
  feature description, and every named lookup resolves to a real DOM
  element on the captured F7.1 fixture for the matching page under
  real headless Chromium.
* ``sg.trusted-host`` — ``ticketmaster.sg`` is in
  ``src.utils.config_loader._DEFAULT_TRUSTED_HOSTS`` so a real SG event
  URL passes ``_validate_event_url(..., strict=True)`` and ``load_config``
  accepts an SG event without ``strict_host=false`` opt-out.

The F7.1 recon test already exercises a parametrised per-selector pass
against the same fixtures; F7.2's tests are deliberately narrower and
re-state the F7.2-specific contract (>=10 entries; trusted-host gate;
representative real-Chromium named-lookup sanity check) so the
assertion that "F7.2 finalised the SG selectors and trusted-host" can
be verified by running just this file.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from src.utils.config_loader import (
    _DEFAULT_TRUSTED_HOSTS,
    _validate_event_url,
    load_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SELECTORS_YAML = REPO_ROOT / "config" / "selectors" / "ticketmaster_sg.yaml"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "vendors" / "ticketmaster_sg"


# ---------------------------------------------------------------------------
# sg.selectors-registered — YAML shape (>=10 entries, non-empty fallbacks)
# ---------------------------------------------------------------------------


def _load_sg_yaml() -> dict[str, list[str]]:
    raw = yaml.safe_load(SELECTORS_YAML.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), "ticketmaster_sg.yaml must be a top-level mapping"
    return raw


def test_sg_selectors_yaml_exists() -> None:
    assert SELECTORS_YAML.is_file(), f"missing {SELECTORS_YAML}"


def test_sg_selectors_yaml_has_at_least_ten_entries() -> None:
    """F7.2 description: 'load YAML and assert >=10 entries'."""
    data = _load_sg_yaml()
    assert len(data) >= 10, (
        f"ticketmaster_sg.yaml must declare at least 10 logical names "
        f"(got {len(data)}); current keys: {sorted(data)}"
    )


def test_sg_selectors_yaml_every_value_is_non_empty_list_of_non_empty_strings() -> None:
    """F7.2 description: 'with non-empty fallbacks'."""
    data = _load_sg_yaml()
    for name, fallbacks in data.items():
        assert isinstance(name, str) and name, f"non-string/empty key in YAML: {name!r}"
        assert isinstance(fallbacks, list) and fallbacks, (
            f"selector {name!r} must map to a non-empty list, got {fallbacks!r}"
        )
        for entry in fallbacks:
            assert isinstance(entry, str) and entry.strip(), (
                f"selector {name!r} contains a non-string/empty fallback: {entry!r}"
            )


# A representative slice of names that must be present so the downstream
# vendor adapter (F7.3 / F7.4) can rely on them. Smaller than the F7.1
# super-set; these are the names the SG adapter's auth/navigator/queue
# modules MUST be able to resolve.
F7_2_REQUIRED_NAMES = (
    "event_date_row",
    "event_find_tickets_link",
    "ticket_area_quantity_select",
    "ticket_area_best_available_button",
    "captcha_image",
    "captcha_input",
    "captcha_submit_button",
    "login_email_input",
    "login_continue_button",
    "csrf_input",
)


@pytest.mark.parametrize("name", F7_2_REQUIRED_NAMES)
def test_required_logical_name_present(name: str) -> None:
    data = _load_sg_yaml()
    assert name in data, f"ticketmaster_sg.yaml is missing required logical name {name!r}"


# ---------------------------------------------------------------------------
# sg.trusted-host — _DEFAULT_TRUSTED_HOSTS contains 'ticketmaster.sg'
# ---------------------------------------------------------------------------


def test_trusted_hosts_contains_ticketmaster_sg() -> None:
    """F7.2: add 'ticketmaster.sg' to _DEFAULT_TRUSTED_HOSTS."""
    assert "ticketmaster.sg" in _DEFAULT_TRUSTED_HOSTS, (
        f"'ticketmaster.sg' must be in _DEFAULT_TRUSTED_HOSTS, got "
        f"{_DEFAULT_TRUSTED_HOSTS!r}"
    )


def test_validate_event_url_accepts_ticketmaster_sg_strict() -> None:
    """A real SG event URL passes the strict-host gate without warnings."""
    _validate_event_url(
        "https://ticketmaster.sg/activity/detail/26sg_pglcs2major",
        strict=True,
    )


def test_validate_event_url_accepts_ticketmaster_sg_subdomain_strict() -> None:
    """Subdomains of ticketmaster.sg also pass (matches the existing logic)."""
    _validate_event_url(
        "https://identity.ticketmaster.sg/exchange",
        strict=True,
    )


def test_load_config_accepts_ticketmaster_sg_event_with_strict_host(tmp_path: Path) -> None:
    """``load_config`` accepts a ticketmaster.sg event URL with ``strict_host: true``.

    Before F7.2 this would raise because the SG host was not trusted.
    """
    (tmp_path / "config.yaml").write_text(
        textwrap.dedent(
            """
            event:
              url: "https://ticketmaster.sg/activity/detail/26sg_pglcs2major"
              strict_host: true
            """
        )
    )
    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing-accounts.yaml")
    assert cfg.event.url == "https://ticketmaster.sg/activity/detail/26sg_pglcs2major"
    assert cfg.event.strict_host is True


# ---------------------------------------------------------------------------
# Real-Chromium named-lookup sanity check on the captured F7.1 fixtures.
#
# F7.2 description: "real-Chromium test against captured fixtures confirms
# named lookups resolve". One representative name per page; if any of
# these fails, the YAML has drifted from the fixtures it was extracted
# against.
# ---------------------------------------------------------------------------

_REPRESENTATIVE_LOOKUPS = (
    # (logical name, fixture file, must-be-visible)
    ("event_date_row", "event_detail.html", True),
    ("event_find_tickets_link", "event_detail.html", True),
    ("ticket_area_quantity_select", "ticket_area.html", True),
    ("ticket_area_best_available_button", "ticket_area.html", True),
    ("ticket_type_row", "ticket_ticket.html", True),
    ("ticket_form_submit_button", "ticket_ticket.html", True),
    ("captcha_image", "check_captcha.html", True),
    ("captcha_input", "check_captcha.html", True),
    ("captcha_submit_button", "check_captcha.html", True),
    ("login_email_input", "login.html", True),
    ("login_continue_button", "login.html", True),
)


@pytest.mark.parametrize(("logical_name", "fixture", "must_be_visible"), _REPRESENTATIVE_LOOKUPS)
async def test_named_lookup_resolves_on_fixture(
    chromium_context,  # noqa: ANN001 - pytest fixture (Playwright BrowserContext)
    fixture_url,  # noqa: ANN001 - pytest fixture (Callable[[str], str])
    logical_name: str,
    fixture: str,
    must_be_visible: bool,
) -> None:
    """Each representative SG selector resolves on its captured fixture.

    Drives real headless Chromium against the file:// URL of the
    distilled F7.1 fixture and asserts that the combined fallback
    selector (registry-style ``", "`` join) matches at least one element.
    """
    data = _load_sg_yaml()
    assert logical_name in data, f"selector YAML missing {logical_name!r}"
    combined = ", ".join(data[logical_name])

    page = await chromium_context.new_page()
    try:
        await page.goto(fixture_url(f"vendors/ticketmaster_sg/{fixture}"))
        locator = page.locator(combined)
        count = await locator.count()
        assert count >= 1, (
            f"selector {logical_name!r} ({combined!r}) matched 0 elements on "
            f"tests/fixtures/vendors/ticketmaster_sg/{fixture}"
        )
        if must_be_visible:
            assert await locator.first.is_visible(timeout=2000), (
                f"selector {logical_name!r} matched but first element is not visible on "
                f"tests/fixtures/vendors/ticketmaster_sg/{fixture}"
            )
    finally:
        await page.close()


async def test_all_required_names_resolve_against_their_fixtures_in_one_pass(
    chromium_context,  # noqa: ANN001 - pytest fixture (Playwright BrowserContext)
    fixture_url,  # noqa: ANN001 - pytest fixture (Callable[[str], str])
) -> None:
    """Aggregate pass: every F7.2-required name resolves on the right fixture.

    Mirrors the contract framing of ``[dom.selector-registry-resolves]``
    in the validation contract — after the YAML is loaded, every named
    lookup the SG adapter relies on must produce ``count() >= 1`` on a
    real Chromium page.
    """
    data = _load_sg_yaml()

    # Map the F7.2 required names to their owning fixture page. Same
    # ownership rules as the parametric block above; centralised here so
    # this test cleanly fails listing every offender if the fixtures or
    # YAML drift apart.
    name_to_fixture = {
        "event_date_row": "event_detail.html",
        "event_find_tickets_link": "event_detail.html",
        "ticket_area_quantity_select": "ticket_area.html",
        "ticket_area_best_available_button": "ticket_area.html",
        "captcha_image": "check_captcha.html",
        "captcha_input": "check_captcha.html",
        "captcha_submit_button": "check_captcha.html",
        "login_email_input": "login.html",
        "login_continue_button": "login.html",
        # csrf_input is rendered hidden in the ticket_ticket and
        # check_captcha forms; either is fine, pick the latter so this
        # one assertion covers a name that's not in the parametric set.
        "csrf_input": "check_captcha.html",
    }
    misses: list[str] = []
    page = await chromium_context.new_page()
    try:
        for name, fixture in name_to_fixture.items():
            assert name in data, f"YAML missing {name!r}"
            combined = ", ".join(data[name])
            await page.goto(fixture_url(f"vendors/ticketmaster_sg/{fixture}"))
            count = await page.locator(combined).count()
            if count < 1:
                misses.append(f"{name} on {fixture} (selector: {combined!r})")
    finally:
        await page.close()
    assert not misses, "F7.2 named lookups failed to resolve:\n  " + "\n  ".join(misses)
