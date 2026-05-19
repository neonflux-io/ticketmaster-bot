# Architecture

This document describes the runtime architecture of `ticketmaster-bot`
as it ships today: the per-account state machine, the plugin
registries, the layered configuration pipeline, and the orchestration
layer that races multiple accounts in parallel.

## High-level layout

The codebase is partitioned along four axes that map directly to the
directory layout under `src/`:

| Concern | Module |
| --- | --- |
| Vendor adapter (per-step modules + runner) | `src/vendors/ticketmaster/` |
| Plugin registries (strategies, notifiers, hooks, vendors, selectors) | `src/registry/` |
| Selection strategies (pure logic over the DOM) | `src/strategies/` |
| Outbound notifications | `src/notifiers/` |
| Anti-detection humanisation primitives | `src/humanize/` |
| Hooks + lifecycle dispatcher | `src/hooks/`, `src/orchestrator/lifecycle.py` |
| Parallel multi-account orchestration | `src/orchestrator/parallel.py` |
| Per-account proxy assignment | `src/proxy/manager.py` |
| Observability (metrics + control panel + history + artefacts) | `src/observability/`, `src/utils/run_artifacts.py`, `src/utils/history.py` |
| Layered config + CLI entry point | `src/utils/config_loader.py`, `src/cli.py`, `src/main.py` |

Every concern below is registered with a typed `Registry[T]` defined in
`src/registry/base.py` so external plugins discovered via Python
entry-points slot in without code changes.

## Per-account state machine

The end-to-end purchase flow for a single account is implemented by
`BotRunner` in `src/vendors/ticketmaster/core.py`. Each step is its own
module under `src/vendors/ticketmaster/` so the state machine reads as
a sequence of small, replaceable functions:

```
                    on_run_start (LifecycleDispatcher.fire)
                              │
                              ▼
                ┌──────────────────────────┐
                │ launch_persistent_context│  ← src/vendors/ticketmaster/core.py
                └──────────────────────────┘
                              │
       ┌──────────────────────┴──────────────────────┐
       │ stealth: apply_stealth(context, …)          │  ← src/utils/stealth.py
       │ profile: apply_profile(name, options)        │  ← src/humanize/profile.py
       │ proxy:   ProxyManager(config).resolve(name)  │  ← src/proxy/manager.py
       └──────────────────────┬──────────────────────┘
                              ▼
   before_login → auth.login(...) → after_login                     ← src/vendors/ticketmaster/auth.py
                              │
   before_navigate → navigator.open_event(...) → wait_until_on_sale ← src/vendors/ticketmaster/navigator.py
                              │
   ┌──────────── queue.wait_through_queue ────────────┐             ← src/vendors/ticketmaster/queue.py
   │                                                  │
   ▼                                                  ▼
 sold_out / not_on_sale ────────────────► return False
                              │
   after_queue_release
                              │
   before_select → strategy.pick(page) → after_select               ← src/strategies/*
                              │
   before_cart → cart.set_quantity + cart.add_to_cart → after_cart  ← src/vendors/ticketmaster/cart.py
                              │
   before_checkout → checkout.run_checkout → before_place_order     ← src/vendors/ticketmaster/checkout.py
                              │
                              ▼
                      on_run_end (always fires)
                              │
                              ▼
              context.close() (HAR finalised here)
```

Every named transition above is one of the entries in
`LIFECYCLE_EVENTS` defined in `src/orchestrator/lifecycle.py`; the
`LifecycleDispatcher` skips unknown event names so a typo at a call
site fails loudly rather than silently dropping the event. Hooks
registered on the runner's `HookRegistry` (`src/hooks/base.py`) observe
every transition.

## Registries

All long-lived collections (strategies, notifiers, hooks, vendors,
selectors) share a single generic implementation. The contract:

- `register(name, obj)` raises `DuplicateRegistration` from
  `src/registry/base.py` on a second registration of the same name.
- `get(name)` raises `NotRegistered` whose message contains the missing
  name (used by the `refactor.registry-missing` assertion).
- Discovery walks `importlib.metadata.entry_points(group=...)` lazily
  on first read; failures during one plugin's `entry.load()` are
  logged but do not abort discovery for siblings.

| Registry | Module | Entry-point group |
| --- | --- | --- |
| Strategies | `src/registry/strategies.py` | `ticketmaster_bot.strategies` |
| Notifiers | `src/registry/notifiers.py` | `ticketmaster_bot.notifiers` |
| Hooks | `src/registry/hooks.py` | `ticketmaster_bot.hooks` |
| Vendors | `src/registry/vendors.py` | `ticketmaster_bot.vendors` |
| Selectors | `src/registry/selectors.py` | `ticketmaster_bot.selectors` |

The Selector registry stores **lists of CSS/XPath fallbacks** rather
than a pre-joined string; helpers `locator`, `locator_multi`, and
`locator_template` in `src/registry/selectors.py` join them with `", "`
at call time so Playwright treats the result as a CSS-or selector.
Templates are interpolated via `str.format_map` with CSS attribute
values escaped first so user-supplied values cannot break the
selector.

## Hooks + lifecycle

`Hook` (`src/hooks/base.py`) is the ABC every observer implements. Each
runner owns its own `HookRegistry` so multi-account / parallel runs
never share hook state. Registered hooks are sorted by integer
`priority` (lower first) with a stable tie-breaker on registration
order. The dispatcher in `src/orchestrator/lifecycle.py` iterates them
on every fire and isolates exceptions: a hook that raises is logged
and skipped, never aborts the runner. Built-in hooks:

- `ScreenshotOnFailureHook` (`src/hooks/screenshot_on_failure.py`) —
  dumps `screenshot.png`, `dom.html`, and `console.log` to a per-run
  artefact directory on the `on_failure` event.
