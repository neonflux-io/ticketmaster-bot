# Selection strategies

Every strategy implements the `SelectionStrategy` ABC in
`src/strategies/base.py`. The base class also provides
`list_candidates(page)`, which walks the `quick_pick_row` selector (see
`config/selectors/ticketmaster.yaml`) and parses each row into a
`TicketCandidate` dataclass (price, section, row, parsed sections list,
optional `data-price-level-id` attribute).

Strategies are registered with `src/registry/strategies.py` at import
time (see `_register_default_strategies` in
`src/strategies/factory.py`) and instantiated through
`build_strategy(cfg)` from the same module. The config dataclass keys
they read are defined in `src/utils/config_loader.py`.

The Ticketmaster fixture pages used in the test suite live under
`tests/fixtures/` and each strategy section below references the
fixture it is exercised against.

---

## `cheapest`

Source: `src/strategies/cheapest.py`.

Walks every parsed candidate, drops rows above the optional `max_price`
cap, and clicks the survivor with the lowest parsed price. Rows whose
price could not be parsed sort to the back of the list.

Config keys (`tickets:` block):

```yaml
tickets:
  strategy: "cheapest"
  max_price: 250.0     # optional inclusive upper bound
  quantity: 2
```

---

## `best_available`

Source: `src/strategies/best_available.py`.

Picks the first row Ticketmaster's own "Best Available" sort emits.
Honours `max_price` the same way `cheapest` does.

```yaml
tickets:
  strategy: "best_available"
  max_price: null
```

---

## `section_target`

Source: `src/strategies/section_target.py`.

Filters candidates down to a configured `(section, row_range,
price_level_id)` triple and picks the cheapest survivor. Row ranges
are inclusive and lexicographic.

```yaml
tickets:
  strategy: "section_target"
  section_target:
    section: "108"
    row_range: ["A", "Z"]
    price_level_id: "PL1"
  max_price: 400
```

---

## `price_range`

Source: `src/strategies/price_range.py`.

Filters by `[min_price, max_price]` (both bounds inclusive, either may
be `null`) and clicks the cheapest survivor.

```yaml
tickets:
  strategy: "price_range"
  price_range:
    min_price: 100.0
    max_price: 300.0
```

Validation contract: the strategy returns `None` when no row falls in
the band, matching the `dom.price-range-reject` assertion. Fixture:
`tests/fixtures/strategies/price_range_basic.html`.

---

## `multi_section`

Source: `src/strategies/multi_section.py`.

Walks an ordered list of section preferences. The first preference
with at least one matching row wins; among the matching rows the
cheapest is clicked. Sections are compared uppercase so callers may
pass either `"100"` or `"Floor"` regardless of DOM casing.

```yaml
tickets:
  strategy: "multi_section"
  multi_section:
    sections: ["100", "200", "Floor"]
  max_price: 500
```

Empty section lists are rejected at construction (raises `ValueError`).

---

## `accessible`

Source: `src/strategies/accessible.py`.

Filters rows to only those that carry the `accessible_seat_marker`
selector (defined in `config/selectors/ticketmaster.yaml`) and clicks
the cheapest. When `tickets.accessible_seats: false`, the filter is
bypassed and the strategy degrades into a plain cheapest pick — this
keeps the existing config key meaningful.

```yaml
tickets:
  strategy: "accessible"
  accessible_seats: true
  max_price: null
```

Fixture: `tests/fixtures/strategies/accessible.html`.

---

## `seat_quality`

Source: `src/strategies/seat_quality.py`.

Scores every candidate using:

1. `data-quality-score` row attribute (cast to float), and
2. `+1.0` per whole-word match (case-insensitive) of `floor`,
   `center`, `aisle`, or `front` in the row text.

The highest score wins; cheaper row breaks score ties; the original
DOM order is the final tie-breaker so the call is deterministic.

```yaml
tickets:
  strategy: "seat_quality"
  max_price: null
```

Fixture: `tests/fixtures/strategies/seat_quality.html`.

---

## `random_pick`

Source: `src/strategies/random_pick.py`.

Shuffles the candidate list (with an optional `seed` for deterministic
tests) and clicks the first row that survives the optional `max_price`
cap. Two calls with the same `seed` against the same DOM produce the
same pick — the `dom.random-pick-deterministic` validation assertion
relies on this.

```yaml
tickets:
  strategy: "random_pick"
  max_price: null
```

