# Parallel orchestration

The `ParallelCoordinator` in `src/orchestrator/parallel.py` races
multiple per-account runners, cancels the losers as soon as one of
them wins, and integrates with the per-account proxy manager so each
runner gets its own egress IP. This document describes the race
semantics, the proxy plumbing, and the `StopOnSuccess` cancellation
flow that ties them together.

## Coordinator overview

`ParallelCoordinator(accounts, config, ...)` accepts:

- `accounts: list[AccountConfig]` — one runner per entry.
- `config: BotConfig` — shared by every runner.
- `max_parallel: int` (default `3`) — hard cap on concurrent runners.
- `stagger_seconds: float` (default `5.0`) — delay between successive
  launches; applied *inside* the semaphore so a sleeping runner does
  not waste its concurrency slot.
- `runner_factory: RunnerFactory | None` — defaults to
  `default_runner_factory` in `src/orchestrator/parallel.py`, which
  resolves the `ticketmaster` adapter from `src/registry/vendors.py`
  and asks it for a `BotRunner`. Tests inject a custom factory to
  swap in a fixture runner.

Construction validates the arguments — `max_parallel >= 1`,
`stagger_seconds >= 0`, `len(accounts) >= 1` — and raises `ValueError`
on bad input so misconfiguration surfaces before the first
Chromium launch.

The coordinator exposes a single public `asyncio.Event`:
`coordinator.stop_event`. Runners that want to short-circuit
voluntarily can poll it; the production `BotRunner` relies on
`asyncio.Task.cancel()` for early termination and ignores the event.

## Race semantics

```
ParallelCoordinator.run()
  ├── create one Task per account via _run_one
  ├── asyncio.wait(pending, return_when=FIRST_COMPLETED) loop:
  │     ├── if a task returned True  → set stop_event, cancel pending, return True
  │     ├── if a task raised         → log warning, keep racing
  │     └── if a task returned False → keep racing
  ├── if every task finished without a winner → return False
  └── on outer CancelledError → cancel still-running tasks, re-raise
```

The core loop in `ParallelCoordinator.run()`:

1. One `asyncio.Task` is created per account. Each task runs
   `_run_one(account, index, sem)` where `sem = asyncio.Semaphore(max_parallel)`.
2. `asyncio.wait(pending, return_when=FIRST_COMPLETED)` collects the
   first batch of completions; for each completed task we inspect
   `task.exception()` / `task.result()` and stop the loop as soon as
   any task returns truthy.
3. The winner sets `stop_event` inside `_run_one` *before* `run()`
   returns, so any sibling polling the event can short-circuit on
   its own.
4. `_cancel_pending(pending)` calls `task.cancel()` on every
   still-running task and awaits them all with a
   `_CANCEL_GRACE_SECONDS = 10.0` timeout. The grace window is
   double the validation contract's 5-second
   `io.parallel-stoponsuccess-cancels` requirement so Chromium-on-CI
   machines have headroom.

`_run_one` itself respects the configured cap + stagger:

```python
async with sem:
    if index > 0 and stagger_seconds > 0 and not stop_event.is_set():
        await asyncio.sleep(stagger_seconds)
    if stop_event.is_set():
        return False                       # late slot, race already decided
    runner = runner_factory(account, config, stop_event)
    result = await runner.run()
    if result:
        stop_event.set()
    return bool(result)
```

The first runner (`index == 0`) skips the stagger so the only-account
case has no extra latency.

## StopOnSuccess semantics

"`StopOnSuccess`" is the contract that ties the coordinator to the
per-runner cancellation flow:

1. **Winner signal**: any runner whose `run()` returns truthy is the
   winner. `_run_one` sets `stop_event` and returns; the coordinator
   sees the truthy result and exits the wait loop.
2. **Sibling cancellation**: still-pending tasks are cancelled via
   `task.cancel()`. The `asyncio.CancelledError` raised inside the
   sibling's `await runner.run()` propagates through the runner's
   own `try / finally` block in
   `src/vendors/ticketmaster/core.py::BotRunner._run`, which closes
   the Playwright context cleanly before the cancellation surfaces
   to the coordinator.
3. **Grace window**: `_cancel_pending(pending)` awaits the cancelled
   tasks under `asyncio.wait_for(..., timeout=_CANCEL_GRACE_SECONDS)`.
   If a runner ignores cancellation beyond 10 seconds, the
   coordinator logs a warning and returns anyway — the parent task
   tree never wedges.
4. **External cancellation**: when the coordinator itself is
   cancelled by its caller, the same cleanup path runs and the
   `CancelledError` is re-raised so the cancellation discipline
   propagates upward.

