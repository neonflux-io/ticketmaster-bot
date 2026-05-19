# Hooks

A *hook* is an observer that reacts to lifecycle events fired by the
runner. Each runner owns its own `HookRegistry` (`src/hooks/base.py`)
so multi-account / parallel runs never share hook state. The
`LifecycleDispatcher` (`src/orchestrator/lifecycle.py`) fires events on
every registered hook in priority order and isolates exceptions: a
hook that raises is logged via `log.exception` and skipped, never
breaks the runner's main flow.

## Lifecycle event catalogue

The full canonical list of events lives in
`src/orchestrator/lifecycle.py` as the tuple `LIFECYCLE_EVENTS`. They
are listed here in the order they fire during a successful end-to-end
run:

| Event | When it fires (in `src/vendors/ticketmaster/core.py`) | Payload kwargs |
| --- | --- | --- |
| `on_run_start` | Top of `BotRunner.run` before any I/O. | `account` |
| `before_login` | About to call `auth.login(...)`. Only fires when an account is configured and session is not already valid. | `account` |
| `after_login` | `auth.login(...)` completed without raising. | `account` |
| `before_navigate` | About to call `navigator.open_event(page, url, …)`. | `url` |
| `after_queue_release` | Queue cleared (or no queue was present). The current page state is reported. | `state` |
| `before_select` | About to call `strategy.pick(page)`. | `strategy` |
| `after_select` | `strategy.pick(page)` returned. | `candidate` |
| `before_cart` | About to call `cart.add_to_cart(...)`. | `candidate` |
| `after_cart` | `cart.add_to_cart(...)` returned. | `candidate`, `added` |
| `before_checkout` | About to call `checkout.run_checkout(...)`. | `candidate` |
| `before_place_order` | About to click "Place Order" (only fires when `checkout.auto_purchase: true`). | `candidate` |
| `on_failure` | A step returned `False` or raised. Always fired before `on_run_end`. | `error` (exception or `None`), `account` |
| `on_run_end` | Always fired in `BotRunner.run`'s `finally` block. | `success`, `account` |

The dispatcher rejects unknown event names with `ValueError`, so a
typo at a call site fails loudly. The set of valid names is the
`_LIFECYCLE_EVENTS_SET` frozenset in
`src/orchestrator/lifecycle.py`.

## Hook ABC + ordering rules

`src/hooks/base.py` defines the `Hook` ABC:

```python
class Hook(ABC):
    enabled: bool = True       # gate flag; dispatcher skips disabled hooks
    priority: int = 0          # lower fires first; ties keep registration order

    @abstractmethod
    async def on_event(
        self,
        ctx: Any,
        event_type: str,
        **kwargs: Any,
    ) -> None: ...
```

- `ctx` is whatever the runner passes (the production runner passes
  `self`, so hooks can read `ctx.config`, `ctx.account`,
  `ctx.action_delay_min`, etc.).
- `event_type` is one of the names from `LIFECYCLE_EVENTS`.
- `kwargs` is the per-event payload from the table above. Hooks must
  tolerate missing keys (`kwargs.get(...)`).

`HookRegistry.all()` returns hooks sorted ascending by `priority` with
registration order as the stable tie-breaker (Python's sort is
stable). Disabled hooks still appear in the iteration so callers can
introspect the full set; the dispatcher applies the `enabled` filter
itself.

Convention for priorities:

- Negative priority for hooks that must snapshot state *before*
  observers (e.g. `SlowDownAfterFailureHook` uses `-10` so the
  slow-down lands before the screenshot dump).
- `0` (default) for ordinary observers.
- Positive priority for final cleanup-style hooks
  (e.g. `ScreenshotOnFailureHook` uses `+50` so it dumps last and
  sees any state mutations earlier hooks applied).

## Built-in hooks

### `ScreenshotOnFailureHook`

Source: `src/hooks/screenshot_on_failure.py`. Priority `+50`.

On `on_failure`, dumps three artefacts to
`<base_dir>/run-<UTC>-<account>/`:

- `screenshot.png` — full-page PNG via `Page.screenshot`.
- `dom.html` — rendered HTML returned by `Page.content`.
- `console.log` — one line per buffered console message (formatted as
  `[<type>] <text>`).

