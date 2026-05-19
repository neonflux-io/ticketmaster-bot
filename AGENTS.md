<coding_guidelines>
# Repository Guidelines

A Playwright-based Python bot that automates Ticketmaster ticket purchasing.
This document gives contributors and AI assistants the conventions and
commands needed to work productively in this repo.

## Project Structure & Module Organization

```
ticketmaster-bot/
├── run.py                           # Top-level CLI entry point
├── src/
│   ├── cli.py                       # argparse surface (every CLI flag lives here)
│   ├── main.py                      # bootstrap: config → vendor → runner
│   ├── registry/                    # Registry[T] + 5 instances
│   ├── vendors/
│   │   ├── base.py                  # VendorAdapter ABC
│   │   ├── ticketmaster/            # US/CA: auth/navigator/queue/cart/checkout/core
│   │   └── ticketmaster_sg/         # SG: SG-scoped selectors + SGD price + Queue-It
│   ├── strategies/                  # SelectionStrategy implementations
│   ├── notifiers/                   # desktop/webhook/discord/slack/telegram + multiplex
│   ├── humanize/                    # mouse/typing/warmer/profile
│   ├── proxy/                       # per-account proxy manager
│   ├── hooks/                       # Hook ABC + screenshot_on_failure + slow_down_after_failure
│   ├── orchestrator/                # lifecycle dispatcher + ParallelCoordinator
│   ├── observability/               # Prometheus metrics + FastAPI control panel
│   └── utils/                       # config_loader, logger, stealth, run_artifacts, history
├── config/
│   ├── config.yaml                  # Main runtime config (committed)
│   ├── profiles/                    # fast.yaml, safe.yaml
│   ├── selectors/
│   │   ├── ticketmaster.yaml        # US/CA logical-name → fallback list
│   │   └── ticketmaster_sg.yaml     # SG logical-name → fallback list
│   └── accounts.yaml.example        # Credential template (real file gitignored)
├── docs/                            # One subsystem doc per directory
├── tests/                           # pytest + pytest-asyncio + real Chromium
├── sessions/                        # Playwright persistent profiles (gitignored)
├── logs/                            # Runtime logs + per-run artefact dirs (gitignored)
└── requirements.txt
```

State flow lives in `src/vendors/ticketmaster/core.py`; each step (login,
queue, select, cart, checkout) is its own module in the same directory.
The legacy `src/bot/` package is a thin re-export shim over
`src/vendors/ticketmaster/` and is kept only for backwards compatibility.

The Singapore adapter at `src/vendors/ticketmaster_sg/` is the second
shipped vendor and the canonical worked example for adding a new
vendor: it subclasses `BotRunner` from
`src/vendors/ticketmaster/core.py` to inherit the state machine and
only overrides the per-step modules + selectors. SG-specific behaviours
(SGD price parser, Yii image CAPTCHA + invisible reCAPTCHA Enterprise
handling, Queue-It customer id `ticketmasterasia`, SG OAuth via
`client_id=...tmsg`) live in their own modules under
`src/vendors/ticketmaster_sg/`. See [docs/vendors.md](docs/vendors.md)
and [docs/vendors_ticketmaster_sg.md](docs/vendors_ticketmaster_sg.md)
for the full story.

Vendor selection is automatic from the first event URL's host:
`ticketmaster.sg` → `ticketmaster_sg`, anything else → `ticketmaster`.
`--vendor <name>` overrides the auto-detection.

## Build, Test, and Development Commands

```bash
python3 -m venv .venv && source .venv/bin/activate   # set up venv
pip install -r requirements.txt                      # install deps
playwright install chromium                          # one-time browser install
python run.py                                        # run with default config
python run.py --explain                              # print resolved YAML, exit 0
python run.py --dry-run                              # validate config, exit 0
python run.py --profile fast --set tickets.quantity=4
python run.py --parallel --max-parallel 3 --stagger 5
```

`--set key.path=value` overlays repeat; `--events URL` overlays repeat and
also accept comma-separated values. Every flag is declared in `src/cli.py`
— if you add a new one, document it both there (module docstring) and in
the README's CLI reference table.

## Coding Style & Naming Conventions

