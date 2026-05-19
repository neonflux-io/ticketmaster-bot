# Ticketmaster Bot

A modular, fully configurable Python bot that automates the Ticketmaster
ticket-buying flow using Playwright browser automation. Login, on-sale polling,
the virtual waiting room, ticket selection, add-to-cart, and (optionally)
end-to-end checkout are each their own swappable subsystem.

> **DISCLAIMER**. This tool is for educational and personal use only.
> Automating purchases on Ticketmaster may violate their Terms of Service
> and the US BOTS Act. Use at your own risk. Do not use to scalp tickets.

## Feature matrix

| Subsystem | What ships | Source | Docs |
| --- | --- | --- | --- |
| Vendor adapter | `TicketmasterAdapter` (`auth` → `navigator` → `queue` → `cart` → `checkout`); plugin loader for additional vendors | `src/vendors/`, `src/vendors/ticketmaster/` | [docs/vendors.md](docs/vendors.md), [docs/architecture.md](docs/architecture.md) |
| Selection strategies | `cheapest`, `best_available`, `section_target`, `price_range`, `multi_section`, `accessible`, `seat_quality`, `random_pick`, `composite`, `interactive_seatmap`, `resale_filter` (wrapper), `vfan_aware` (wrapper) | `src/strategies/`, `src/strategies/factory.py` | [docs/strategies.md](docs/strategies.md) |
| Notifiers | desktop toast, generic webhook, Discord embeds, Slack, Telegram (MarkdownV2), fan-out via `MultiplexNotifier` | `src/notifiers/`, `src/notifiers/multiplex.py` | [docs/notifiers.md](docs/notifiers.md) |
| Anti-detection | Cubic-Bezier mouse moves, normally-distributed `human_type`, pre-flight warmer, mobile/desktop profile, stealth patches | `src/humanize/`, `src/utils/stealth.py` | [docs/humanize.md](docs/humanize.md) |
| Per-account proxy | Sticky or round-robin URL assignment, threaded into `launch_persistent_context` | `src/proxy/manager.py` | [docs/humanize.md](docs/humanize.md) |
| Parallel multi-account | `ParallelCoordinator` races N runners with staggered launch, stop-on-first-success | `src/orchestrator/parallel.py` | [docs/parallel.md](docs/parallel.md) |
| Hooks | `Hook` ABC, lifecycle dispatcher, `ScreenshotOnFailureHook`, `SlowDownAfterFailureHook` | `src/hooks/`, `src/orchestrator/lifecycle.py` | [docs/hooks.md](docs/hooks.md) |
| Observability | Per-run artefact dir (PNG + DOM + console + HAR), JSON logs, aiosqlite history, Prometheus `/metrics`, FastAPI `/status`+`/logs/tail`+`/stop` | `src/utils/run_artifacts.py`, `src/utils/history.py`, `src/observability/metrics.py`, `src/observability/control.py` | [docs/observability.md](docs/observability.md) |
| Layered config | Defaults → `--profile` overlay → `${VAR}` env expansion → `--set key.path=value` CLI, plus per-event arrays | `src/utils/config_loader.py`, `config/profiles/` | [docs/architecture.md](docs/architecture.md) |
| Selector registry | Logical-name → fallback-list YAML, accessed via `locator()` helper; zero inline selectors in source | `src/registry/selectors.py`, `config/selectors/ticketmaster.yaml` | [docs/architecture.md](docs/architecture.md) |
| CLI | `src/cli.py` (argparse) → `src/main.py` (bootstrap) | `src/cli.py`, `src/main.py` | [docs/architecture.md](docs/architecture.md) |

## Quickstart

```bash
# 1. Set up the virtualenv and Playwright.
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# 2. Configure the event and credentials.
cp config/accounts.yaml.example config/accounts.yaml && $EDITOR config/accounts.yaml
$EDITOR config/config.yaml   # set the event URL and your preferences

# 3. Validate the config without launching a browser.
python run.py --dry-run

# 4. Print the fully-resolved config (defaults + profile + --set overlays).
python run.py --explain

# 5. First real run — keep auto-purchase off and the browser visible.
python run.py --no-headless --no-auto-purchase
```

