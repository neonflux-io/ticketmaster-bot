"""SGD price-parser tests for :mod:`src.vendors.ticketmaster_sg.price`.

Verifies that the SG parser understands every dollar-text shape the
ticketmaster.sg site actually renders (bare ``$``, ``S$``, ``SGD ``,
``SGD$``) AND that the existing US ``$1,234.56`` parser keeps working
side-by-side via :func:`src.strategies.base._extract_price`. The F7.4
brief explicitly asks for this side-by-side test so a future change
to either parser can't silently regress the other.
"""

from __future__ import annotations

import pytest

from src.strategies.base import _extract_price as us_extract_price
from src.vendors.ticketmaster_sg.price import parse_sgd_price

# ---------------------------------------------------------------------------
# SG-format inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Bare-$ (the form observed on the live PGL CS 2 Major page).
        ("$144.00", 144.00),
        ("$144", 144.00),
        ("$0.50", 0.50),
        # S$ prefix.
        ("S$144.00", 144.00),
        ("S$1,234.56", 1234.56),
        # ISO code prefix (with and without trailing $).
        ("SGD 288.00", 288.00),
        ("SGD288.00", 288.00),
        ("SGD$1,234.56", 1234.56),
        ("sgd 12.50", 12.50),  # case-insensitive
        # Comma grouping holds.
        ("$1,234.56", 1234.56),
        ("$12,345.67", 12345.67),
        ("$1,234,567.89", 1234567.89),
        # Embedded inside a sentence.
        ("Total: $1,234", 1234.0),
        ("Subtotal: SGD 288.00 plus fees", 288.00),
        ("Booking fee S$8.00", 8.00),
    ],
)
def test_parse_sgd_price_accepts_known_shapes(text: str, expected: float) -> None:
    assert parse_sgd_price(text) == pytest.approx(expected)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "see venue",
        "TBA",
        "no price info",
        "Free admission",
        "Section 100 Row C",
    ],
)
def test_parse_sgd_price_returns_none_for_non_prices(text: str) -> None:
    assert parse_sgd_price(text) is None


def test_parse_sgd_price_rejects_non_string() -> None:
    assert parse_sgd_price(None) is None  # type: ignore[arg-type]
    assert parse_sgd_price(12345) is None  # type: ignore[arg-type]
    assert parse_sgd_price(["$10"]) is None  # type: ignore[arg-type]


def test_parse_sgd_price_picks_first_token_in_multi_price_text() -> None:
    """When text contains multiple prices, the SG parser returns the first."""
    assert parse_sgd_price("$10.00 / $25.00 / $50.00") == pytest.approx(10.0)
    assert parse_sgd_price("S$5 then SGD 100") == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Side-by-side: US extractor must still work after F7.4 ships
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$89.50", 89.50),
        ("$1,234.56", 1234.56),
        ("Total $250", 250.0),
        ("Section 200 Row C $100.00", 100.00),
    ],
)
def test_us_extract_price_still_works(text: str, expected: float) -> None:
    """The shared US ``$1,234.56`` parser keeps its contract."""
    assert us_extract_price(text) == pytest.approx(expected)


def test_us_and_sg_parsers_agree_on_bare_dollar() -> None:
    """For bare ``$X`` input, the SG parser must equal the US parser."""
    for text in ("$10", "$100.00", "$1,234.56", "Total: $25"):
        assert parse_sgd_price(text) == us_extract_price(text)


def test_sg_parser_strict_superset_of_us_parser() -> None:
    """SG parser handles everything the US parser handles, plus the SG prefixes."""
    # US shape — both return same value.
    assert parse_sgd_price("$144.00") == us_extract_price("$144.00")
    # SG-only shape — SG parser must return a number; US parser fails.
    assert parse_sgd_price("S$144.00") == 144.0
    assert us_extract_price("S$144.00") == 144.0  # US parser does match bare $ inside S$
    # ISO-code shape — only SG parser handles.
    assert parse_sgd_price("SGD 288.00") == 288.0
    assert us_extract_price("SGD 288.00") is None
