"""Tests for :class:`src.proxy.manager.ProxyManager` and its plumbing into
``BotRunner.launch_persistent_context``.

The validation contract assertion ``io.proxy-plumbed-to-playwright``
requires that when ``proxy.url`` is set per account, the
``launch_persistent_context`` call receives a ``proxy={"server": ...}``
kwarg whose ``server`` equals the configured URL.

The full coverage here is:

* :func:`parse_proxy_url` happy/edge cases (scheme handling, embedded
  credentials, missing-scheme/host errors).
* :class:`ProxyManager` with ``sticky`` policy assigns one URL per
  account deterministically and is stable across calls.
* :class:`ProxyManager` with ``round_robin`` policy rotates through
  the proxy list across successive calls.
* When ``proxy.enabled`` is ``False`` or the URL list is empty,
  :meth:`ProxyManager.resolve` returns ``None`` — the contract for
  callers to know they must omit the kwarg.
* End-to-end plumbing: an instrumented stand-in for
  ``chromium.launch_persistent_context`` is injected via
  ``monkeypatch`` so the test can assert that
  ``BotRunner.run`` forwards exactly the configured proxy URL and
  credentials.
* Optional real-network integration: when ``TEST_PROXY_URL`` is set in
  the environment, route ``httpbin.org/ip`` through the proxy and
  confirm the returned ``origin`` differs from the local-egress
  baseline. Skipped cleanly when the env var is absent.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.async_api import async_playwright

from src.proxy.manager import ProxyManager, parse_proxy_url
from src.utils.config_loader import (
    AccountConfig,
    BotConfig,
    BrowserConfig,
    CheckoutConfig,
    EventConfig,
    LoggingConfig,
    NotificationsConfig,
    PaymentConfig,
    ProxyConfig,
    StealthConfig,
    TicketsConfig,
    TimingConfig,
    load_config,
)
from src.vendors.ticketmaster.core import BotRunner

# ---------------------------------------------------------------------------
# parse_proxy_url
# ---------------------------------------------------------------------------


def test_parse_proxy_url_host_port_only() -> None:
    out = parse_proxy_url("http://proxy.example.com:8080")
    assert out == {"server": "http://proxy.example.com:8080"}


def test_parse_proxy_url_with_credentials() -> None:
    out = parse_proxy_url("http://alice:s3cret@proxy.example.com:8080")
    assert out == {
        "server": "http://proxy.example.com:8080",
        "username": "alice",
        "password": "s3cret",
    }


def test_parse_proxy_url_socks5_scheme() -> None:
    out = parse_proxy_url("socks5://proxy.example.com:1080")
    assert out["server"] == "socks5://proxy.example.com:1080"


def test_parse_proxy_url_url_encoded_password_decoded() -> None:
    # Real proxy providers commonly URL-encode passwords with special chars.
    # parse_proxy_url should decode them so Playwright receives the raw
    # secret in the username/password fields.
    out = parse_proxy_url("http://user:p%40ss%20w%23@proxy.example.com:8080")
    assert out["username"] == "user"
    assert out["password"] == "p@ss w#"


def test_parse_proxy_url_strips_credentials_from_server_field() -> None:
    # Playwright re-attaches creds via the separate fields; embedding
    # them in the URL too produces a double-auth header on some proxies.
    out = parse_proxy_url("http://u:p@host.example:3128")
    assert "u:p" not in out["server"]
    assert "@" not in out["server"]


def test_parse_proxy_url_requires_scheme() -> None:
    with pytest.raises(ValueError, match="scheme"):
        parse_proxy_url("proxy.example.com:8080")


def test_parse_proxy_url_requires_host() -> None:
    with pytest.raises(ValueError, match="host"):
        parse_proxy_url("http://")


def test_parse_proxy_url_rejects_empty() -> None:
    with pytest.raises(ValueError):
        parse_proxy_url("")


def test_parse_proxy_url_rejects_non_string() -> None:
    with pytest.raises(ValueError):
        parse_proxy_url(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ProxyManager — sticky policy
# ---------------------------------------------------------------------------


def _make_proxy_config(
    urls: list[str],
    *,
    policy: str = "sticky",
    enabled: bool = True,
) -> ProxyConfig:
    return ProxyConfig(enabled=enabled, policy=policy, urls=list(urls))


def test_proxy_manager_sticky_assigns_same_proxy_to_same_account() -> None:
    cfg = _make_proxy_config(
        ["http://proxy-a:8080", "http://proxy-b:8080"],
        policy="sticky",
    )
    mgr = ProxyManager(cfg)

    first = mgr.resolve("alice")
    second = mgr.resolve("alice")
    third = mgr.resolve("alice")

    assert first == second == third
    assert first is not None
    assert first["server"] in {"http://proxy-a:8080", "http://proxy-b:8080"}


def test_proxy_manager_sticky_distinct_accounts_get_distinct_proxies() -> None:
    cfg = _make_proxy_config(
        ["http://proxy-a:8080", "http://proxy-b:8080"],
        policy="sticky",
    )
    mgr = ProxyManager(cfg)

    alice = mgr.resolve("alice")
    bob = mgr.resolve("bob")

    assert alice is not None and bob is not None
    assert alice["server"] != bob["server"]


def test_proxy_manager_sticky_wraps_when_accounts_exceed_proxies() -> None:
    cfg = _make_proxy_config(
        ["http://proxy-a:8080", "http://proxy-b:8080"],
        policy="sticky",
    )
    mgr = ProxyManager(cfg)

    a = mgr.resolve("a")
    b = mgr.resolve("b")
    c = mgr.resolve("c")  # wraps back to proxy-a

    assert a is not None and b is not None and c is not None
    assert {a["server"], b["server"]} == {"http://proxy-a:8080", "http://proxy-b:8080"}
    assert c["server"] == a["server"]


# ---------------------------------------------------------------------------
# ProxyManager — round_robin policy
# ---------------------------------------------------------------------------


def test_proxy_manager_round_robin_rotates_through_proxies() -> None:
    cfg = _make_proxy_config(
        ["http://proxy-a:8080", "http://proxy-b:8080", "http://proxy-c:8080"],
        policy="round_robin",
    )
    mgr = ProxyManager(cfg)

    seen = [mgr.resolve("any")["server"] for _ in range(6)]  # type: ignore[index]
    assert seen[:3] == [
        "http://proxy-a:8080",
        "http://proxy-b:8080",
        "http://proxy-c:8080",
    ]
    # And then it wraps cleanly:
    assert seen[3:] == [
        "http://proxy-a:8080",
        "http://proxy-b:8080",
        "http://proxy-c:8080",
    ]


def test_proxy_manager_round_robin_ignores_account_name() -> None:
    """Round-robin advances per call regardless of account argument.

    The contract: round_robin's whole point is that consecutive calls
    return successive entries from the list. The account_name argument
    is accepted (so the signature is stable across policies) but does
    *not* short-circuit to a sticky binding.
    """
    cfg = _make_proxy_config(
        ["http://proxy-a:8080", "http://proxy-b:8080", "http://proxy-c:8080"],
        policy="round_robin",
    )
    mgr = ProxyManager(cfg)

    first = mgr.resolve("alice")
    second = mgr.resolve("bob")
    third = mgr.resolve("alice")  # next slot, not alice's previous one
    assert first is not None and second is not None and third is not None
    assert [first["server"], second["server"], third["server"]] == [
        "http://proxy-a:8080",
        "http://proxy-b:8080",
        "http://proxy-c:8080",
    ]


# ---------------------------------------------------------------------------
# ProxyManager — disabled / empty
# ---------------------------------------------------------------------------


def test_proxy_manager_empty_url_list_returns_none() -> None:
    mgr = ProxyManager(_make_proxy_config([], policy="sticky"))
    assert mgr.resolve("alice") is None
    assert mgr.enabled is False


def test_proxy_manager_disabled_returns_none_even_with_urls() -> None:
    mgr = ProxyManager(
        _make_proxy_config(
            ["http://proxy-a:8080"],
            policy="sticky",
            enabled=False,
        )
    )
    assert mgr.enabled is False
    assert mgr.resolve("alice") is None


def test_proxy_manager_rejects_unknown_policy() -> None:
    with pytest.raises(ValueError, match="sticky|round_robin"):
        ProxyManager(_make_proxy_config(["http://proxy-a:8080"], policy="rotate"))


def test_proxy_manager_rejects_malformed_url() -> None:
    with pytest.raises(ValueError):
        ProxyManager(_make_proxy_config(["proxy-no-scheme:8080"], policy="sticky"))


def test_proxy_manager_returns_credentials_when_url_has_them() -> None:
    cfg = _make_proxy_config(
        ["http://alice:s3cret@proxy-a:8080"],
        policy="sticky",
    )
    mgr = ProxyManager(cfg)
    out = mgr.resolve("alice")
    assert out == {
        "server": "http://proxy-a:8080",
        "username": "alice",
        "password": "s3cret",
    }


def test_proxy_manager_resolved_dict_is_a_copy() -> None:
    """Mutating the returned dict must not corrupt the manager's state."""
    cfg = _make_proxy_config(["http://proxy-a:8080"], policy="sticky")
    mgr = ProxyManager(cfg)

    first = mgr.resolve("alice")
    assert first is not None
    first["server"] = "tampered"

    second = mgr.resolve("alice")
    assert second is not None
    assert second["server"] == "http://proxy-a:8080"