`${VAR}` placeholders in `config/accounts.yaml` are expanded from the
environment, including `.env` (auto-loaded via `python-dotenv`). The fallback
env vars `TM_EMAIL` / `TM_PASSWORD` activate when no `accounts.yaml` is
present.

## CLI reference

Every flag below is declared in `src/cli.py` and documented in that
module's docstring. The `--dry-run` and `--explain` paths never launch a
browser.

```bash
# I/O paths
python run.py --config config/config.yaml
python run.py -c config/config.yaml --accounts config/accounts.yaml
python run.py -a config/accounts.yaml --account-name primary

# Vendor selection (currently only "ticketmaster" ships; --vendor nope -> exit 2)
python run.py --vendor ticketmaster

# Layered config overlays
python run.py --profile fast                                # apply config/profiles/fast.yaml
python run.py --profile safe --set checkout.auto_purchase=false
python run.py --set tickets.quantity=4 --set tickets.max_price=300
python run.py --events https://www.ticketmaster.com/event/A --events https://...B

# Browser / checkout overrides
python run.py --headless                                    # force headless
python run.py --no-headless                                 # force visible
python run.py --auto-purchase                               # force final-click (DANGEROUS)
python run.py --no-auto-purchase                            # force stop-at-cart

# Parallel multi-account orchestration
python run.py --parallel --max-parallel 3 --stagger 5

# Validation-only paths
python run.py --dry-run                                     # exit 0/2
python run.py --explain                                     # print resolved YAML, exit 0
```

`--set` accepts repeated `key.path=value` overrides and coerces booleans
(`true|false`), integers, floats, and `null` from the string form. `--events`
accepts repeated values; each value may itself be a comma-separated list, so
both `--events A --events B` and `--events A,B` work.

## Configuration

The configuration schema lives in `src/utils/config_loader.py` and is
authoritative: every shipped knob is a field on `BotConfig` or one of its
nested dataclasses. Top-level groups:

- `events` — list of `{url, on_sale_time, refresh_interval_seconds,
  strict_host}` entries. The legacy `event: {...}` form still loads and is
  normalised into a one-element `events` list at load time.
- `tickets` — `quantity` (1-8), `strategy` (one of the names in the matrix
  above), plus per-strategy sub-blocks `section_target`, `price_range`,
  `multi_section`, `resale_filter`, `vfan_aware`, and a nested
  `inner_strategy` block for wrapper strategies.
- `checkout` — `auto_purchase`, `price_tolerance`, `payment.card_last_four`,
  `delivery.preferred`, `delivery.allow_any`.
- `timing` — `page_timeout_seconds`, `queue_check_interval_seconds`,
  `action_delay_seconds: [min, max]`, `max_total_runtime_seconds`,
  `hold_open_seconds`, and the nested `humanize` block (`enabled`, `mouse`,
  `typing`).
- `browser` — `headless`, `slow_mo_ms`, `user_data_dir`, `locale`,
  `timezone`, `profile` (`desktop` or `mobile`), and the nested `stealth`
  block.
- `logging` — `level`, `file`, `format` (`rich` or `json`), and
  `logging.artifacts.record_har`.
- `notifications` — `desktop`, `sound`.
- `proxy` — `enabled`, `policy` (`sticky` or `round_robin`), `urls`.
- `accounts` — list of `{email, password, name}`; values may contain
  `${VAR}` placeholders.