- `SlowDownAfterFailureHook` (`src/hooks/slow_down_after_failure.py`)
  — multiplies `action_delay_min`/`action_delay_max` on the runner
  after every failure.

See `docs/hooks.md` for the full lifecycle catalogue and how to add a
custom hook.

## Layered config pipeline

`src/utils/config_loader.py` resolves configuration in five overlays,
applied left-to-right (later wins):

```
defaults
  └→ profile (config/profiles/<name>.yaml)
      └→ events array (per-event scoped overrides)
          └→ env (${VAR_NAME} substitution)
              └→ --set key.path=value (CLI)
```

`config/config.yaml` carries the in-repo defaults; profiles such as
`config/profiles/fast.yaml` and `config/profiles/safe.yaml` overlay
specific keys on top. The `--explain` flag in `src/cli.py` re-emits the
fully merged result as YAML on stdout without launching a browser, so
operators can preview the effective config before a run.

## CLI surface

`src/cli.py` owns every flag declaration. `src/main.py` is the bootstrap
that:

1. Calls `build_parser().parse_args()` from `src/cli.py`.
2. Loads + merges configuration via `src/utils/config_loader.py::load_config`.
3. Resolves the vendor adapter from `src/registry/vendors.py`
   (`--vendor`).
4. Either prints the merged config (`--explain`), exits after
   validation (`--dry-run`), runs a single account, or hands control to
   `ParallelCoordinator` in `src/orchestrator/parallel.py` when
   `--parallel` is set.

The full flag list is documented in `src/cli.py`'s module docstring and
the `README.md`.

## Parallel orchestration

`ParallelCoordinator.run()` in `src/orchestrator/parallel.py` races up
to `max_parallel` per-account runners, applies a `stagger_seconds`
launch delay (skipping the stagger for the first runner so the only-
account-running case has no extra latency), and shares an
`asyncio.Event` (`stop_event`). The first runner whose `run()` returns
`True` sets the event; the coordinator immediately cancels every
sibling task and waits up to 10 s for their `try / finally` blocks to
close their Chromium contexts cleanly. See `docs/parallel.md` for the
race semantics and the proxy-manager interaction.

## Observability surface

Three independent observability layers run side-by-side:

- **Run artefacts** (`src/utils/run_artifacts.py`): a per-run directory
  `logs/run-<UTC>-<account>/` collects the failure screenshot, DOM
  dump, console log, and HAR. The HAR is wired into Playwright via
  `har_kwargs(...)` at `launch_persistent_context` time so the HAR is
  produced live, not reconstructed after the fact.
- **Metrics** (`src/observability/metrics.py`): Prometheus counters /
  histogram exposed via an ASGI app running under `uvicorn`. The
  default port is `9000`; tests bind to an ephemeral port.
- **Control panel** (`src/observability/control.py`): a FastAPI app
  serving `/status`, `/logs/tail`, and `/stop`. Default port `9051`.

The two HTTP servers boot via `asyncio.create_task` so they live inside
the main process and tear down via `should_exit = True` + a
`serve_task` await.

## Notifier fan-out

`src/notifiers/multiplex.py` (`MultiplexNotifier`) is the single entry
point the runner pushes events through. Its routing table is read from
`notifications.routing` in `config/config.yaml`; the table maps
`event_type` to a list of channel names. Each channel name resolves to
a concrete `Notifier` constructed by `MultiplexNotifier.from_config`
via `src/registry/notifiers.py`. Channels run concurrently; one
channel's failure is isolated from siblings via `_dispatch_one` which
swallows non-`CancelledError` exceptions. See `docs/notifiers.md` for
the channel matrix and payload shapes.

## File-by-file index of architectural touch-points

- `src/main.py` — top-level bootstrap.
- `src/cli.py` — argparse surface.
- `src/utils/config_loader.py` — layered config resolution.
- `src/vendors/base.py` — `VendorAdapter` ABC.
- `src/vendors/ticketmaster/adapter.py` — Ticketmaster adapter glue.
- `src/vendors/ticketmaster/core.py` — `BotRunner` state machine.
- `src/registry/base.py` — generic `Registry[T]`.
- `src/registry/selectors.py` — selector YAML loader + `locator()`
  helpers.
- `src/strategies/base.py` — `SelectionStrategy` ABC + candidate
  parsing.
- `src/strategies/factory.py` — `build_strategy(cfg)` + default
  registrations.
- `src/notifiers/base.py` — `Notifier` ABC + `NotifyEvent` dataclass.
- `src/notifiers/multiplex.py` — fan-out + routing.
- `src/hooks/base.py` — `Hook` ABC + `HookRegistry`.
- `src/orchestrator/lifecycle.py` — `LIFECYCLE_EVENTS` + dispatcher.
- `src/orchestrator/parallel.py` — `ParallelCoordinator`.
- `src/proxy/manager.py` — `ProxyManager` + URL parsing.
- `src/humanize/mouse.py`, `src/humanize/typing.py`,
  `src/humanize/warmer.py`, `src/humanize/profile.py` — anti-detection
  primitives.
- `src/observability/metrics.py`, `src/observability/control.py` — HTTP
  observability surface.
- `src/utils/run_artifacts.py`, `src/utils/history.py`,
  `src/utils/logger.py`, `src/utils/stealth.py` — supporting utilities.
- `config/config.yaml`, `config/profiles/fast.yaml`,
  `config/profiles/safe.yaml`, `config/selectors/ticketmaster.yaml` —
  shipped configuration files.

Every cross-document link in the rest of `docs/` points back at this
list, so this file is the canonical map of the system.
