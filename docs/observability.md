# Observability

Four observability surfaces ship together: Prometheus metrics, a
FastAPI control panel, structured JSON logs, and per-run artefact
directories. They are independent (each can be enabled in isolation)
and share no global state, so multi-account / parallel runs do not
fight over them.

## Prometheus metrics

Source: `src/observability/metrics.py`.

Four collectors are registered at module-import time, all exposed
under the `ticketmaster_bot_` prefix the validation contract
(`io.prometheus-metrics-endpoint`) greps for:

| Metric name (as exposed) | Python identifier | Type | Description |
| --- | --- | --- | --- |
| `ticketmaster_bot_runs_total` | `RUNS_TOTAL` | Counter | Total runs attempted. |
| `ticketmaster_bot_runs_succeeded_total` | `RUNS_SUCCEEDED_TOTAL` | Counter | Runs that completed successfully. |
| `ticketmaster_bot_runs_failed_total` | `RUNS_FAILED_TOTAL` | Counter | Runs that failed. |
| `ticketmaster_bot_run_duration_seconds` | `RUN_DURATION_SECONDS` | Histogram | Per-run wall-clock duration in seconds. |

`prometheus_client` automatically appends the `_total` suffix to
counter names in the exposition format, which is why the Python
identifiers register the metric without the `_total` suffix.

### Serving `/metrics`

`make_asgi_app()` returns the standard `prometheus_client` ASGI app.
`start_metrics_server(port, host="127.0.0.1")` boots that app under
`uvicorn` inside an `asyncio.create_task` and returns a
`MetricsServer` handle whose `should_exit` setter triggers a clean
shutdown:

```python
from src.observability.metrics import start_metrics_server

server = await start_metrics_server(port=9000)
# … run the bot …
server.should_exit = True
await server.serve_task
```

The default production port is `9000` (declared in
`config/config.yaml` under the `observability` block when present);
tests bind to an ephemeral port via `socket.bind(("127.0.0.1", 0))`
to avoid clashing with the user's other services.

The endpoint returns HTTP `200` with `Content-Type: text/plain;
version=0.0.4; charset=utf-8` and a body containing each metric in
Prometheus exposition format.

## Control panel

Source: `src/observability/control.py`.

A FastAPI app served by `uvicorn` in a background task, exposing
three endpoints:

### `GET /status`

Returns a JSON snapshot of the runner's state pulled from a
`RunnerRef` dataclass:

```json
{
  "state": "checkout",
  "account": "alice",
  "event_url": "https://www.ticketmaster.com/event/…",
  "started_at": "2025-01-01T12:34:56.789012+00:00"
}
```

The `state` value is free-form and updated by the runner as it
transitions between lifecycle phases (`"idle"`, `"running"`,
`"queue"`, `"checkout"`, …).

### `GET /logs/tail?n=100`

Returns the last `n` lines of the runner's log file (defaults to
`100`, clamped to a non-negative integer). The endpoint streams the
file through a bounded `collections.deque` so very large log files do
not load fully into memory:

```json
{
  "lines": [
    "2025-01-01 12:34:56 [INFO] ticketmaster-bot: Launching browser",
    "..."
  ],
  "n": 100,
  "log_file": "logs/bot.log"
}
```

`RunnerRef.log_file` is `None` (or pointing at a missing file) when
the runner has not yet started writing; the endpoint returns an empty
`lines` list in that case rather than raising.

### `POST /stop`

Sets `RunnerRef.stop_event` and flips
`uvicorn.Server.should_exit = True` so the panel itself drains and
exits on the next loop iteration. Responds `202 Accepted`:

```json
{"status": "stopping"}
```

The validation contract assertion `io.control-stop-shuts-down`
verifies that a follow-up `GET /status` raises `httpx.ConnectError`
within 5 seconds of the POST.

### Boot + shutdown

```python
from src.observability.control import ControlServer, RunnerRef

ref = RunnerRef(state="idle", log_file=Path("logs/bot.log"))
server = ControlServer(port=9051, runner_ref=ref)
await server.serve()
# … run …
await server.shutdown(timeout=5.0)
```

Default port `9051`. Tests use an ephemeral port and tear the server
down via the same `serve_task` handle so no orphan listener survives
(`io.servers-no-orphan-listeners`).

## JSON logging

Source: `src/utils/logger.py`.

`setup_logger(name, level, log_file, format)` configures the
project-wide `"ticketmaster-bot"` logger with a Rich console handler
plus an optional file handler. When `format="json"` (driven by
`logging.format: "json"` in `config/config.yaml`), the file handler
swaps in `JsonFormatter` instead of the default human-readable
layout. Each line is one JSON object:

```json
{
  "timestamp": "2025-01-01T12:34:56.789Z",
  "level": "INFO",
  "name": "ticketmaster-bot",
  "message": "Launching browser",
  "exc_info": "Traceback (most recent call last):\n  …"
}
```

- `timestamp` is UTC ISO-8601 with millisecond precision and a `Z`
  suffix.
- `level` is the standard `logging` level name.
- `name` is the logger name (always `"ticketmaster-bot"`).
- `message` is the rendered message with Rich markup stripped via
  `_strip_rich_markup` so the JSON file mirrors the rich-text sibling.
- `exc_info` is present only when the record carries exception info.

The `io.json-log-mode` validation assertion iterates every non-empty
line in the log file and confirms `json.loads(line)` returns a dict
with `timestamp`, `level`, and `message` keys.

## Run artefact directories

Source: `src/utils/run_artifacts.py`.

A `RunArtifactDir` is a lazy handle to
`logs/run-<UTC>-<account>/`. The directory is only created on the
first `ensure()` call or implicit creation inside one of the
`dump_*` helpers, so a successful run never leaves an empty folder
behind.

`src/vendors/ticketmaster/core.py` instantiates a `RunArtifactDir` at
the top of `_run` when `cfg.logging.artifacts.record_har: true` and
threads the HAR path into Playwright via
`har_kwargs("network.har")` so HAR recording is live from the moment
`launch_persistent_context` returns.

### Artefacts written on failure

Per the `io.run-artifacts-complete` validation assertion, a failed
run leaves four files in the run directory, each with
`stat().st_size > 0`:

| File | Writer | Notes |
| --- | --- | --- |
| `screenshot.png` | `dump_screenshot(page, target)` | Full-page PNG. On Playwright failure, a 1-byte placeholder is written so size > 0. |
| `dom.html` | `dump_dom(page, target)` | Rendered HTML. Falls back to a `<!-- no DOM captured -->\n` body when `Page.content()` raises. |
| `console.log` | `dump_console(messages, target)` | One line per buffered console message. Empty buffer → single informational line. |
| `network.har` | Playwright (via `har_kwargs(...)` at launch) | Finalised on `context.close()`. |

The `ScreenshotOnFailureHook` (`src/hooks/screenshot_on_failure.py`)
is the typical caller for the first three; the HAR is wired in at
launch by the runner itself.

## Run history (SQLite)

Source: `src/utils/history.py`.

Optional async-SQLite log of completed runs. The schema lives in
`_SCHEMA_MIGRATIONS` keyed by `PRAGMA user_version`; current version
is `1`, creating the `runs` table with columns `id`, `started_at`,
`ended_at`, `account`, `event_url`, `outcome`, and `error`.

Usage:

```python
from src.utils.history import HistoryDB

async with HistoryDB("logs/history.sqlite") as db:
    run_id = await db.insert_run(
        started_at="2025-01-01T12:34:56Z",
        ended_at="2025-01-01T12:36:02Z",
        account="alice",
        event_url="https://www.ticketmaster.com/event/…",
        outcome="success",
    )
    history = await db.query_runs(limit=20)  # newest first
```

The `init()` method runs missing migrations in order and bumps
`PRAGMA user_version` so old DB files upgrade in place — adding a new
schema is a one-entry append to `_SCHEMA_MIGRATIONS`. The
`io.history-sqlite-roundtrip` assertion verifies the file is created,
`user_version` becomes a positive integer, and inserted rows are
retrievable.

## Reserved ports

Mission-wide reserved ports for the production config:

| Service | Port | Source |
| --- | --- | --- |
| Prometheus `/metrics` | `9000` | `src/observability/metrics.py` |
| FastAPI control panel | `9051` | `src/observability/control.py` |

Tests must use ephemeral ports (`socket.bind(("127.0.0.1", 0))`) to
avoid clashing with parallel test runs. Both servers expose a
`serve_task` handle and a `should_exit` setter so test teardown can
deterministically reclaim the port; the `io.servers-no-orphan-listeners`
validation assertion confirms `lsof -iTCP:<port> -sTCP:LISTEN` returns
empty in the test teardown.

## Wiring observability into a run

The CLI bootstrap in `src/main.py` calls
`setup_logger(format=cfg.logging.format)` from
`src/utils/logger.py` before constructing any runner so every
subsequent module call routes through the configured handler.

The metrics + control servers are not booted by default — operators
opt in by reading the relevant block from `config/config.yaml` and
calling `start_metrics_server(...)` / `ControlServer(...).serve()`
from their bootstrap path. The HAR + run-history paths are similarly
opt-in through `logging.artifacts.record_har: true` and the
`history.path` config key.