- Python 3.10+; 4-space indentation; type hints required on public functions.
- Use `from __future__ import annotations` at the top of new modules.
- `snake_case` for functions/variables, `PascalCase` for classes, `UPPER_SNAKE` for constants.
- Module names lowercase, single word where possible (`cart.py`, `queue.py`).
- Async functions use the `async def` form; await all I/O.
- Log via `logging.getLogger("ticketmaster-bot")` — never `print()`.
- **No inline selectors in `src/`**. Every CSS / XPath string lives in
  `config/selectors/ticketmaster.yaml` and is accessed via
  `src/registry/selectors.py::locator`. A CI grep enforces this:
  `rg "data-bdd=" src/ --type py` must return zero matches.
- **No mocks, no stubs**. The repo bans `unittest.mock`, `MagicMock`,
  `FakePage`, `FakeLocator`, `NotImplementedError`, and `pass # stub` in
  production code. Tests drive real Chromium against `tests/fixtures/*.html`.

## Testing Guidelines

- Unit tests live under `tests/` and run via `pytest` + `pytest-asyncio`
  (asyncio mode = auto, see `pyproject.toml`). Name files `test_<module>.py`.
- All Playwright tests use real headless Chromium against
  `tests/fixtures/*.html`. The shared `chromium` and `chromium_context`
  fixtures live in `tests/conftest.py`.

### Required validators

```bash
# Full test suite, serialized (~40 s on a modern laptop).
.venv/bin/python -m pytest

# Parallel unit run, 8 workers.
.venv/bin/python -m pytest -n 8

# Parallel integration run, 4 workers (real Chromium safe).
.venv/bin/python -m pytest -n 4 tests/integration

# Focused on a single file.
.venv/bin/python -m pytest tests/test_readme.py -v

# Lint + format.
.venv/bin/python -m ruff check src tests --fix
.venv/bin/python -m ruff format src tests

# Type check (strict-optional).
.venv/bin/python -m mypy src --strict-optional

# CLI smoke.
.venv/bin/python run.py --dry-run
.venv/bin/python run.py --explain
```

### Pre-commit audits

```bash
# Mock/stub gate (must return OK).
git diff --staged | rg -i '\b(todo|fixme|xxx|hack|stub)\b|notimplementederror|pass\s*#\s*stub|unittest\.mock|magicmock|fakepage|fakelocator' || echo OK

# Inline-selector gate.
rg "data-bdd=" src/ --type py

# Orphan-process gate (must print 0).
pgrep -f 'chromium|headless_shell|uvicorn' | wc -l
```

## Commit & Pull Request Guidelines

- Commit messages: imperative mood, ≤72 chars, optional scope prefix
  (`bot:`, `strategies:`, `config:`, `notifiers:`, `humanize:`, `proxy:`,
  `parallel:`, `hooks:`, `observability:`, `cli:`, `docs:`, `tests:`).
  Example: `bot: handle queue timeout gracefully`.
- One logical change per commit; rebase before opening a PR.
- PRs must include: summary, config/selector changes called out, manual-test
  notes, and any updated screenshots of the flow if UI selectors changed.
- Never commit `config/accounts.yaml`, `.env`, `sessions/`, or `logs/`.

## Recipes

### Adding a strategy

1. Create `src/strategies/<name>.py` exposing a class that subclasses
   `SelectionStrategy` (`src/strategies/base.py`) and implements
   `async def pick(self, page) -> TicketCandidate | None`.
2. Register it in `src/strategies/factory.py::_register_default_strategies`
   so `tickets.strategy: <name>` resolves at config load.
3. Extend `tickets:` parsing in `src/utils/config_loader.py` if the
   strategy needs new YAML keys (add a small dataclass alongside
   `PriceRangeConfig` etc. and update `_parse_tickets`).
4. Add fallback selectors to `config/selectors/ticketmaster.yaml` (the
   strategy must look them up via `locator()` — no inline strings).
5. Add a real-Chromium test in `tests/strategies/test_<name>.py` that
   drives a fixture HTML file under `tests/fixtures/strategies/`.
6. Document the strategy with a YAML snippet in `docs/strategies.md`.

### Adding a notifier

1. Create `src/notifiers/<channel>.py` exposing a class that subclasses
   `Notifier` (`src/notifiers/base.py`) and implements
   `async def notify(self, event: NotifyEvent) -> None`.
2. Register it in `src/notifiers/__init__.py::_register_default_notifiers`
   so the channel name resolves in
   `MultiplexNotifier.from_config` (`src/notifiers/multiplex.py`).
3. Build the outbound payload with `httpx.AsyncClient`; reuse the existing
   retry policy (4xx → no retry, 5xx → bounded retries).
4. Add the channel to the `notifications.channels` and (optionally)
   `notifications.routing` shape in `config/config.yaml`, and document
   the YAML in `docs/notifiers.md`.