The strategy class accepts a `seed: int | None` constructor argument
that the factory currently does not forward; callers using the
strategy directly (tests, custom integrations) can pass a seed
explicitly. Fixture:
`tests/fixtures/strategies/quick_picks_basic.html`.

---

## `composite`

Source: `src/strategies/composite.py`.

Tries each child strategy in order and returns the first non-`None`
result. The composite never clicks itself; the winning child clicks.
Construction with an empty list raises `ValueError` so misconfiguration
fails loudly.

The factory does not currently expose a composite via YAML keys — it
is intended for programmatic use by callers building custom chains
(e.g. tests). Use it directly:

```python
from src.strategies.composite import CompositeStrategy
from src.strategies.price_range import PriceRangeStrategy
from src.strategies.multi_section import MultiSectionStrategy

strategy = CompositeStrategy(children=[
    PriceRangeStrategy(min_price=0, max_price=200),
    MultiSectionStrategy(sections=["100"]),
])
```

---

## `interactive_seatmap`

Source: `src/strategies/interactive_seatmap.py`.

Locates the seat-map `<rect data-section=… data-row=… data-seat=…>`
matching the configured tuple and clicks it. Top-frame first, then
every iframe matched by the `seatmap_frame` selector
(`config/selectors/ticketmaster.yaml`). The selector template comes
from `seatmap_seat_rect_template` in the same YAML so the data-attr
strings stay out of Python.

```yaml
tickets:
  strategy: "interactive_seatmap"
  interactive_seatmap:
    section: "100"
    row: "A"
    seat: "5"
```

Validation contract: `dom.interactive-seatmap-happy` asserts the
clicked element is the `<rect>` whose data attrs match. Fixture:
`tests/fixtures/strategies/seatmap_svg.html`.

---

## `resale_filter`

Source: `src/strategies/resale_filter.py`.

Wraps any other strategy and pre-filters rows by the presence
(`include_resale=True`) or absence (`exclude_resale=True`) of the
`resale_tag` marker selector (`config/selectors/ticketmaster.yaml`)
before letting the inner strategy run.

The filter is implemented by temporarily renaming the `data-bdd`
attribute on every row that should be hidden, then restoring the
attribute inside a `finally` block — that way the DOM is never left in
a weird state even if the inner strategy raises.

```yaml
tickets:
  strategy: "resale_filter"
  inner_strategy:
    strategy: "cheapest"
    max_price: 300
  resale_filter:
    exclude_resale: true       # XOR with include_resale
```

Setting both `include_resale: true` and `exclude_resale: true` (or
neither) raises `ValueError`. Validation contract:
`dom.resale-filter-delegate`, `dom.resale-filter-reject`.

---

## `vfan_aware`

Source: `src/strategies/vfan_aware.py`.

Wraps any other strategy. Before delegating, fills the
`vfan_code_input` field (`config/selectors/ticketmaster.yaml`) with the
configured code and clicks `vfan_submit_button` (same YAML file). Each
step is best-effort: a missing input logs a warning and lets the inner
strategy run anyway — that matches the `dom.vfan-aware-no-input`
validation assertion.

```yaml
tickets:
  strategy: "vfan_aware"
  inner_strategy:
    strategy: "cheapest"
    max_price: null
  vfan_aware:
    code: "ABCD1234"
```

Empty / whitespace codes are rejected at construction.

---

## Empty pages

Every strategy returns `None` when `list_candidates(page)` yields no
rows (`dom.empty-candidates-none` assertion). The fixture for the
empty path is
`tests/fixtures/strategies/empty_quick_picks.html`.

## Adding a new strategy

1. Subclass `SelectionStrategy` in a new file under `src/strategies/`.
2. Implement `async def pick(self, page)`; use
   `await self.list_candidates(page)` for the base parsing and
   `await self.click_candidate(candidate, page)` for stale-locator-
   safe clicks.
3. Read any new DOM selectors via
   `from src.registry.selectors import locator, locator_multi` —
   never inline `data-bdd=` strings in Python. Add the fallback list
   under a new logical name in `config/selectors/ticketmaster.yaml`.
4. Register the class in `src/strategies/factory.py`
   (`_register_default_strategies`) so it is reachable via
   `tickets.strategy: <name>` in `config/config.yaml`.
5. Add a real-Chromium DOM test against a new fixture HTML file under
   `tests/fixtures/strategies/`.

External packages may register strategies via the
`ticketmaster_bot.strategies` entry-point group without touching the
factory; discovery happens lazily on the first registry read.
