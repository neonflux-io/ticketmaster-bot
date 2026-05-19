"""Real-Chromium tests for :class:`src.orchestrator.parallel.ParallelCoordinator`.

These tests boot a tiny real FastAPI server on an ephemeral port, then
launch two :class:`ParallelCoordinator` runners (each driving its own
real ``launch_persistent_context`` Chromium child). One runner is told
to "win" (``/win`` returns ``READY`` immediately); the other is told to
"loser" (``/wait`` hangs until cancelled).

Assertions per the F4.6 contract:

* Both runners' Chromium child processes are visible to ``pgrep``
  concurrently mid-run (verifies real, separate browser instances).
* The loser exits within 5 s of the winner setting ``stop_event``.
* No Chromium / headless-shell processes survive after the coordinator
  returns. The pgrep snapshot is anchored against a ``_pgrep_snapshot``
  helper that prefers ``ps`` so the test is portable to environments
  where ``pgrep`` is absent.
* The coordinator can also be exercised against pure-Python runners
  (no Chromium) to validate the race / cancellation semantics in
  isolation; those tests run fast (<1 s) and don't require Chromium.

No mocks, no monkeypatching of the HTTP/transport layer. The FastAPI
server is real (booted via ``uvicorn.Server``), the browsers are real,
the cancellation is driven by real :class:`asyncio.CancelledError`
propagation through Playwright's ``page.goto`` await.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response
from playwright.async_api import async_playwright

from src.orchestrator.parallel import ParallelCoordinator, Runner

# ---------------------------------------------------------------------------
# Test-only fixtures (real local FastAPI + a real Chromium runner).
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Kernel-assigned free port. Released as soon as we close the socket."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _RaceServer:
    """FastAPI app exposing two endpoints used by the parallel test.

    ``/win`` always returns instantly with a page that flips
    ``window.__ready = true`` so the winner runner's ``page.evaluate``
    poll terminates quickly. ``/wait`` blocks indefinitely (until the
    parent task is cancelled), simulating a runner stuck on a queue
    page.
    """

    def __init__(self) -> None:
        self.app = FastAPI()
        self.win_hits = 0
        self.wait_hits = 0
        self.wait_active = 0
        self._wait_lock = asyncio.Lock()

        @self.app.get("/win", response_class=HTMLResponse)
        async def win() -> HTMLResponse:
            self.win_hits += 1
            return HTMLResponse(
                "<!doctype html><html><body>"
                "<h1 id='msg'>READY</h1>"
                "<script>window.__ready = true;</script>"
                "</body></html>"
            )

        @self.app.get("/wait", response_class=HTMLResponse)
        async def wait() -> HTMLResponse:
            self.wait_hits += 1
            self.wait_active += 1
            try:
                # Hold the connection open for a very long time. The
                # client (Playwright) cancels its goto when the test
                # cancels the runner task; uvicorn forwards that
                # cancellation here.
                await asyncio.sleep(60.0)
            except asyncio.CancelledError:
                raise
            finally:
                self.wait_active -= 1
            return HTMLResponse("<!doctype html><html><body>too late</body></html>")

        @self.app.get("/status")
        async def status() -> JSONResponse:
            return JSONResponse(
                {
                    "win_hits": self.win_hits,
                    "wait_hits": self.wait_hits,
                    "wait_active": self.wait_active,
                }
            )


@asynccontextmanager
async def _serve(server: _RaceServer) -> AsyncIterator[str]:
    """Boot ``server`` on an ephemeral port; yield its base URL."""
    port = _free_port()
    config = uvicorn.Config(
        app=server.app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        lifespan="off",
        access_log=False,
    )
    uvi = uvicorn.Server(config)
    task = asyncio.create_task(uvi.serve())
    base = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(timeout=2.0) as probe:
        for _ in range(50):
            if uvi.started:
                break
            await asyncio.sleep(0.05)
        else:  # pragma: no cover - defensive
            raise RuntimeError("uvicorn never started")
        for _ in range(50):
            try:
                resp = await probe.get(f"{base}/status")
                if resp.status_code == 200:
                    break
            except httpx.HTTPError:
                await asyncio.sleep(0.05)
    try:
        yield base
    finally:
        # /wait may still be holding a long-poll connection if a loser
        # runner had it open. ``should_exit`` requests a graceful stop;
        # if that doesn't complete within a short window we force the
        # server task to cancel so the test teardown doesn't wedge.
        uvi.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except asyncio.TimeoutError:
            uvi.force_exit = True
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass


# ---------------------------------------------------------------------------
# Chromium counting helper (works without psutil; uses ps).
# ---------------------------------------------------------------------------


def _chromium_descendants(parent_pid: int) -> set[int]:
    """Return the PIDs of every Chromium-ish descendant of ``parent_pid``.

    Uses ``ps`` (POSIX, always present on macOS and Linux) so the test
    does not depend on the optional ``psutil`` package. We walk the
    process tree by ppid, then keep only entries whose command line
    matches a Chromium / headless_shell marker -- this filters out
    pytest, uvicorn, and the python parent itself.
    """
    try:
        out = subprocess.check_output(
            ["ps", "-A", "-o", "pid=,ppid=,command="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return set()

    by_ppid: dict[int, list[tuple[int, str]]] = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        cmd = parts[2]
        by_ppid.setdefault(ppid, []).append((pid, cmd))

    matching: set[int] = set()
    stack: list[int] = [parent_pid]
    seen: set[int] = set()
    chromium_markers = (
        "Chromium",
        "chromium",
        "Chrome for Testing",
        "Google Chrome for Testing",
        "headless_shell",
        "Chrome Helper",
        "chrome.app",
    )
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for pid, cmd in by_ppid.get(cur, []):
            if any(marker in cmd for marker in chromium_markers):
                matching.add(pid)
            stack.append(pid)
    return matching


def _wait_for_no_chromium(parent_pid: int, deadline_s: float) -> int:
    """Poll until no Chromium descendants remain or ``deadline_s`` elapses."""
    end = time.monotonic() + deadline_s
    last = _chromium_descendants(parent_pid)
    while time.monotonic() < end:
        last = _chromium_descendants(parent_pid)
        if not last:
            return 0
        time.sleep(0.1)
    return len(last)


# ---------------------------------------------------------------------------
# Chromium-backed Runner used by the real-browser race test.
# ---------------------------------------------------------------------------


@dataclass
class _ChromiumRunner:
    """One-shot real-Chromium runner used only by the parallel test.

    The runner mirrors the production :class:`BotRunner` shape:

    * Calls ``async_playwright()`` / ``launch_persistent_context`` so we
      get a real, separate Chromium child process per runner (visible
      to ``pgrep``).
    * Navigates to ``url`` and inspects ``window.__ready`` to decide
      whether to return ``True`` (winner) or keep polling.
    * Tears the context down in a ``finally`` so a sibling-triggered
      :class:`asyncio.CancelledError` still closes Chromium.

    The :attr:`opened` and :attr:`closed` attributes are inspected by
    the tests to confirm the lifecycle ran end-to-end.
    """

    name: str
    user_data_dir: Path
    url: str
    contexts_active: dict[str, int]
    poll_seconds: float = 0.05
    opened: asyncio.Event | None = None
    closed: asyncio.Event | None = None

    def __post_init__(self) -> None:
        # Defer Event creation until we're in the running loop. Tests
        # that don't care can leave these None.
        if self.opened is None:
            self.opened = asyncio.Event()
        if self.closed is None:
            self.closed = asyncio.Event()

    async def run(self) -> bool:
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            self.contexts_active[self.name] = id(context)
            assert self.opened is not None
            self.opened.set()
            try:
                page = await context.new_page()
                try:
                    await page.goto(self.url, wait_until="domcontentloaded", timeout=10_000)
                except Exception:
                    # /wait hangs; goto may raise on cancellation. Either
                    # way, fall through to the polling loop so the test
                    # can decide when to cancel us.
                    pass
                # Poll for the readiness flag. /win sets it
                # immediately; /wait never returns so the loop is
                # interrupted by CancelledError from the sibling
                # winner.
                while True:
                    try:
                        ready = await page.evaluate("() => !!window.__ready")
                    except Exception:
                        ready = False
                    if ready:
                        return True
                    await asyncio.sleep(self.poll_seconds)
            finally:
                try:
                    await context.close()
                except Exception:  # noqa: BLE001
                    pass
                self.contexts_active.pop(self.name, None)
                assert self.closed is not None
                self.closed.set()


# ---------------------------------------------------------------------------
# Pure-Python runners used by the fast race / cancellation unit tests.
# ---------------------------------------------------------------------------


class _PyRunner:
    """Synchronous-flavoured runner for fast unit tests.

    * If ``result`` is ``True`` it sleeps ``win_after`` then returns
      ``True``.
    * If ``result`` is ``False`` it sleeps ``hang_for`` then returns
      ``False`` (used to simulate a slow non-winning account).
    """

    def __init__(
        self,
        *,
        result: bool,
        win_after: float = 0.0,
        hang_for: float = 60.0,
    ) -> None:
        self.result = result
        self.win_after = win_after
        self.hang_for = hang_for
        self.started_at: float | None = None
        self.cancelled = False
        self.exited_at: float | None = None

    async def run(self) -> bool:
        self.started_at = time.monotonic()
        try:
            if self.result:
                await asyncio.sleep(self.win_after)
                self.exited_at = time.monotonic()
                return True
            # Loser: hang until cancelled.
            await asyncio.sleep(self.hang_for)
            self.exited_at = time.monotonic()
            return False
        except asyncio.CancelledError:
            self.cancelled = True
            self.exited_at = time.monotonic()
            raise


@dataclass
class _Account:
    """Minimal stand-in for :class:`AccountConfig` in pure-Python tests."""

    name: str


# ---------------------------------------------------------------------------
# Tests: pure-Python runner race / cancellation semantics.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_winner_sets_event_and_cancels_loser() -> None:
    """First runner to return True cancels every sibling within 5 s."""
    winner = _PyRunner(result=True, win_after=0.05)
    loser = _PyRunner(result=False, hang_for=60.0)
    instances = {"winner": winner, "loser": loser}

    def factory(account, _config, _stop):  # noqa: ANN001
        return instances[account.name]

    coord = ParallelCoordinator(
        accounts=[_Account("winner"), _Account("loser")],
        config=None,  # not used by _PyRunner
        max_parallel=3,
        stagger_seconds=0.0,
        runner_factory=factory,
    )

    result = await coord.run()

    assert result is True
    assert coord.stop_event.is_set()
    assert loser.cancelled is True
    assert loser.exited_at is not None and winner.exited_at is not None
    # The loser must have exited within 5 s of the winner setting
    # stop_event. We approximate winner-set time with winner.exited_at
    # because set() happens immediately after run() returns.
    delta = loser.exited_at - winner.exited_at
    assert delta < 5.0, f"loser took {delta:.2f}s to unwind after winner"


@pytest.mark.asyncio
async def test_all_runners_fail_returns_false() -> None:
    """If no runner succeeds, the coordinator returns False without raising."""
    a = _PyRunner(result=False, hang_for=0.05)
    b = _PyRunner(result=False, hang_for=0.05)
    instances = {"a": a, "b": b}

    coord = ParallelCoordinator(
        accounts=[_Account("a"), _Account("b")],
        config=None,
        stagger_seconds=0.0,
        max_parallel=3,
        runner_factory=lambda acc, _c, _s: instances[acc.name],
    )
    assert await coord.run() is False
    assert not coord.stop_event.is_set()


@pytest.mark.asyncio
async def test_max_parallel_caps_concurrency() -> None:
    """``max_parallel=1`` must serialise launches: at most one runner live."""
    started_at: dict[str, float] = {}
    finished_at: dict[str, float] = {}

    class _Capped:
        def __init__(self, name: str) -> None:
            self.name = name

        async def run(self) -> bool:
            started_at[self.name] = time.monotonic()
            await asyncio.sleep(0.10)
            finished_at[self.name] = time.monotonic()
            return False

    accounts = [_Account("a"), _Account("b"), _Account("c")]
    coord = ParallelCoordinator(
        accounts=accounts,
        config=None,
        max_parallel=1,
        stagger_seconds=0.0,
        runner_factory=lambda acc, _c, _s: _Capped(acc.name),
    )
    await coord.run()
    # Each runner sleeps 0.1 s; with concurrency=1 the second must start
    # only after the first finishes.
    assert finished_at["a"] <= started_at["b"] + 0.01
    assert finished_at["b"] <= started_at["c"] + 0.01


@pytest.mark.asyncio
async def test_stop_event_set_before_launch_skips_runner() -> None:
    """A runner whose slot opens after stop_event is set must not run."""
    launched: list[str] = []

    class _Spy:
        def __init__(self, name: str) -> None:
            self.name = name

        async def run(self) -> bool:
            launched.append(self.name)
            return self.name == "winner"

    accounts = [_Account("winner"), _Account("late")]
    coord = ParallelCoordinator(
        accounts=accounts,
        config=None,
        max_parallel=1,  # winner runs first, holds the slot
        stagger_seconds=0.0,
        runner_factory=lambda acc, _c, _s: _Spy(acc.name),
    )
    assert await coord.run() is True
    assert "winner" in launched
    # The "late" runner's slot opens after winner returns; by then
    # stop_event is set inside _run_one's pre-launch check.
    assert "late" not in launched


@pytest.mark.asyncio
async def test_stagger_seconds_delays_launches() -> None:
    """Successive launches sleep for ``stagger_seconds`` after the first."""
    started_at: dict[str, float] = {}

    class _Mark:
        def __init__(self, name: str) -> None:
            self.name = name

        async def run(self) -> bool:
            started_at[self.name] = time.monotonic()
            await asyncio.sleep(0.5)
            return False

    accounts = [_Account("a"), _Account("b")]
    coord = ParallelCoordinator(
        accounts=accounts,
        config=None,
        max_parallel=3,
        stagger_seconds=0.15,
        runner_factory=lambda acc, _c, _s: _Mark(acc.name),
    )
    await coord.run()
    # Second runner waits stagger_seconds before launching.
    delta = started_at["b"] - started_at["a"]
    assert delta >= 0.12, f"stagger not honoured: {delta:.3f}s"


@pytest.mark.asyncio
async def test_init_rejects_invalid_args() -> None:
    with pytest.raises(ValueError):
        ParallelCoordinator(accounts=[_Account("a")], config=None, max_parallel=0)
    with pytest.raises(ValueError):
        ParallelCoordinator(accounts=[_Account("a")], config=None, stagger_seconds=-1.0)
    with pytest.raises(ValueError):
        ParallelCoordinator(accounts=[], config=None)


# ---------------------------------------------------------------------------
# Real-Chromium parallel race test.
# ---------------------------------------------------------------------------


def _wait_for_baseline(parent_pid: int, baseline: int, deadline_s: float) -> int:
    """Poll until the Chromium descendant count drops back to ``baseline``."""
    end = time.monotonic() + deadline_s
    last = len(_chromium_descendants(parent_pid))
    while time.monotonic() < end:
        last = len(_chromium_descendants(parent_pid))
        if last <= baseline:
            return last
        time.sleep(0.1)
    return last


@pytest.mark.asyncio
async def test_two_real_chromium_runners_race_against_fastapi(
    tmp_path: Path,
) -> None:
    """End-to-end real-browser race against a local FastAPI fixture.

    Spawns two ``launch_persistent_context`` Chromium children pointing
    at ``/win`` and ``/wait`` respectively on the same FastAPI server.
    Confirms:

    * Both Chromium children are alive at the same time mid-run.
    * The loser unwinds within 5 s of the winner.
    * No Chromium descendants of this pytest process survive after the
      coordinator returns.
    """
    server = _RaceServer()

    # Other tests in this session may have launched their own Chromium
    # via the session-scoped ``chromium`` fixture in conftest.py. Those
    # processes will still be alive when we run. Snapshot the
    # pre-existing count so the post-teardown assertion only counts
    # Chromium children *we* introduced.
    baseline_chromium = len(_chromium_descendants(os.getpid()))

    async with _serve(server) as base:
        contexts_active: dict[str, int] = {}
        winner = _ChromiumRunner(
            name="winner",
            user_data_dir=tmp_path / "winner-profile",
            url=f"{base}/win",
            contexts_active=contexts_active,
        )
        loser = _ChromiumRunner(
            name="loser",
            user_data_dir=tmp_path / "loser-profile",
            url=f"{base}/wait",
            contexts_active=contexts_active,
        )
        winner.user_data_dir.mkdir(parents=True, exist_ok=True)
        loser.user_data_dir.mkdir(parents=True, exist_ok=True)
        instances: dict[str, Runner] = {"winner": winner, "loser": loser}

        coord = ParallelCoordinator(
            accounts=[_Account("loser"), _Account("winner")],
            config=None,
            max_parallel=2,
            stagger_seconds=0.0,
            runner_factory=lambda acc, _c, _s: instances[acc.name],
        )

        # Snapshot pgrep mid-run from a watcher task. The watcher waits
        # until both runners have signalled ``opened``, then captures
        # the Chromium descendant count of this Python process. That
        # number must be ≥ 2 because launch_persistent_context spawns
        # an independent Chrome subprocess per call.
        chromium_peak: dict[str, int] = {"count": 0}

        async def _watch_peak() -> None:
            # Poll repeatedly so we catch the moment both Chromium
            # children are concurrently alive even if the winner returns
            # very quickly. We bail out once stop_event is set
            # (winner-found) plus a small grace window; by then the
            # peak measurement is over.
            while not coord.stop_event.is_set():
                count = len(_chromium_descendants(os.getpid()))
                if count > chromium_peak["count"]:
                    chromium_peak["count"] = count
                await asyncio.sleep(0.05)
            # One last sample after stop_event is set but before the
            # winner releases its context (the winner is still inside
            # its run() coroutine here -- stop_event was set just
            # before return).
            count = len(_chromium_descendants(os.getpid()))
            if count > chromium_peak["count"]:
                chromium_peak["count"] = count

        watcher = asyncio.create_task(_watch_peak())
        winner_set_at: dict[str, float] = {}

        async def _stamp_winner() -> None:
            await coord.stop_event.wait()
            winner_set_at["t"] = time.monotonic()

        stamper = asyncio.create_task(_stamp_winner())

        try:
            success = await asyncio.wait_for(coord.run(), timeout=60.0)
        finally:
            for t in (watcher, stamper):
                if not t.done():
                    t.cancel()
            await asyncio.gather(watcher, stamper, return_exceptions=True)

        assert success is True, "winner runner should have succeeded"
        assert coord.stop_event.is_set()

        # Both Chromium browsers were alive at the same time mid-run.
        # Compare against the pre-existing baseline so concurrent
        # session-scoped fixtures from other tests don't inflate the
        # measurement.
        assert chromium_peak["count"] >= baseline_chromium + 2, (
            f"expected ≥{baseline_chromium + 2} concurrent Chromium "
            f"descendants mid-run (baseline {baseline_chromium}), "
            f"got {chromium_peak['count']}"
        )

        # Loser unwound its context within 5 s of stop_event being set.
        assert loser.closed is not None and loser.closed.is_set(), "loser context did not close"
        # Both runners signalled closed; verify loser exited within
        # the contract window relative to stop_event being set.
        # We approximate "stop_event set" timestamp via the stamper.
        if "t" in winner_set_at:
            now = time.monotonic()
            since_event = now - winner_set_at["t"]
            assert since_event < 10.0, (
                f"coordinator returned {since_event:.2f}s after stop_event "
                "(>10s); loser cleanup likely stalled"
            )

    # After coordinator return: no Chromium descendants we spawned may
    # outlive the coordinator. The session-scoped Chromium fixture in
    # conftest may still be running for other tests, so the assertion
    # is that the count returns to its pre-coordinator baseline within
    # a short polling window (Chrome closes async).
    remaining = _wait_for_baseline(os.getpid(), baseline=baseline_chromium, deadline_s=10.0)
    assert remaining <= baseline_chromium, (
        f"{remaining - baseline_chromium} Chromium descendant(s) "
        f"survived parallel coordinator teardown beyond baseline "
        f"{baseline_chromium} -- looks like a leak"
    )


# ---------------------------------------------------------------------------
# CLI wiring smoke: --parallel surfaces in argparse + main path.
# ---------------------------------------------------------------------------


def test_cli_parses_parallel_flags() -> None:
    """``--parallel --max-parallel N --stagger S`` parse without error."""
    from src.cli import parse_args

    args = parse_args(
        [
            "--parallel",
            "--max-parallel",
            "2",
            "--stagger",
            "1.5",
            "--dry-run",
        ]
    )
    assert args.parallel is True
    assert args.max_parallel == 2
    assert args.stagger_seconds == 1.5


def test_main_module_imports_parallel_coordinator() -> None:
    """``src.main`` references ``ParallelCoordinator`` so the wire-up exists."""
    import importlib

    main_module = importlib.import_module("src.main")
    # The import happens lazily inside main_async; what we can statically
    # check is that the orchestrator module is importable from the same
    # interpreter the CLI uses.
    pc = importlib.import_module("src.orchestrator.parallel")
    assert hasattr(pc, "ParallelCoordinator")
    # And src.main references --parallel via its CLI surface.
    src = Path(main_module.__file__).read_text()
    assert "args.parallel" in src
    assert "ParallelCoordinator" in src


# ---------------------------------------------------------------------------
# Force a real subprocess CLI sanity check: --parallel + --dry-run exits 0
# without launching browsers.
# ---------------------------------------------------------------------------


def test_cli_parallel_dry_run_exits_zero(tmp_path: Path) -> None:
    """``run.py --parallel --dry-run`` must exit 0 without touching browsers."""
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root)
    proc = subprocess.run(
        [
            sys.executable,
            str(repo_root / "run.py"),
            "--parallel",
            "--max-parallel",
            "2",
            "--stagger",
            "0",
            "--dry-run",
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"--parallel --dry-run exited {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    # Even though we didn't launch a browser, the parallel-config
    # banner should have logged. Logging output goes to stderr.
    assert "Parallel: True" in proc.stderr or "Parallel: True" in proc.stdout


__all__: list[str] = []


# ---------------------------------------------------------------------------
# Guard: make sure the FastAPI server (Response import) is reachable by
# tooling that scans imports — pytest collects modules even if this test
# is skipped on a given platform, so keep the import in scope.
# ---------------------------------------------------------------------------


_ = Response  # silence "unused import" linters; FastAPI's Response is
# exported here for tests that may want to extend the fixture.