5. Add a real-outbound test under `tests/notifiers/test_<channel>_live.py`
   that `pytest.skip`s cleanly when the required env var (e.g.
   `DISCORD_WEBHOOK_URL`) is absent, and a real-local-server retry test
   under `tests/notifiers/test_<channel>_retry.py`.

### Adding a hook

1. Create `src/hooks/<event_or_purpose>.py` exposing a class that
   subclasses `Hook` (`src/hooks/base.py`). Override only the lifecycle
   methods you care about; the base implementation is a no-op.
2. Set `priority` if order matters (lower fires first; ties broken by
   registration order).
3. Register the hook on the runner's `HookRegistry` from
   `src/main.py` or a vendor adapter — built-in hooks register
   themselves in `src/hooks/__init__.py`.
4. Lifecycle event names are declared in `src/orchestrator/lifecycle.py`.
   The dispatcher isolates per-hook exceptions, so a raising hook never
   aborts the run.
5. Add a pytest test under `tests/hooks/` that asserts the hook fires
   the expected side effects (e.g. file written, runner attribute
   mutated).
6. Document the hook in `docs/hooks.md`.

### Adding a vendor

`src/vendors/ticketmaster_sg/` is the canonical second-vendor example —
mirror it for any new adapter. The Singapore adapter inherits its
state machine from the US `BotRunner` and only overrides the per-step
modules + selectors, which is the minimum surface a new vendor needs to
re-implement.

1. Create `src/vendors/<vendor>/` mirroring the layout of
   `src/vendors/ticketmaster_sg/` (or `src/vendors/ticketmaster/` if
   the new vendor's state machine truly diverges): at minimum,
   `adapter.py` (an implementation of `VendorAdapter` from
   `src/vendors/base.py`) and per-step modules (`auth.py`,
   `navigator.py`, `queue.py`, `cart.py`, `checkout.py`, `core.py`).
2. `VendorAdapter.build_runner(config, account)` should return a runner
   exposing `async run() -> bool`. The state machine pattern in
   `src/vendors/ticketmaster/core.py` is the worked example; the SG
   adapter at `src/vendors/ticketmaster_sg/core.py` shows how to
   subclass it and only swap the per-step modules.
3. Register the adapter in `src/vendors/<vendor>/__init__.py` via
   `src.registry.vendors.register("<vendor>", <Adapter>)`. Importing the
   subpackage from `src.vendors/__init__.py` registers it eagerly; the
   `ticketmaster_bot.vendors` entry-point group is the external plugin
   path.
4. Add the vendor's selectors to `config/selectors/<vendor>.yaml` and
   load them either through the shared registry in
   `src/registry/selectors.py` or, when logical-name collisions with
   another vendor's YAML are a risk, via a vendor-scoped helper
   (`src/vendors/<vendor>/selectors.py`) — see
   `src/vendors/ticketmaster_sg/selectors.py` for the pattern.
5. Add an `src/vendors/<vendor>/` test directory (`tests/vendors/<vendor>/`)
   with at least one real-Chromium end-to-end smoke against a fixture
   HTML file. Mirror `tests/vendors/ticketmaster_sg/` for the layout
   (`test_adapter.py`, `test_auth.py`, `test_cart.py`,
   `test_checkout.py`, `test_navigator.py`, `test_queue.py`,
   `test_recon_artifacts.py`).
6. Document the adapter in `docs/vendors.md`, including the per-step
   responsibilities and any non-Ticketmaster YAML keys. Add an
   operator-facing user guide at
   `docs/vendors_<vendor>.md` (see
   [docs/vendors_ticketmaster_sg.md](docs/vendors_ticketmaster_sg.md)
   for the model).
7. If the new vendor is selected by a distinct host, add its
   `(host_suffix, vendor_name)` pair to `_HOST_VENDOR_MAP` in
   `src/main.py` so the bot auto-detects it from the event URL.

## Security & Configuration Tips

- Credentials belong in `.env` or `config/accounts.yaml` (both gitignored);
  reference env vars in YAML with `${VAR_NAME}`.
- Keep `checkout.auto_purchase: false` until a flow is verified end-to-end.
- Persistent Chromium profiles in `sessions/` contain auth cookies — treat
  as secrets.
- The reserved ports for production observability are `9000` (Prometheus
  `/metrics`) and `9051` (FastAPI control panel). Tests bind ephemeral
  ports (`socket.bind(("127.0.0.1", 0))`) to avoid clashes.
</coding_guidelines>