The hook attaches a `context.on("console", …)` listener the first time
it sees an event that carries a Playwright `BrowserContext` so console
messages emitted *during* the run end up in the dump. The listener is
detached in the `finally` block of `_dump` so contexts can be garbage-
collected even when the hook outlives the runner.

`base_dir` defaults to `logs`; tests pass a `tmp_path`. The
`io.screenshot-on-failure-writes` validation assertion checks the PNG
exists with `stat().st_size > 0`.

### `SlowDownAfterFailureHook`

Source: `src/hooks/slow_down_after_failure.py`. Priority `-10`.

On `on_failure`, multiplies the runner's `action_delay_min` and
`action_delay_max` by `multiplier` (default `1.5`). The bump is
cumulative — three consecutive failures at `multiplier=2.0` drive the
delays up by `8x`.

The hook returns silently when `ctx` is `None` or lacks the expected
attributes, so failure handling never throws from a hook. A
non-positive `multiplier` is rejected at construction with
`ValueError`.

The `io.slowdown-after-failure-bumps-delay` validation assertion pins
the strict-greater-than relationship between the pre- and post-failure
delay attributes.

## Wiring hooks into a run

The runner constructs a fresh `HookRegistry` per instance:

```python
# src/vendors/ticketmaster/core.py
self.hooks: HookRegistry = HookRegistry()
self.lifecycle: LifecycleDispatcher = LifecycleDispatcher(self.hooks)
```

Hooks are registered by the caller before `await runner.run()`:

```python
from src.vendors.ticketmaster.adapter import TicketmasterAdapter
from src.hooks.screenshot_on_failure import ScreenshotOnFailureHook
from src.hooks.slow_down_after_failure import SlowDownAfterFailureHook

runner = TicketmasterAdapter().build_runner(config, account=account)
runner.hooks.register(SlowDownAfterFailureHook(multiplier=2.0))
runner.hooks.register(ScreenshotOnFailureHook(base_dir="logs"))
await runner.run()
```

For the parallel coordinator (`src/orchestrator/parallel.py`), every
runner the `default_runner_factory` builds gets its own registry — the
caller wires the hooks per-runner by injecting a custom
`runner_factory` if they want a shared set.

## Adding a custom hook

1. Subclass `Hook` from `src/hooks/base.py`. Implement
   `async def on_event(self, ctx, event_type, **kwargs)`. Always
   filter by `event_type` first so your hook only acts on the events
   it cares about.
2. Set class-level `priority` and `enabled` defaults appropriate to
   the hook's intent (see the convention above).
3. Tolerate missing kwargs via `kwargs.get(...)` — runners may grow
   new payload keys over time.
4. Never raise from `on_event` for non-fatal conditions; log and
   return. The dispatcher will catch exceptions but a noisy log
   trail is preferable.
5. Register your hook with a runner's `HookRegistry` (typically in
   the CLI bootstrap or a test fixture). For distribution as a
   plugin, advertise the hook class through the
   `ticketmaster_bot.hooks` entry-point group bound to
   `src/registry/hooks.py`. Hook *instances* are registered with the
   per-runner `HookRegistry`; hook *classes* are advertised to the
   module-level `src/registry/hooks.py` singleton for entry-point
   discovery — this two-tier split keeps multi-account runs
   isolated while still allowing third-party hook discovery.

Example skeleton — a hook that logs every event for debugging:

```python
from src.hooks.base import Hook

class DebugLogHook(Hook):
    priority = -100   # fire before everything else

    async def on_event(self, ctx, event_type, **kwargs):
        # ctx is the BotRunner instance from src/vendors/ticketmaster/core.py
        account = kwargs.get("account") or getattr(
            getattr(ctx, "account", None), "name", None
        )
        log.info("[debug-hook] event=%s account=%s payload=%s",
                 event_type, account, kwargs)
```

The `io.hooks-lifecycle-order` validation assertion uses this exact
pattern — a hook that appends to a list — to assert the canonical
ordering of `on_run_start, before_select, after_select, on_run_end`
fires correctly.