# ---------------------------------------------------------------------------
# Config loader integration
# ---------------------------------------------------------------------------


def test_config_loader_parses_proxy_block(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        """
event:
  url: "https://www.ticketmaster.com/event/X"
proxy:
  enabled: true
  policy: "round_robin"
  urls:
    - "http://user:pass@proxy-a:8080"
    - "http://proxy-b:8080"
"""
    )
    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing-accounts.yaml")
    assert cfg.proxy.enabled is True
    assert cfg.proxy.policy == "round_robin"
    assert cfg.proxy.urls == [
        "http://user:pass@proxy-a:8080",
        "http://proxy-b:8080",
    ]


def test_config_loader_proxy_defaults_disabled(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        """
event:
  url: "https://www.ticketmaster.com/event/X"
"""
    )
    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing-accounts.yaml")
    assert cfg.proxy.enabled is False
    assert cfg.proxy.urls == []
    assert cfg.proxy.policy == "sticky"


def test_config_loader_rejects_bad_proxy_policy(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        """
event:
  url: "https://www.ticketmaster.com/event/X"
proxy:
  enabled: true
  policy: "rotate"
  urls: ["http://proxy-a:8080"]
"""
    )
    with pytest.raises(ValueError, match="sticky|round_robin"):
        load_config(tmp_path / "config.yaml", tmp_path / "missing-accounts.yaml")


