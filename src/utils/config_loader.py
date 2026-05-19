"""Configuration loader and validator.

Resolution order (highest precedence last):

1. Built-in dataclass defaults.
2. Top-level ``config.yaml`` (the file passed to ``--config``).
3. Named profile YAML from ``config/profiles/<name>.yaml`` when ``--profile``
   is given. Profiles are themselves layered: ``profiles: [a, b]`` in either
   the main config or as a CLI list applies ``a`` first, then ``b``.
4. ``--set key.path=value`` CLI overrides.
5. ``${VAR}`` env-var expansion runs over the merged tree last so it always
   sees the final string values (regardless of which layer introduced them).

The single-event ``event: {...}`` key continues to load. Internally it is
normalised into a one-element ``events: [...]`` list so the rest of the
codebase only needs to handle one shape.
"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python <3.9
    ZoneInfo = None  # type: ignore[assignment,misc]

import yaml
from dotenv import load_dotenv

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_VALID_LOG_FORMATS = {"rich", "json"}
_DEFAULT_TRUSTED_HOSTS = (
    "ticketmaster.com",
    "ticketmaster.ca",
    "ticketmaster.sg",
    "livenation.com",
)
_ENV_VAR_RE = re.compile(r"^\$\{([A-Z0-9_]+)\}$")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PROFILES_DIR = _REPO_ROOT / "config" / "profiles"


@dataclass
class EventConfig:
    url: str
    on_sale_time: datetime | None = None
    refresh_interval_seconds: float = 2.0
    strict_host: bool = False


@dataclass
class SectionTargetConfig:
    section: str | None = None
    row_range: list[str] | None = None
    price_level_id: str | None = None


@dataclass
class PriceRangeConfig:
    """Bounds for the ``price_range`` strategy (both inclusive)."""

    min_price: float | None = None
    max_price: float | None = None


@dataclass
class MultiSectionConfig:
    """Ordered section-preference list for the ``multi_section`` strategy."""

    sections: list[str] = field(default_factory=list)


@dataclass
class ResaleFilterConfig:
    """Tuning for the ``resale_filter`` wrapper strategy.

    Exactly one of ``include_resale`` / ``exclude_resale`` must be true
    when ``tickets.strategy = 'resale_filter'``; setting both (or
    neither) is a configuration mistake and rejected at load time.
    """

    include_resale: bool = False
    exclude_resale: bool = False


@dataclass
class VFanAwareConfig:
    """Tuning for the ``vfan_aware`` wrapper strategy."""

    code: str | None = None


@dataclass
class InnerStrategyConfig:
    """Nested strategy definition used by wrapper strategies.

    Wrapper strategies (``resale_filter``, ``vfan_aware``) need a way to
    declare the strategy they delegate to. ``tickets.inner_strategy:`` is
    that nested block; it mirrors the top-level ticket-strategy shape so
    every wrappable strategy can be expressed without bespoke YAML keys.

    Only the ``strategy`` key is required; the other fields are reused
    only when the chosen inner strategy needs them (price_range needs
    ``price_range``, multi_section needs ``multi_section``, etc.).
    """

    strategy: str = "cheapest"
    section_target: SectionTargetConfig = field(default_factory=SectionTargetConfig)
    price_range: PriceRangeConfig = field(default_factory=PriceRangeConfig)
    multi_section: MultiSectionConfig = field(default_factory=MultiSectionConfig)
    max_price: float | None = None
    accessible_seats: bool = False


@dataclass
class TicketsConfig:
    quantity: int = 2
    strategy: str = "cheapest"
    section_target: SectionTargetConfig = field(default_factory=SectionTargetConfig)
    price_range: PriceRangeConfig = field(default_factory=PriceRangeConfig)
    multi_section: MultiSectionConfig = field(default_factory=MultiSectionConfig)
    resale_filter: ResaleFilterConfig = field(default_factory=ResaleFilterConfig)
    vfan_aware: VFanAwareConfig = field(default_factory=VFanAwareConfig)
    inner_strategy: InnerStrategyConfig | None = None
    max_price: float | None = None
    accessible_seats: bool = False


@dataclass
class PaymentConfig:
    card_last_four: str | None = None


@dataclass
class DeliveryConfig:
    # Substrings matched (case-insensitive) against delivery option labels.
    preferred: list[str] = field(
        default_factory=lambda: [
            "mobile entry",
            "mobile ticket",
            "ticketfast",
            "email delivery",
            "e-ticket",
        ]
    )
    # If true, fall back to the first delivery radio when no preferred label matches.
    allow_any: bool = False


@dataclass
class CheckoutConfig:
    auto_purchase: bool = False
    payment: PaymentConfig = field(default_factory=PaymentConfig)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    # Tolerance for the auto-purchase price safety check (proportional).
    price_tolerance: float = 0.05


@dataclass
class HumanizeMouseConfig:
    """Per-step settings for :func:`src.humanize.mouse.bezier_move`."""

    enabled: bool = True
    steps_min: int = 20
    steps_max: int = 40
    delay_ms_min: int = 8
    delay_ms_max: int = 25


@dataclass
class HumanizeTypingConfig:
    """Per-keystroke settings for :func:`src.humanize.typing.human_type`."""

    enabled: bool = True
    mean_ms: int = 70
    std_ms: int = 25
    min_ms: int = 20


@dataclass
class HumanizeConfig:
    """Container for humanisation subsystems.

    ``enabled`` acts as the master switch and preserves the historic
    ``timing.humanize: true|false`` shape; sub-blocks (``mouse``,
    ``typing``, future ``warmer``…) carry their own ``enabled`` flag so
    any subsystem can be disabled independently.

    ``__bool__`` returns ``enabled`` so the existing call-sites
    (``if cfg.timing.humanize:`` / ``bool(cfg.timing.humanize)``) keep
    working without changes.
    """

    enabled: bool = False
    mouse: HumanizeMouseConfig = field(default_factory=HumanizeMouseConfig)
    typing: HumanizeTypingConfig = field(default_factory=HumanizeTypingConfig)

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return bool(self.enabled)


@dataclass
class TimingConfig:
    page_timeout_seconds: float = 30.0
    queue_check_interval_seconds: float = 5.0
    action_delay_seconds: list[float] = field(default_factory=lambda: [0.5, 2.0])
    max_total_runtime_seconds: float = 3600.0
    # How long to leave a non-headless browser open after the flow ends so the
    # human can complete a manual checkout. Default 10 minutes.
    hold_open_seconds: float = 600.0
    # When ``humanize.enabled`` is false, random_human_delay calls are
    # no-ops (faster on-sale racing). The structured form also carries
    # per-subsystem settings (currently ``mouse``).
    humanize: HumanizeConfig = field(default_factory=HumanizeConfig)


@dataclass
class ArtifactsConfig:
    """Per-run artefact capture settings.

    Currently controls whether Playwright records a HAR of every
    network request during the run. When ``record_har`` is true, the
    runner threads ``record_har_path`` into
    ``Browser.launch_persistent_context`` at context creation time so
    a ``network.har`` file is finalised when the context closes.

    Tied to :class:`LoggingConfig.artifacts` so the YAML key path is
    ``logging.artifacts.record_har`` — keeping every observation/debug
    knob under one top-level section.
    """

    record_har: bool = False


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str | None = "logs/bot.log"
    format: str = "rich"
    artifacts: ArtifactsConfig = field(default_factory=ArtifactsConfig)


@dataclass
class NotificationsConfig:
    desktop: bool = True
    sound: bool = True


@dataclass
class StealthConfig:
    enabled: bool = True
    # If empty, a random UA from a small modern Chrome pool is used.
    user_agent: str | None = None
    extra_http_headers: dict[str, str] = field(default_factory=dict)
    webgl_vendor: str = "Intel Inc."
    webgl_renderer: str = "Intel Iris OpenGL Engine"
    # +/- pixel jitter applied to viewport at launch.
    viewport_jitter: int = 30


@dataclass
class BrowserConfig:
    headless: bool = False
    slow_mo_ms: int = 0
    user_data_dir: str = "sessions/default"
    locale: str = "en-US"
    timezone: str = "America/New_York"
    # Per-context fingerprint preset: ``desktop`` keeps the historic
    # 1366x900 / no-touch behaviour; ``mobile`` switches to a 390x844
    # touch-enabled iPhone profile (see :mod:`src.humanize.profile`).
    profile: str = "desktop"
    stealth: StealthConfig = field(default_factory=StealthConfig)


@dataclass
class AccountConfig:
    email: str
    password: str
    name: str = "default"


@dataclass
class ProxyConfig:
    """Per-account proxy plumbing for :class:`src.proxy.ProxyManager`.

    ``urls`` is the raw, full proxy URL list (each entry is a
    ``scheme://[USER[:PASSWORD]@]host:port`` string). Credentials are
    expressed inline in the URL — the manager parses them out into
    Playwright's separate ``username`` / ``password`` fields at
    resolve-time so config stays compact and consistent with proxy
    providers that hand out single-line connection strings.

    ``policy`` controls how URLs are mapped to accounts:

    * ``sticky`` (default) — deterministic per-account assignment.
    * ``round_robin`` — round-robin across the proxy list at every
      ``resolve()`` call.

    ``enabled`` is a master switch. Even with URLs in the list, a
    ``false`` flag forces :meth:`ProxyManager.resolve` to return
    ``None`` so the runner falls back to direct egress.
    """

    enabled: bool = False
    policy: str = "sticky"
    urls: list[str] = field(default_factory=list)


@dataclass
class BotConfig:
    events: list[EventConfig]
    tickets: TicketsConfig
    checkout: CheckoutConfig
    timing: TimingConfig
    logging: LoggingConfig
    notifications: NotificationsConfig
    browser: BrowserConfig
    accounts: list[AccountConfig]
    proxy: ProxyConfig = field(default_factory=ProxyConfig)

    @property
    def event(self) -> EventConfig:
        """Backwards-compat: return the first event.

        Existing call-sites that pre-date the multi-event refactor read
        ``cfg.event``. With ``events: [...]`` normalisation, that always
        resolves to the first (and primary) event.
        """
        return self.events[0]


# ---------------------------------------------------------------------------
# Pure helpers (exposed for tests).
# ---------------------------------------------------------------------------


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` into ``base`` without mutating either.

    Dict values are merged key-by-key; every other type (including lists)
    is replaced wholesale. The returned dict shares no mutable state with
    the inputs.
    """
    result: dict[str, Any] = copy.deepcopy(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _expand_env_in_obj(obj: Any) -> Any:
    """Recursively expand ``${VAR}`` placeholders in every string in ``obj``."""
    if isinstance(obj, dict):
        return {k: _expand_env_in_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_in_obj(item) for item in obj]
    if isinstance(obj, str):
        return _expand_env(obj)
    return obj


def _parse_datetime(value: Any, fallback_tz: str | None = None) -> datetime | None:
    """Parse a datetime; if naive and ``fallback_tz`` is set, attach it."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"event.on_sale_time is not valid ISO 8601: {value!r}") from exc
    else:
        raise ValueError(f"Invalid datetime value for event.on_sale_time: {value!r}")

    if dt.tzinfo is None:
        if fallback_tz and ZoneInfo is not None:
            try:
                dt = dt.replace(tzinfo=ZoneInfo(fallback_tz))
            except Exception as exc:  # noqa: BLE001
                raise ValueError(
                    f"Could not resolve browser.timezone {fallback_tz!r} for naive on_sale_time"
                ) from exc
        else:
            raise ValueError(
                "event.on_sale_time is naive (no timezone). "
                "Either include an offset (e.g. '2026-06-01T10:00:00-05:00') "
                "or set browser.timezone so it can be localized."
            )
    return dt


def _coerce_optional_float(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric, got {value!r}") from exc


def _expand_env(value: Any) -> Any:
    """Expand a ``${VAR}`` placeholder to the env var's value if it matches."""
    if not isinstance(value, str):
        return value
    match = _ENV_VAR_RE.match(value.strip())
    if not match:
        return value
    return os.getenv(match.group(1), "")


def _validate_event_url(url: str, strict: bool) -> None:
    parsed = urlparse(url)
    if not parsed.scheme.startswith("http"):
        raise ValueError(f"event.url must be an http(s) URL, got {url!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError(f"event.url has no host: {url!r}")
    trusted = any(host == h or host.endswith("." + h) for h in _DEFAULT_TRUSTED_HOSTS)
    if not trusted:
        msg = (
            f"event.url host {host!r} is not a known Ticketmaster/Live Nation host. "
            "Set event.strict_host=false to bypass."
        )
        if strict:
            raise ValueError(msg)
        import logging

        logging.getLogger("ticketmaster-bot").warning(msg)


# ---------------------------------------------------------------------------
# Profile loading.
# ---------------------------------------------------------------------------


def _resolve_profile_path(name: str, profiles_dir: Path) -> Path:
    """Return the path to ``<profiles_dir>/<name>.yaml`` or raise."""
    candidate = profiles_dir / f"{name}.yaml"
    if not candidate.is_file():
        raise FileNotFoundError(
            f"Profile {name!r} not found at {candidate}. Add the file or remove --profile."
        )
    return candidate


def _load_profile(name: str, profiles_dir: Path) -> dict[str, Any]:
    path = _resolve_profile_path(name, profiles_dir)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Profile {name!r} ({path}) must be a YAML mapping")
    return raw


# ---------------------------------------------------------------------------
# Parsing the merged dict into typed dataclasses.
# ---------------------------------------------------------------------------


def _parse_browser(raw: dict[str, Any]) -> BrowserConfig:
    browser_raw = raw.get("browser", {}) or {}
    stealth_raw = browser_raw.get("stealth", {}) or {}
    extra_headers = stealth_raw.get("extra_http_headers") or {}
    if not isinstance(extra_headers, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in extra_headers.items()
    ):
        raise ValueError("browser.stealth.extra_http_headers must be {str: str}")
    stealth = StealthConfig(
        enabled=bool(stealth_raw.get("enabled", True)),
        user_agent=stealth_raw.get("user_agent"),
        extra_http_headers=dict(extra_headers),
        webgl_vendor=str(stealth_raw.get("webgl_vendor", "Intel Inc.")),
        webgl_renderer=str(stealth_raw.get("webgl_renderer", "Intel Iris OpenGL Engine")),
        viewport_jitter=int(stealth_raw.get("viewport_jitter", 30)),
    )
    profile_name = str(browser_raw.get("profile", "desktop"))
    # Imported lazily to avoid a circular import between humanize.profile
    # (which references this module's BrowserConfig in TYPE_CHECKING) and
    # the loader itself. The validation set is cheap to evaluate at load
    # time and produces a precise error for bad config names.
    from ..humanize.profile import VALID_PROFILES

    if profile_name not in VALID_PROFILES:
        raise ValueError(
            f"browser.profile {profile_name!r} is invalid. "
            f"Valid options: {', '.join(sorted(VALID_PROFILES))}"
        )
    return BrowserConfig(
        headless=bool(browser_raw.get("headless", False)),
        slow_mo_ms=int(browser_raw.get("slow_mo_ms", 0)),
        user_data_dir=browser_raw.get("user_data_dir", "sessions/default"),
        locale=browser_raw.get("locale", "en-US"),
        timezone=browser_raw.get("timezone", "America/New_York"),
        profile=profile_name,
        stealth=stealth,
    )


def _parse_events(raw: dict[str, Any], browser_tz: str) -> list[EventConfig]:
    """Return one ``EventConfig`` per entry in the merged ``events`` list.

    The legacy ``event: {...}`` key is normalised into a one-element list
    when ``events:`` is absent. If both are present, ``events:`` wins.
    """
    if "events" in raw and raw["events"] is not None:
        events_raw = raw["events"]
        if not isinstance(events_raw, list):
            raise ValueError("config.events must be a list of event mappings")
        if not events_raw:
            raise ValueError("config.events must contain at least one event")
    elif "event" in raw and raw["event"] is not None:
        events_raw = [raw["event"]]
    else:
        raise ValueError(
            "config must define either 'event: {url: ...}' or 'events: [{url: ...}, ...]'"
        )

    parsed: list[EventConfig] = []
    for i, entry in enumerate(events_raw):
        if not isinstance(entry, dict):
            raise ValueError(f"events[{i}] must be a mapping, got {type(entry).__name__}")
        if "url" not in entry:
            raise ValueError(f"events[{i}].url is required")
        strict_host = bool(entry.get("strict_host", False))
        _validate_event_url(entry["url"], strict=strict_host)
        parsed.append(
            EventConfig(
                url=entry["url"],
                on_sale_time=_parse_datetime(entry.get("on_sale_time"), fallback_tz=browser_tz),
                refresh_interval_seconds=float(entry.get("refresh_interval_seconds", 2.0)),
                strict_host=strict_host,
            )
        )
    return parsed


_LEAF_STRATEGIES: frozenset[str] = frozenset(
    {
        "cheapest",
        "best_available",
        "section_target",
        "price_range",
        "multi_section",
        "accessible",
        "seat_quality",
    }
)
_WRAPPER_STRATEGIES: frozenset[str] = frozenset({"resale_filter", "vfan_aware"})
_VALID_STRATEGIES: frozenset[str] = _LEAF_STRATEGIES | _WRAPPER_STRATEGIES


def _parse_section_target(raw: dict[str, Any], prefix: str) -> SectionTargetConfig:
    section_target_raw = raw.get("section_target", {}) or {}
    row_range_raw = section_target_raw.get("row_range")
    if row_range_raw is not None:
        if (
            not isinstance(row_range_raw, list)
            or len(row_range_raw) != 2
            or not all(isinstance(r, str) for r in row_range_raw)
        ):
            raise ValueError(f"{prefix}.section_target.row_range must be [low, high] strings")
    return SectionTargetConfig(
        section=section_target_raw.get("section"),
        row_range=row_range_raw,
        price_level_id=section_target_raw.get("price_level_id"),
    )


def _parse_price_range(raw: dict[str, Any], prefix: str) -> PriceRangeConfig:
    price_range_raw = raw.get("price_range", {}) or {}
    if not isinstance(price_range_raw, dict):
        raise ValueError(f"{prefix}.price_range must be a mapping with min_price/max_price")
    return PriceRangeConfig(
        min_price=_coerce_optional_float(
            price_range_raw.get("min_price"), f"{prefix}.price_range.min_price"
        ),
        max_price=_coerce_optional_float(
            price_range_raw.get("max_price"), f"{prefix}.price_range.max_price"
        ),
    )


def _parse_multi_section(raw: dict[str, Any], prefix: str) -> MultiSectionConfig:
    multi_section_raw = raw.get("multi_section", {}) or {}
    if not isinstance(multi_section_raw, dict):
        raise ValueError(f"{prefix}.multi_section must be a mapping with a sections list")
    sections_raw = multi_section_raw.get("sections")
    if sections_raw is None:
        return MultiSectionConfig(sections=[])
    if not isinstance(sections_raw, list) or not all(
        isinstance(s, str) and s.strip() for s in sections_raw
    ):
        raise ValueError(f"{prefix}.multi_section.sections must be a list of non-empty strings")
    return MultiSectionConfig(sections=[s.strip() for s in sections_raw])


def _validate_strategy_constraints(
    *,
    strategy: str,
    price_range: PriceRangeConfig,
    multi_section: MultiSectionConfig,
    prefix: str,
) -> None:
    """Apply the per-leaf-strategy required-field validation."""
    if strategy not in _VALID_STRATEGIES:
        raise ValueError(
            f"Invalid {prefix}.strategy: {strategy!r}. "
            f"Must be one of: {', '.join(sorted(_VALID_STRATEGIES))}"
        )
    if strategy == "price_range":
        if price_range.min_price is None and price_range.max_price is None:
            raise ValueError(
                f"{prefix}.strategy='price_range' requires at least one of "
                f"{prefix}.price_range.min_price or {prefix}.price_range.max_price"
            )
        if (
            price_range.min_price is not None
            and price_range.max_price is not None
            and price_range.min_price > price_range.max_price
        ):
            raise ValueError(
                f"{prefix}.price_range.min_price must be <= {prefix}.price_range.max_price"
            )
    if strategy == "multi_section" and not multi_section.sections:
        raise ValueError(
            f"{prefix}.strategy='multi_section' requires "
            f"{prefix}.multi_section.sections to be a non-empty list"
        )


def _parse_inner_strategy(raw: dict[str, Any] | None, prefix: str) -> InnerStrategyConfig:
    """Parse the nested ``tickets.inner_strategy:`` block.

    Wrapper strategies (``resale_filter``, ``vfan_aware``) must be told
    which inner strategy to delegate to; the same field shape used by
    ``tickets:`` itself is accepted here so any leaf strategy can be
    wrapped without inventing new schema keys.
    """
    if raw is None:
        raise ValueError(
            f"{prefix}.inner_strategy is required when "
            f"{prefix.split('.')[0]}.strategy wraps another strategy "
            "(e.g. 'resale_filter', 'vfan_aware')"
        )
    if not isinstance(raw, dict):
        raise ValueError(f"{prefix}.inner_strategy must be a mapping")

    strategy = raw.get("strategy", "cheapest")
    if strategy in _WRAPPER_STRATEGIES:
        raise ValueError(
            f"{prefix}.inner_strategy.strategy={strategy!r} is itself a "
            "wrapper; nested wrappers are not supported. Use a leaf "
            "strategy (e.g. 'cheapest', 'price_range') instead."
        )
    section_target = _parse_section_target(raw, prefix)
    price_range = _parse_price_range(raw, prefix)
    multi_section = _parse_multi_section(raw, prefix)
    _validate_strategy_constraints(
        strategy=strategy,
        price_range=price_range,
        multi_section=multi_section,
        prefix=prefix,
    )
    return InnerStrategyConfig(
        strategy=strategy,
        section_target=section_target,
        price_range=price_range,
        multi_section=multi_section,
        max_price=_coerce_optional_float(raw.get("max_price"), f"{prefix}.max_price"),
        accessible_seats=bool(raw.get("accessible_seats", False)),
    )


def _parse_resale_filter(raw: dict[str, Any], prefix: str) -> ResaleFilterConfig:
    rf_raw = raw.get("resale_filter", {}) or {}
    if not isinstance(rf_raw, dict):
        raise ValueError(f"{prefix}.resale_filter must be a mapping")
    return ResaleFilterConfig(
        include_resale=bool(rf_raw.get("include_resale", False)),
        exclude_resale=bool(rf_raw.get("exclude_resale", False)),
    )


def _parse_vfan_aware(raw: dict[str, Any], prefix: str) -> VFanAwareConfig:
    vf_raw = raw.get("vfan_aware", {}) or {}
    if not isinstance(vf_raw, dict):
        raise ValueError(f"{prefix}.vfan_aware must be a mapping")
    code = vf_raw.get("code")
    if code is not None and not isinstance(code, str):
        raise ValueError(f"{prefix}.vfan_aware.code must be a string")
    return VFanAwareConfig(code=code)


def _parse_tickets(raw: dict[str, Any]) -> TicketsConfig:
    tickets_raw = raw.get("tickets", {}) or {}
    section_target = _parse_section_target(tickets_raw, "tickets")
    price_range = _parse_price_range(tickets_raw, "tickets")
    multi_section = _parse_multi_section(tickets_raw, "tickets")
    resale_filter = _parse_resale_filter(tickets_raw, "tickets")
    vfan_aware = _parse_vfan_aware(tickets_raw, "tickets")

    strategy = tickets_raw.get("strategy", "cheapest")
    inner_strategy_raw = tickets_raw.get("inner_strategy")
    inner_strategy: InnerStrategyConfig | None
    if strategy in _WRAPPER_STRATEGIES:
        inner_strategy = _parse_inner_strategy(inner_strategy_raw, "tickets.inner_strategy")
    else:
        if inner_strategy_raw is not None:
            # Parse for validity but the field is meaningful only for
            # wrapper strategies; we still surface mistakes early.
            inner_strategy = _parse_inner_strategy(inner_strategy_raw, "tickets.inner_strategy")
        else:
            inner_strategy = None

    tickets = TicketsConfig(
        quantity=int(tickets_raw.get("quantity", 2)),
        strategy=strategy,
        section_target=section_target,
        price_range=price_range,
        multi_section=multi_section,
        resale_filter=resale_filter,
        vfan_aware=vfan_aware,
        inner_strategy=inner_strategy,
        max_price=_coerce_optional_float(tickets_raw.get("max_price"), "tickets.max_price"),
        accessible_seats=bool(tickets_raw.get("accessible_seats", False)),
    )

    _validate_strategy_constraints(
        strategy=tickets.strategy,
        price_range=tickets.price_range,
        multi_section=tickets.multi_section,
        prefix="tickets",
    )

    if tickets.strategy == "resale_filter":
        if tickets.resale_filter.include_resale and tickets.resale_filter.exclude_resale:
            raise ValueError(
                "tickets.resale_filter: set exactly one of include_resale or "
                "exclude_resale, not both"
            )
        if not tickets.resale_filter.include_resale and not tickets.resale_filter.exclude_resale:
            raise ValueError(
                "tickets.strategy='resale_filter' requires "
                "tickets.resale_filter.include_resale=true or "
                "tickets.resale_filter.exclude_resale=true"
            )

    if tickets.strategy == "vfan_aware":
        code = tickets.vfan_aware.code
        if not code or not code.strip():
            raise ValueError(
                "tickets.strategy='vfan_aware' requires a non-empty tickets.vfan_aware.code"
            )

    if not 1 <= tickets.quantity <= 8:
        raise ValueError(f"tickets.quantity must be between 1 and 8, got {tickets.quantity}")
    return tickets


def _parse_checkout(raw: dict[str, Any]) -> CheckoutConfig:
    checkout_raw = raw.get("checkout", {}) or {}
    payment_raw = checkout_raw.get("payment", {}) or {}
    delivery_raw = checkout_raw.get("delivery", {}) or {}
    preferred = delivery_raw.get("preferred")
    if preferred is not None and (
        not isinstance(preferred, list) or not all(isinstance(s, str) for s in preferred)
    ):
        raise ValueError("checkout.delivery.preferred must be a list of strings")
    delivery_cfg = DeliveryConfig()
    if preferred is not None:
        delivery_cfg.preferred = [s.lower() for s in preferred]
    if "allow_any" in delivery_raw:
        delivery_cfg.allow_any = bool(delivery_raw["allow_any"])
    return CheckoutConfig(
        auto_purchase=bool(checkout_raw.get("auto_purchase", False)),
        payment=PaymentConfig(card_last_four=payment_raw.get("card_last_four")),
        delivery=delivery_cfg,
        price_tolerance=float(checkout_raw.get("price_tolerance", 0.05)),
    )


def _parse_humanize(raw: Any) -> HumanizeConfig:
    """Parse ``timing.humanize`` accepting both bool and nested-dict shapes.

    The historic shape is a plain bool (``humanize: true``); new code
    uses a nested mapping with per-subsystem settings (``mouse``…). Both
    must keep working so existing configs/profiles don't have to change.
    """
    if raw is None or isinstance(raw, bool):
        return HumanizeConfig(enabled=bool(raw))
    if not isinstance(raw, dict):
        raise ValueError(f"timing.humanize must be a bool or mapping, got {type(raw).__name__}")
    mouse_raw = raw.get("mouse", {}) or {}
    if not isinstance(mouse_raw, dict):
        raise ValueError(f"timing.humanize.mouse must be a mapping, got {type(mouse_raw).__name__}")
    defaults = HumanizeMouseConfig()
    steps_min = int(mouse_raw.get("steps_min", defaults.steps_min))
    steps_max = int(mouse_raw.get("steps_max", defaults.steps_max))
    delay_ms_min = int(mouse_raw.get("delay_ms_min", defaults.delay_ms_min))
    delay_ms_max = int(mouse_raw.get("delay_ms_max", defaults.delay_ms_max))
    if steps_min < 1 or steps_max < steps_min:
        raise ValueError("timing.humanize.mouse.steps_min/steps_max must satisfy 1 <= min <= max")
    if delay_ms_min < 0 or delay_ms_max < delay_ms_min:
        raise ValueError(
            "timing.humanize.mouse.delay_ms_min/delay_ms_max must satisfy 0 <= min <= max"
        )
    mouse_cfg = HumanizeMouseConfig(
        enabled=bool(mouse_raw.get("enabled", defaults.enabled)),
        steps_min=steps_min,
        steps_max=steps_max,
        delay_ms_min=delay_ms_min,
        delay_ms_max=delay_ms_max,
    )
    typing_raw = raw.get("typing", {}) or {}
    if not isinstance(typing_raw, dict):
        raise ValueError(
            f"timing.humanize.typing must be a mapping, got {type(typing_raw).__name__}"
        )
    typing_defaults = HumanizeTypingConfig()
    mean_ms = int(typing_raw.get("mean_ms", typing_defaults.mean_ms))
    std_ms = int(typing_raw.get("std_ms", typing_defaults.std_ms))
    min_ms = int(typing_raw.get("min_ms", typing_defaults.min_ms))
    if mean_ms <= 0:
        raise ValueError("timing.humanize.typing.mean_ms must be > 0")
    if std_ms < 0:
        raise ValueError("timing.humanize.typing.std_ms must be >= 0")
    if min_ms < 0:
        raise ValueError("timing.humanize.typing.min_ms must be >= 0")
    typing_cfg = HumanizeTypingConfig(
        enabled=bool(typing_raw.get("enabled", typing_defaults.enabled)),
        mean_ms=mean_ms,
        std_ms=std_ms,
        min_ms=min_ms,
    )
    return HumanizeConfig(
        enabled=bool(raw.get("enabled", False)),
        mouse=mouse_cfg,
        typing=typing_cfg,
    )


def _parse_timing(raw: dict[str, Any]) -> TimingConfig:
    timing_raw = raw.get("timing", {}) or {}
    delays = timing_raw.get("action_delay_seconds", [0.5, 2.0])
    if not isinstance(delays, list) or len(delays) != 2:
        raise ValueError("timing.action_delay_seconds must be [min, max] list")
    try:
        delay_min, delay_max = float(delays[0]), float(delays[1])
    except (TypeError, ValueError) as exc:
        raise ValueError("timing.action_delay_seconds must be numeric") from exc
    if delay_min < 0 or delay_max < delay_min:
        raise ValueError("timing.action_delay_seconds must satisfy 0 <= min <= max")
    return TimingConfig(
        page_timeout_seconds=float(timing_raw.get("page_timeout_seconds", 30.0)),
        queue_check_interval_seconds=float(timing_raw.get("queue_check_interval_seconds", 5.0)),
        action_delay_seconds=[delay_min, delay_max],
        max_total_runtime_seconds=float(timing_raw.get("max_total_runtime_seconds", 3600.0)),
        hold_open_seconds=float(timing_raw.get("hold_open_seconds", 600.0)),
        humanize=_parse_humanize(timing_raw.get("humanize", False)),
    )


def _parse_logging(raw: dict[str, Any]) -> LoggingConfig:
    logging_raw = raw.get("logging", {}) or {}
    level = str(logging_raw.get("level", "INFO")).upper()
    if level not in _VALID_LOG_LEVELS:
        raise ValueError(f"logging.level must be one of {sorted(_VALID_LOG_LEVELS)}, got {level!r}")
    fmt = str(logging_raw.get("format", "rich")).lower()
    if fmt not in _VALID_LOG_FORMATS:
        raise ValueError(f"logging.format must be one of {sorted(_VALID_LOG_FORMATS)}, got {fmt!r}")
    artifacts_raw = logging_raw.get("artifacts", {}) or {}
    if not isinstance(artifacts_raw, dict):
        raise ValueError(f"logging.artifacts must be a mapping, got {type(artifacts_raw).__name__}")
    artifacts = ArtifactsConfig(record_har=bool(artifacts_raw.get("record_har", False)))
    return LoggingConfig(
        level=level,
        file=logging_raw.get("file", "logs/bot.log"),
        format=fmt,
        artifacts=artifacts,
    )


def _parse_notifications(raw: dict[str, Any]) -> NotificationsConfig:
    notif_raw = raw.get("notifications", {}) or {}
    return NotificationsConfig(
        desktop=bool(notif_raw.get("desktop", True)),
        sound=bool(notif_raw.get("sound", True)),
    )


def _parse_proxy(raw: dict[str, Any]) -> ProxyConfig:
    """Parse the top-level ``proxy:`` block into :class:`ProxyConfig`.

    The shape mirrors the rest of the config: a master ``enabled``
    flag, an explicit ``policy`` (``sticky`` or ``round_robin``), and a
    flat list of proxy URLs. URL syntax validation happens later inside
    :class:`src.proxy.ProxyManager` so the loader can keep its
    dependency on the proxy package zero (the proxy module imports
    :class:`ProxyConfig` from here, not the other way around).
    """
    proxy_raw = raw.get("proxy", {}) or {}
    if not isinstance(proxy_raw, dict):
        raise ValueError(
            f"proxy must be a mapping with keys enabled/policy/urls, got {type(proxy_raw).__name__}"
        )
    urls_raw = proxy_raw.get("urls", [])
    if urls_raw is None:
        urls_raw = []
    if not isinstance(urls_raw, list) or not all(isinstance(u, str) for u in urls_raw):
        raise ValueError("proxy.urls must be a list of strings")
    urls: list[str] = [u.strip() for u in urls_raw if u and u.strip()]
    policy = str(proxy_raw.get("policy", "sticky"))
    # The set of valid policies is owned by src.proxy.manager. Importing
    # it here at parse-time would create an unnecessary import cycle
    # between config_loader and the proxy package; instead, we validate
    # against the same literal set used in the manager and let the
    # manager re-validate at construction time as a belt-and-braces
    # check.
    if policy not in {"sticky", "round_robin"}:
        raise ValueError(f"proxy.policy must be 'sticky' or 'round_robin', got {policy!r}")
    return ProxyConfig(
        enabled=bool(proxy_raw.get("enabled", False)),
        policy=policy,
        urls=urls,
    )


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def load_config(
    config_path: str | Path = "config/config.yaml",
    accounts_path: str | Path = "config/accounts.yaml",
    *,
    profile: str | None = None,
    overrides: dict[str, Any] | None = None,
    profiles_dir: str | Path | None = None,
) -> BotConfig:
    """Load and validate config, applying overlays in order.

    Parameters
    ----------
    config_path:
        Main config YAML.
    accounts_path:
        Accounts YAML (optional; env-only auth supported when missing).
    profile:
        Name of a profile YAML to overlay on top of the main config. Looked
        up at ``<profiles_dir>/<name>.yaml``.
    overrides:
        Arbitrary nested dict applied last, before env expansion. Produced
        by :func:`src.cli.parse_set_overrides` for ``--set`` flags.
    profiles_dir:
        Directory containing profile YAMLs. Defaults to
        ``<repo>/config/profiles``.
    """
    load_dotenv()

    config_path = Path(config_path)
    accounts_path = Path(accounts_path)
    profiles_root = Path(profiles_dir) if profiles_dir else _DEFAULT_PROFILES_DIR

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Config file {config_path} must be a YAML mapping")

    # 1. Profile overlay. Names come from the explicit kwarg first, then
    #    from a top-level `profiles: [a, b]` list in the main config (each
    #    applied in order so the last name wins for conflicting keys).
    profile_names: list[str] = []
    config_profiles = raw.get("profiles")
    if isinstance(config_profiles, list):
        for entry in config_profiles:
            if isinstance(entry, str) and entry:
                profile_names.append(entry)
    if profile:
        profile_names.append(profile)
    # Strip the bookkeeping key so it doesn't bleed into the parsed dict.
    raw.pop("profiles", None)

    merged: dict[str, Any] = raw
    for name in profile_names:
        merged = _deep_merge(merged, _load_profile(name, profiles_root))

    # 2. CLI --set overrides.
    if overrides:
        merged = _deep_merge(merged, overrides)

    # 3. ${VAR} env expansion runs last over the fully merged tree so
    #    placeholders introduced at any layer see the final env values.
    merged = _expand_env_in_obj(merged)

    # 4. Parse into dataclasses.
    browser = _parse_browser(merged)
    events = _parse_events(merged, browser_tz=browser.timezone)
    tickets = _parse_tickets(merged)
    checkout = _parse_checkout(merged)
    timing = _parse_timing(merged)
    logging_cfg = _parse_logging(merged)
    notifications = _parse_notifications(merged)
    proxy = _parse_proxy(merged)
    accounts = _load_accounts(accounts_path)

    if checkout.auto_purchase:
        # Imported lazily so the loader doesn't pay the import cost on every
        # call when the override isn't relevant.
        from . import purchase_guard

        if not purchase_guard.purchase_allowed():
            import logging as _logging

            _logging.getLogger("ticketmaster-bot").warning(
                "auto_purchase=true is configured but the PurchaseGuard env "
                "override is not set; place-order clicks will still be blocked. "
                "Set %s=%s to enable real purchases.",
                purchase_guard.PURCHASE_OVERRIDE_ENV,
                purchase_guard.PURCHASE_OVERRIDE_VALUE,
            )

    return BotConfig(
        events=events,
        tickets=tickets,
        checkout=checkout,
        timing=timing,
        logging=logging_cfg,
        notifications=notifications,
        browser=browser,
        accounts=accounts,
        proxy=proxy,
    )


def _load_accounts(path: Path) -> list[AccountConfig]:
    if not path.exists():
        # Allow env-only auth
        email = os.getenv("TM_EMAIL")
        password = os.getenv("TM_PASSWORD")
        if email and password:
            return [AccountConfig(email=email, password=password, name="env")]
        return []

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    accounts_raw = raw.get("accounts", [])
    accounts: list[AccountConfig] = []
    for entry in accounts_raw:
        # Expand ${VAR} in every string field.
        email = _expand_env(entry.get("email"))
        password = _expand_env(entry.get("password"))
        name = _expand_env(entry.get("name"))
        if not email or not password:
            continue
        accounts.append(
            AccountConfig(
                email=email,
                password=password,
                name=name or email.split("@")[0],
            )
        )
    return accounts


def config_to_yaml(config: BotConfig) -> str:
    """Serialize ``BotConfig`` (including all events) back into YAML.

    Used by ``--explain``. ``datetime`` fields are converted to ISO 8601
    strings so the result is round-trippable through ``yaml.safe_load``.
    """
    data = asdict(config)
    # Drop the @property `event` shim from the output; only emit `events`.
    data = {k: v for k, v in data.items() if k != "event"}
    for entry in data.get("events", []):
        on_sale = entry.get("on_sale_time")
        if isinstance(on_sale, datetime):
            entry["on_sale_time"] = on_sale.isoformat()
    return yaml.safe_dump(data, sort_keys=False)


__all__ = [
    "AccountConfig",
    "ArtifactsConfig",
    "BotConfig",
    "BrowserConfig",
    "CheckoutConfig",
    "DeliveryConfig",
    "EventConfig",
    "HumanizeConfig",
    "HumanizeMouseConfig",
    "HumanizeTypingConfig",
    "InnerStrategyConfig",
    "LoggingConfig",
    "MultiSectionConfig",
    "NotificationsConfig",
    "PaymentConfig",
    "PriceRangeConfig",
    "ProxyConfig",
    "ResaleFilterConfig",
    "SectionTargetConfig",
    "StealthConfig",
    "TicketsConfig",
    "TimingConfig",
    "VFanAwareConfig",
    "config_to_yaml",
    "load_config",
]