Validation contract assertions:

- `io.parallel-spawns-n-contexts` — `ParallelCoordinator.run(n=2)`
  produces two concurrently live Chromium contexts at peak.
- `io.parallel-stoponsuccess-cancels` — once one worker sets
  `stop_event`, siblings exit within 5 seconds and
  `pgrep -f chromium` returns zero within 10 seconds.

## Proxy manager integration

Source: `src/proxy/manager.py`.

Each runner asks its own `ProxyManager` for a `proxy=` kwarg before
launching Chromium. The manager lives on the runner instance (not the
coordinator) so a runner's proxy assignment is stable across its own
retries.

### Policy: `sticky`

`ProxyManager.resolve(account_name)` returns the same proxy for the
same `account_name` every time. The mapping is populated lazily on
first lookup: new accounts pick the next never-assigned slot before
any reuse happens, so a config with N accounts and N proxies always
produces a 1-to-1 mapping. The mapping is held on
`ProxyManager._sticky_assignments` and protected by an internal
`threading.Lock` — a `ParallelCoordinator`'s workers may share a
single `ProxyManager` safely.

### Policy: `round_robin`

`ProxyManager.resolve(account_name)` returns successive proxies from
the configured list, wrapping around at the end. The counter is
per-`ProxyManager` instance, so a single bot process driving multiple
accounts sequentially rotates cleanly. Combined with the parallel
coordinator, every runner picks up a fresh slot on launch.

### URL parsing

`parse_proxy_url(url)` in `src/proxy/manager.py` accepts standard
URLs of the form `scheme://[user:pass@]host[:port][/path]`. Bare
`host:port` is rejected so the scheme is always explicit (Playwright
requires it). Credentials embedded in the URL are split into
Playwright's separate `username` / `password` fields rather than
re-embedded in the `server` value — that avoids double-encoding the
auth header on some proxies.

### Validation

The `io.proxy-plumbed-to-playwright` assertion wraps
`chromium.launch_persistent_context` with a recording wrapper inside
a pytest and confirms the kwargs include the expected `proxy={"server":
...}` dict.

### Config knobs

```yaml
proxy:
  enabled: false
  policy: "sticky"          # sticky | round_robin
  urls:
    - "http://user:pass@proxy-1.example.com:8080"
    - "http://user:pass@proxy-2.example.com:8080"
```

When `enabled: false` or `urls: []`, `ProxyManager.resolve(...)`
returns `None` so callers omit the `proxy=` kwarg entirely (Playwright
rejects `proxy=None` on some versions). The `BotRunner` already
gates the `proxy_kwargs is not None` branch in
`src/vendors/ticketmaster/core.py::_run`.

## Wiring it up

```python
from src.utils.config_loader import load_config
from src.orchestrator.parallel import ParallelCoordinator

config = load_config("config/config.yaml")
coordinator = ParallelCoordinator(
    accounts=config.accounts,
    config=config,
    max_parallel=3,
    stagger_seconds=5.0,
)
success = await coordinator.run()
```

The CLI in `src/cli.py` exposes the equivalent flags:

```
ticketmaster-bot --parallel --max-parallel 3 --stagger 5
```

When `--parallel` is set, `src/main.py` constructs a
`ParallelCoordinator` with `max_parallel = args.max_parallel or
len(accounts)` and `stagger_seconds = args.stagger_seconds or
config.parallel.stagger_seconds`. Without `--parallel`, the bootstrap
runs a single account synchronously (the historical behaviour).

## Failure modes + diagnostics

- **All runners return False** — `coordinator.run()` returns `False`
  and `pending` is empty (no losers to cancel). The caller exits
  non-zero so the operator notices.
- **A runner raises** — the exception is captured by
  `task.exception()`, logged as `Runner <name> raised <ExcType>`, and
  the race continues. The remaining runners may still produce a
  winner.
- **Cancellation timeout** — a runner that ignores
  `asyncio.CancelledError` (e.g. by catching and swallowing it inside
  its own `try / finally`) logs the
  `Some sibling runners did not finish teardown within 10.0s` warning
  and the coordinator returns anyway. The orphan-process gate
  (`pgrep -f chromium | wc -l` == 0) catches any Chromium that leaks.
- **Bad proxy URL** — `ProxyManager.__init__` validates every URL up
  front via `parse_proxy_url`, so a typo in `proxy.urls` raises
  `ValueError` before any Chromium spawn. The runner constructs its
  `ProxyManager` in `__init__`, not `_run`, so the error surfaces at
  CLI startup instead of partway through a race.