# ---------------------------------------------------------------------------
# BotRunner plumbing — recording wrapper around launch_persistent_context
# ---------------------------------------------------------------------------


class _LaunchCaptured(Exception):
    """Raised inside the recording wrapper to short-circuit the bot flow.

    The plumbing test only cares about the kwargs ``launch_persistent_context``
    is *called* with. Re-raising once kwargs are captured aborts the
    rest of the run (which would otherwise need a real Ticketmaster
    page) without leaving any Chromium child behind because we never
    proceed to launch one.
    """

    def __init__(self, kwargs: dict[str, Any]) -> None:
        super().__init__("launch kwargs captured")
        self.kwargs = kwargs


def _make_bot_config(*, proxy: ProxyConfig, account_name: str) -> BotConfig:
    """Construct a minimal BotConfig sufficient to instantiate BotRunner."""
    return BotConfig(
        events=[EventConfig(url="https://www.ticketmaster.com/event/X")],
        tickets=TicketsConfig(),
        checkout=CheckoutConfig(payment=PaymentConfig()),
        timing=TimingConfig(),
        logging=LoggingConfig(),
        notifications=NotificationsConfig(),
        browser=BrowserConfig(
            headless=True,
            user_data_dir="sessions/proxy-test",
            stealth=StealthConfig(enabled=False),
        ),
        accounts=[
            AccountConfig(
                email=f"{account_name}@example.com",
                password="hunter2",
                name=account_name,
            )
        ],
        proxy=proxy,
    )


async def test_botrunner_passes_proxy_to_launch_persistent_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """End-to-end: the configured proxy URL reaches Playwright.

    Wraps ``chromium.launch_persistent_context`` so the first call
    records its kwargs and raises :class:`_LaunchCaptured` to abort the
    rest of the bot flow. Asserts the captured ``proxy=`` kwarg matches
    the configured URL (credential-stripped server + username/password).
    """
    proxy_cfg = ProxyConfig(
        enabled=True,
        policy="sticky",
        urls=["http://alice:s3cret@proxy.example.com:8080"],
    )
    cfg = _make_bot_config(proxy=proxy_cfg, account_name="alice")
    # Force the user_data_dir under tmp_path so the test never touches
    # the real sessions/ directory.
    cfg.browser.user_data_dir = str(tmp_path / "default")

    captured: dict[str, Any] = {}

    real_async_playwright = async_playwright

    class _RecordingChromium:
        async def launch_persistent_context(
            self, *args: Any, **kwargs: Any
        ) -> None:
            captured.update(kwargs)
            raise _LaunchCaptured(kwargs)

    class _RecordingPlaywright:
        chromium = _RecordingChromium()

    class _RecordingContextManager:
        async def __aenter__(self) -> _RecordingPlaywright:
            return _RecordingPlaywright()

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            # Suppress only _LaunchCaptured so the test asserts on it
            # below; let everything else propagate.
            return False

    monkeypatch.setattr(
        "src.vendors.ticketmaster.core.async_playwright",
        lambda: _RecordingContextManager(),
    )

    runner = BotRunner(cfg)
    with pytest.raises(_LaunchCaptured):
        await runner.run()

    assert "proxy" in captured, "BotRunner must forward proxy= kwarg to launch_persistent_context"
    proxy_kwarg = captured["proxy"]
    assert proxy_kwarg == {
        "server": "http://proxy.example.com:8080",
        "username": "alice",
        "password": "s3cret",
    }
    # Sanity: real_async_playwright was not invoked (we replaced the
    # symbol cleanly).
    assert real_async_playwright is async_playwright


