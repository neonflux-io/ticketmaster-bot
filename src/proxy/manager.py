"""Per-account proxy assignment for Playwright launches.

:class:`ProxyManager` is the single entry point. Given a configured
:class:`~src.utils.config_loader.ProxyConfig` (a list of proxy URLs and
an assignment policy), :meth:`ProxyManager.resolve` returns the small
kwarg dict Playwright's ``launch_persistent_context`` expects::

    {"server": "http://host:port", "username": "u", "password": "p"}

The ``username``/``password`` keys are emitted **only** when the
underlying URL carries credentials, matching Playwright's own
``ProxySettings`` type where both fields are optional.

Two policies are supported:

* ``sticky`` — every call for a given ``account_name`` returns the same
  proxy. Mapping is deterministic across processes: it hashes
  ``account_name`` into the proxies list. New accounts seen at runtime
  pick the next never-assigned slot before any reuse happens, so a
  config with two accounts and two proxies always produces a 1-to-1
  mapping.
* ``round_robin`` — each call advances a monotonically increasing
  counter so concurrent accounts get spread evenly. Counter is
  per-:class:`ProxyManager` instance so a single ``BotRunner`` can run
  multiple accounts in sequence and rotate cleanly.

If the proxy list is empty *or* the ``enabled`` flag is false, every
:meth:`resolve` returns ``None`` so callers can use ``if proxy_kwargs is
not None`` as the gate on whether to pass ``proxy=...`` at all.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse

if TYPE_CHECKING:
    from ..utils.config_loader import ProxyConfig

log = logging.getLogger("ticketmaster-bot")


VALID_POLICIES: frozenset[str] = frozenset({"sticky", "round_robin"})


def parse_proxy_url(url: str) -> dict[str, str]:
    """Parse a proxy URL into Playwright's ``proxy=`` kwarg shape.

    Accepts URLs in the standard form
    ``scheme://[user[:password]@]host[:port][/path]``. The ``scheme://``
    prefix is required; bare ``host:port`` is rejected because
    Playwright's ProxySettings explicitly requires a scheme.

    Returns a mapping with ``server`` set to the credential-stripped
    URL (Playwright re-attaches credentials via ``username`` /
    ``password``). ``username`` and ``password`` are included **only**
    when present in the source URL.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("proxy URL must be a non-empty string")
    trimmed = url.strip()
    # ``urlparse`` is forgiving: ``"host:8080"`` parses as scheme=host,
    # path=8080 because Python interprets the first ``:`` as the scheme
    # boundary. Require the ``scheme://`` form explicitly so we reject
    # those before the more specific host/port checks.
    if "://" not in trimmed:
        raise ValueError(
            f"proxy URL {trimmed!r} is missing a scheme "
            "(e.g. 'http://', 'https://', 'socks5://')"
        )
    parsed = urlparse(trimmed)
    if not parsed.scheme:
        raise ValueError(
            f"proxy URL {url!r} is missing a scheme "
            "(e.g. 'http://', 'https://', 'socks5://')"
        )
    if not parsed.hostname:
        raise ValueError(f"proxy URL {url!r} is missing a host")

    netloc = parsed.hostname
    if parsed.port is not None:
        netloc = f"{parsed.hostname}:{parsed.port}"
    # Rebuild the server URL without credentials. Playwright reattaches
    # them via the separate username/password fields; embedding them in
    # the URL too would double-encode the auth header in some proxies.
    server = f"{parsed.scheme}://{netloc}"
    if parsed.path and parsed.path != "/":
        server += parsed.path

    out: dict[str, str] = {"server": server}
    if parsed.username:
        out["username"] = unquote(parsed.username)
    if parsed.password:
        out["password"] = unquote(parsed.password)
    return out


class ProxyManager:
    """Resolve a Playwright ``proxy=`` kwarg dict for a given account.

    The manager is constructed from a parsed :class:`ProxyConfig`
    (typically obtained from :func:`src.utils.config_loader.load_config`)
    so the validation of policy names / URL shapes already happened at
    config-load time. The manager itself only enforces the runtime
    invariants: the proxies list is non-empty, and lookups for a given
    ``account_name`` are stable under ``sticky`` policy.

    The class is thread-safe for the operations exposed
    (:meth:`resolve`); a single :class:`ProxyManager` can be shared
    across a :class:`~src.orchestrator.parallel.ParallelCoordinator`'s
    workers.
    """

    def __init__(self, config: ProxyConfig) -> None:
        self._config = config
        # Pre-validate every URL so a bad config explodes at construction
        # time, not at first ``resolve``.
        self._parsed: list[dict[str, str]] = [parse_proxy_url(u) for u in config.urls]
        if config.policy not in VALID_POLICIES:
            raise ValueError(
                f"proxy.policy must be one of {sorted(VALID_POLICIES)}, got {config.policy!r}"
            )
        self._policy: str = config.policy
        self._lock = threading.Lock()
        self._round_robin_index: int = 0
        # Stable account_name -> index mapping populated lazily on first
        # ``resolve`` so the assignment order matches the order accounts
        # first appear (predictable for users with N accounts and M
        # proxies).
        self._sticky_assignments: dict[str, int] = {}
        self._next_sticky_index: int = 0

    @property
    def enabled(self) -> bool:
        """``True`` when at least one proxy is configured.

        :meth:`resolve` short-circuits to ``None`` when this is
        ``False``; callers can use ``manager.enabled`` to skip the call
        entirely if they want to.
        """
        return bool(self._parsed) and self._config.enabled

    @property
    def policy(self) -> str:
        return self._policy

    @property
    def proxy_count(self) -> int:
        return len(self._parsed)

    def resolve(self, account_name: str | None) -> dict[str, str] | None:
        """Return the proxy kwargs for ``account_name`` or ``None``.

        Returns ``None`` when no proxies are configured or the manager
        is disabled, in which case callers must omit the ``proxy=``
        kwarg from ``launch_persistent_context``.
        """
        if not self.enabled:
            return None
        if not self._parsed:
            return None

        with self._lock:
            if self._policy == "round_robin":
                index = self._round_robin_index % len(self._parsed)
                self._round_robin_index += 1
            else:  # sticky
                key = account_name or ""
                if key not in self._sticky_assignments:
                    self._sticky_assignments[key] = (
                        self._next_sticky_index % len(self._parsed)
                    )
                    self._next_sticky_index += 1
                index = self._sticky_assignments[key]

            # Defensive copy so callers can't mutate the manager's
            # internal state by editing the returned dict in place.
            return dict(self._parsed[index])


__all__ = ["VALID_POLICIES", "ProxyManager", "parse_proxy_url"]
