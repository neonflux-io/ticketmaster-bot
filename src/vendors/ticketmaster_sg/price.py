"""SGD price-text parsing for the ticketmaster.sg vendor adapter.

The SG site renders Singapore-dollar prices in four equivalent shapes
(verified against the F7.1 recon report and the static SG help-centre
pages linked from /ticket/check-captcha/...):

* Bare dollar sign — ``$144.00`` (the form used on the PGL CS 2 Major
  ticket-area page; identical visual to USD on the .com site, so context
  rather than glyph is the disambiguation signal).
* SGD prefix with non-breaking ``S`` — ``S$144.00``.
* ISO currency code without sign — ``SGD 288.00`` (used in some help
  pages and the e-mail receipt template).
* ISO currency code with sign — ``SGD$1,234.56``.

The shared :func:`src.strategies.base._extract_price` parser already
handles the bare ``$1,234.56`` shape (it's currency-agnostic — the US
flow uses the same code path), so the SG parser delegates to it for
that case and adds explicit support for the three SG-flavoured
prefixes. The function is intentionally side-effect-free and pure-Python
so it can be exercised by ordinary unit tests as well as the
real-Chromium DOM tests.

Usage:

    from src.vendors.ticketmaster_sg.price import parse_sgd_price

    parse_sgd_price("$144.00")        # → 144.0
    parse_sgd_price("S$144.00")       # → 144.0
    parse_sgd_price("SGD 288.00")     # → 288.0
    parse_sgd_price("SGD$1,234.56")   # → 1234.56
    parse_sgd_price("Total: $1,234")  # → 1234.0  (embedded match)
    parse_sgd_price("see venue")      # → None
"""

from __future__ import annotations

import re

from ...strategies.base import _extract_price as _shared_extract_price

__all__ = ["parse_sgd_price"]

# Match either:
#   * a "SGD " (with optional trailing $) or "S$" prefix immediately
#     followed by a number, OR
#   * a bare "$" followed by a number (same shape the US flow uses).
#
# The numeric body allows one or more comma-grouped digit clusters and
# an optional two-decimal-place fraction. We keep the regex
# intentionally narrow so a stray "$" embedded in a sentence still
# resolves to the first plausible price token rather than the first
# digit-of-anything on the page.
_SGD_PRICE_RE = re.compile(
    r"""
    (?:
        (?:SGD\s*\$?)            # ISO-code prefix, optional trailing $
        |
        (?:S\$)                  # SG dollar prefix with capital S
        |
        \$                       # bare US-style dollar sign
    )
    \s*
    (?P<amount>
        \d{1,3}(?:,\d{3})*       # 1,234,567
        (?:\.\d{1,2})?           # .00 / .5
        |
        \d+(?:\.\d{1,2})?        # 144 / 144.00
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def parse_sgd_price(text: str) -> float | None:
    """Return the first SGD price found in ``text`` as a float, or ``None``.

    Accepts any of the four documented SG formats (``$X``, ``S$X``,
    ``SGD X``, ``SGD$X``) and falls back to the shared
    :func:`src.strategies.base._extract_price` for plain ``$X`` /
    ``$1,234.56`` patterns to keep behaviour identical to the US flow
    when no SG-specific prefix is present.

    Returns ``None`` when the input is not a string or no price token
    can be extracted.
    """
    if not isinstance(text, str):
        return None

    match = _SGD_PRICE_RE.search(text)
    if match is not None:
        raw = match.group("amount").replace(",", "")
        try:
            return float(raw)
        except ValueError:
            return None

    # Fall back to the shared $X parser used by the US flow. This keeps
    # behaviour identical for the bare-$ shape (the US site never uses
    # the SGD/S$ prefixes) and also confirms the side-by-side
    # contract the F7.4 brief calls out — the SGD parser does not
    # break the existing US $1,234.56 parser.
    return _shared_extract_price(text)