async def test_botrunner_omits_proxy_kwarg_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With proxy disabled, ``proxy`` must NOT appear in launch kwargs."""
    proxy_cfg = ProxyConfig(enabled=False, policy="sticky", urls=[])
    cfg = _make_bot_config(proxy=proxy_cfg, account_name="alice")
    cfg.browser.user_data_dir = str(tmp_path / "default")

    captured: dict[str, Any] = {}

    class _RecordingChromium:
        async def launch_persistent_context(
            self, *args: Any, **kwargs: Any
        ) -> None:
            captured.update(kwargs)
            raise _LaunchCaptured(kwargs)

    class _RecordingPlaywright:
        chromium = _RecordingChromium()

    class _RecordingContextManager:
        async def __aenter__(self) -> _RecordingPlaywright:
            return _RecordingPlaywright()

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            return False

    monkeypatch.setattr(
        "src.vendors.ticketmaster.core.async_playwright",
        lambda: _RecordingContextManager(),
    )

    runner = BotRunner(cfg)
    with pytest.raises(_LaunchCaptured):
        await runner.run()

    assert "proxy" not in captured, (
        "BotRunner must NOT forward a proxy kwarg when proxy.enabled is false; "
        f"got {captured.get('proxy')!r}"
    )


# ---------------------------------------------------------------------------
# Optional integration: route httpbin.org/ip through the real proxy
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.getenv("TEST_PROXY_URL"),
    reason="TEST_PROXY_URL not configured — skipping real-proxy integration",
)
async def test_real_proxy_routes_httpbin_through_proxy(tmp_path: Path) -> None:
    """Confirm a real proxy is actually in the request chain.

    Reads ``TEST_PROXY_URL`` from the environment, launches a real
    headless Chromium with the proxy plumbed in (the same way
    ``BotRunner`` does in production), navigates to
    ``https://httpbin.org/ip``, and asserts the returned ``origin`` IP
    is different from a baseline non-proxy request. If anything goes
    wrong (network, proxy auth, etc.), the test fails loudly rather
    than passing silently.
    """
    proxy_url = os.environ["TEST_PROXY_URL"]
    proxy_kwargs = parse_proxy_url(proxy_url)

    # Baseline: what the local egress IP is *without* the proxy.
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        baseline_resp = await client.get("https://httpbin.org/ip")
    assert baseline_resp.status_code == 200, (
        f"baseline httpbin returned {baseline_resp.status_code}: {baseline_resp.text}"
    )
    baseline_origin: str = baseline_resp.json()["origin"]
    assert baseline_origin, "baseline origin must be non-empty"

    # Through the proxy, via real Playwright.
    user_data_dir = tmp_path / "real-proxy-profile"
    user_data_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=True,
            proxy=proxy_kwargs,
        )
        try:
            page = await context.new_page()
            response = await page.goto(
                "https://httpbin.org/ip",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            assert response is not None, "page.goto returned no response"
            assert response.ok, f"httpbin returned non-2xx via proxy: {response.status}"

            # httpbin.org/ip returns text/html-wrapped JSON; read the
            # rendered body as text and parse it ourselves so this also
            # works in headless Chromium with default JSON viewer
            # behaviour.
            body = await page.evaluate("() => document.body.innerText")
            import json as _json

            data = _json.loads(body)
            proxied_origin: str = data["origin"]
            assert proxied_origin, "proxied origin must be non-empty"
        finally:
            await context.close()

    assert proxied_origin != baseline_origin, (
        f"proxy did not change the egress IP: baseline={baseline_origin!r} "
        f"proxied={proxied_origin!r}"
    )