Profile YAMLs in `config/profiles/` (`fast.yaml`, `safe.yaml`) overlay a
named subset of these keys when `--profile` is passed. See
[docs/architecture.md](docs/architecture.md#layered-config-pipeline) for the
overlay order.

## How a run flows

```
INIT → LOGIN → NAVIGATE → [WAIT_SALE | QUEUE] → SELECT → CART → CHECKOUT → DONE
```

Each step is implemented in its own module under
`src/vendors/ticketmaster/`. The runner in `src/vendors/ticketmaster/core.py`
fires lifecycle events (declared in `src/orchestrator/lifecycle.py`) at every
transition so registered hooks observe — and can react to — every step.

1. **INIT** — `src/utils/config_loader.py::load_config` resolves the
   overlays, then `src/main.py` instantiates the vendor adapter from the
   `--vendor` flag.
2. **LOGIN** — `src/vendors/ticketmaster/auth.py` reuses the persistent
   Chromium profile under `sessions/` when cookies are valid; otherwise it
   types credentials (humanised when `timing.humanize.typing.enabled`) and
   pauses for any captcha challenge.
3. **NAVIGATE** — `src/vendors/ticketmaster/navigator.py` opens the event
   URL and polls for tickets going on sale. Naive `event.on_sale_time`
   values are localised using `browser.timezone`.
4. **WAIT_SALE / QUEUE** — `src/vendors/ticketmaster/queue.py` detects the
   virtual waiting room and waits for release **without re-`goto`ing** the
   event URL (which would invalidate the queue token).
5. **SELECT** — the configured `SelectionStrategy` (see
   [docs/strategies.md](docs/strategies.md)) inspects the DOM via the
   selector registry and clicks one row.
6. **CART** — `src/vendors/ticketmaster/cart.py` sets the quantity, ticks
   the *terms* checkbox (never marketing opt-ins), and clicks Add to Cart.
7. **CHECKOUT** — `src/vendors/ticketmaster/checkout.py` picks a preferred
   delivery option, selects a saved card, and (when `checkout.auto_purchase`
   is true) re-reads the order summary to verify section/quantity/price
   before clicking Place Order.

When the browser is visible and `checkout.auto_purchase` is false the
window is held open for `timing.hold_open_seconds` (default 600 s) so you
can finish checkout yourself.

## Project layout

```
ticketmaster-bot/
├── run.py                            # Top-level CLI entry point
├── src/
│   ├── cli.py                        # argparse surface (all flags)
│   ├── main.py                       # bootstrap: config → vendor → runner
│   ├── registry/                     # Generic Registry[T] + 5 instances
│   ├── vendors/
│   │   ├── base.py                   # VendorAdapter ABC
│   │   └── ticketmaster/             # auth/navigator/queue/cart/checkout/core
│   ├── strategies/                   # 12 SelectionStrategy implementations
│   ├── notifiers/                    # desktop/webhook/discord/slack/telegram + multiplex
│   ├── humanize/                     # mouse/typing/warmer/profile
│   ├── proxy/                        # per-account proxy manager
│   ├── hooks/                        # Hook ABC + built-in hooks
│   ├── orchestrator/                 # lifecycle dispatcher + ParallelCoordinator
│   ├── observability/                # Prometheus metrics + FastAPI control panel
│   └── utils/                        # config_loader, logger, stealth, run_artifacts, history
├── config/
│   ├── config.yaml                   # Main runtime config
│   ├── profiles/                     # fast.yaml, safe.yaml
│   ├── selectors/ticketmaster.yaml   # Logical-name → fallback list
│   └── accounts.yaml.example         # Copy to accounts.yaml (gitignored)
├── docs/                             # One subsystem doc per directory above
├── tests/                            # pytest + pytest-asyncio + real Chromium
├── sessions/                         # Playwright persistent profiles (gitignored)
├── logs/                             # Runtime logs + per-run artefact dirs (gitignored)
├── requirements.txt
├── pyproject.toml
└── README.md
```

## Subsystems

### Selection strategies

12 strategies ship out of the box, registered with the singleton in
`src/registry/strategies.py` and built from `tickets:` by
`src/strategies/factory.py::build_strategy`. Wrapper strategies
(`resale_filter`, `vfan_aware`) delegate to a leaf strategy defined under
`tickets.inner_strategy`. See [docs/strategies.md](docs/strategies.md) for
the per-strategy YAML examples.

### Notifiers

`MultiplexNotifier` (`src/notifiers/multiplex.py`) fans events out to any
combination of `desktop`, `webhook`, `discord`, `slack`, and `telegram`.
Channels are constructed via the registry in `src/registry/notifiers.py`
from the `notifications:` block; per-channel failures are isolated.
Per-channel payload shapes (Discord embeds, Telegram MarkdownV2, etc.) are
catalogued in [docs/notifiers.md](docs/notifiers.md).

### Vendors

Only the Ticketmaster adapter ships today. Adding a new vendor is a
one-file drop-in: subclass `VendorAdapter` (`src/vendors/base.py`),
return a runner with the per-step modules wired up, and register the
class either via the `ticketmaster_bot.vendors` entry-point group or by
calling `src.registry.vendors.register(name, cls)` at import time.
See [docs/vendors.md](docs/vendors.md) for the worked example.

### Hooks

Every runner owns a `HookRegistry` (`src/hooks/__init__.py`). The
lifecycle event names live in `src/orchestrator/lifecycle.py`; a hook
that raises is logged and skipped so it never aborts the run.
`ScreenshotOnFailureHook` and `SlowDownAfterFailureHook` ship out of the
box; [docs/hooks.md](docs/hooks.md) walks through writing your own.

### Humanisation + stealth

`src/humanize/mouse.py::bezier_move` walks the cursor along a cubic
Bezier with normally-distributed delays; `src/humanize/typing.py` types
character-by-character; `src/humanize/warmer.py` performs a pre-flight
visit pattern; `src/humanize/profile.py` swaps between `desktop` and
`mobile` fingerprints. `src/utils/stealth.py` applies the
`navigator.webdriver` / WebGL / plugins / languages patches. See
[docs/humanize.md](docs/humanize.md) for tuning knobs.

### Parallel multi-account

`src/orchestrator/parallel.py::ParallelCoordinator` races up to
`--max-parallel` runners, staggered by `--stagger` seconds; the first
runner whose `run()` returns `True` sets a shared `asyncio.Event` and
the coordinator cancels the rest. See
[docs/parallel.md](docs/parallel.md) for the race semantics and the
interaction with `src/proxy/manager.py`.

### Observability

`logs/run-<UTC>-<account>/` collects `screenshot.png`, `dom.html`,
`console.log`, and `network.har` on failure. The Prometheus exporter
(`src/observability/metrics.py`) and the FastAPI control panel
(`src/observability/control.py`, exposing `/status`, `/logs/tail`, and
`/stop`) run side-by-side. Run history is persisted to an aiosqlite DB
via `src/utils/history.py`. See
[docs/observability.md](docs/observability.md) for endpoint shapes and
schema.

## Development

```bash
.venv/bin/python -m pytest                                  # full suite
.venv/bin/python -m pytest -n 8                             # parallel unit
.venv/bin/python -m pytest -n 4 tests/integration           # parallel integration (real Chromium)
.venv/bin/python -m ruff check src tests                    # lint
.venv/bin/python -m ruff format src tests                   # format
.venv/bin/python -m mypy src --strict-optional              # type check
```

Tests drive real headless Chromium against the HTML fixtures under
`tests/fixtures/`. Outbound notifier tests hit real endpoints when their
credentials are present in `.env` (Discord/Telegram/webhook) and are
`pytest.skip`-ed cleanly otherwise. See `AGENTS.md` for the full
contributor checklist (commit conventions, recipes for adding strategies
/ notifiers / hooks / vendors, and the mock/stub audit gate).

## Security and configuration tips

- Credentials live in `.env` or `config/accounts.yaml` (both gitignored).
  Reference env vars in YAML with `${VAR_NAME}`.
- Keep `checkout.auto_purchase: false` until a flow is verified end-to-end.
- Persistent Chromium profiles in `sessions/` contain auth cookies — treat
  them as secrets. Stealth (`src/utils/stealth.py`) removes the most
  obvious automation tells, but modern bot detection (Imperva, Datadome,
  Akamai) has many other signals. Run from a residential IP, use a real
  account with purchase history, and don't fan out parallel sessions from
  the same IP.

## Troubleshooting

- **Login fails / treated as logged out repeatedly** — run with
  `browser.headless: false`, log in once manually, and the persistent
  session in `sessions/` will reuse cookies.
- **Bot can't find tickets** — selectors may have changed. Set
  `logging.level: DEBUG`, inspect `logs/bot.log`, and update the
  fallback list in `config/selectors/ticketmaster.yaml`.
- **Captcha during cart/checkout** — the bot pauses for up to 5 minutes
  while you solve it in the visible browser.
- **`event.on_sale_time` rejected as naive** — either include a
  timezone offset (`2026-06-01T10:00:00-05:00`) or set
  `browser.timezone` so the loader can localise it.

## License

For personal/educational use. No warranty of any kind.
