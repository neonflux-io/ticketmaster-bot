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
_DEFAULT_TRUSTED_HOSTS = ("ticketmaster.com", "ticketmaster.ca", "livenation.com")
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
class TicketsConfig:
    quantity: int = 2
    strategy: str = "cheapest"
    section_target: SectionTargetConfig = field(default_factory=SectionTargetConfig)
    price_range: PriceRangeConfig = field(default_factory=PriceRangeConfig)
    multi_section: MultiSectionConfig = field(default_factory=MultiSectionConfig)
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
class TimingConfig:
    page_timeout_seconds: float = 30.0
    queue_check_interval_seconds: float = 5.0
    action_delay_seconds: list[float] = field(default_factory=lambda: [0.5, 2.0])
    max_total_runtime_seconds: float = 3600.0
    # How long to leave a non-headless browser open after the flow ends so the
    # human can complete a manual checkout. Default 10 minutes.
    hold_open_seconds: float = 600.0
    # When false, random_human_delay calls are no-ops (faster on-sale racing).
    humanize: bool = False


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str | None = "logs/bot.log"


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
    stealth: StealthConfig = field(default_factory=StealthConfig)


@dataclass
class AccountConfig:
    email: str
    password: str
    name: str = "default"


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
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
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
        raise ValueError(
            f"event.url must be an http(s) URL, got {url!r}"
        )
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
            f"Profile {name!r} not found at {candidate}. "
            f"Add the file or remove --profile."
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
        webgl_renderer=str(
            stealth_raw.get("webgl_renderer", "Intel Iris OpenGL Engine")
        ),
        viewport_jitter=int(stealth_raw.get("viewport_jitter", 30)),
    )
    return BrowserConfig(
        headless=bool(browser_raw.get("headless", False)),
        slow_mo_ms=int(browser_raw.get("slow_mo_ms", 0)),
        user_data_dir=browser_raw.get("user_data_dir", "sessions/default"),
        locale=browser_raw.get("locale", "en-US"),
        timezone=browser_raw.get("timezone", "America/New_York"),
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
                on_sale_time=_parse_datetime(
                    entry.get("on_sale_time"), fallback_tz=browser_tz
                ),
                refresh_interval_seconds=float(entry.get("refresh_interval_seconds", 2.0)),
                strict_host=strict_host,
            )
        )
    return parsed


def _parse_tickets(raw: dict[str, Any]) -> TicketsConfig:
    tickets_raw = raw.get("tickets", {}) or {}
    section_target_raw = tickets_raw.get("section_target", {}) or {}
    row_range_raw = section_target_raw.get("row_range")
    if row_range_raw is not None:
        if (
            not isinstance(row_range_raw, list)
            or len(row_range_raw) != 2
            or not all(isinstance(r, str) for r in row_range_raw)
        ):
            raise ValueError(
                "tickets.section_target.row_range must be [low, high] strings"
            )
    price_range_raw = tickets_raw.get("price_range", {}) or {}
    if not isinstance(price_range_raw, dict):
        raise ValueError(
            "tickets.price_range must be a mapping with min_price/max_price"
        )
    price_range = PriceRangeConfig(
        min_price=_coerce_optional_float(
            price_range_raw.get("min_price"), "tickets.price_range.min_price"
        ),
        max_price=_coerce_optional_float(
            price_range_raw.get("max_price"), "tickets.price_range.max_price"
        ),
    )

    multi_section_raw = tickets_raw.get("multi_section", {}) or {}
    if not isinstance(multi_section_raw, dict):
        raise ValueError(
            "tickets.multi_section must be a mapping with a sections list"
        )
    sections_raw = multi_section_raw.get("sections")
    if sections_raw is None:
        sections_list: list[str] = []
    else:
        if not isinstance(sections_raw, list) or not all(
            isinstance(s, str) and s.strip() for s in sections_raw
        ):
            raise ValueError(
                "tickets.multi_section.sections must be a list of non-empty strings"
            )
        sections_list = [s.strip() for s in sections_raw]
    multi_section = MultiSectionConfig(sections=sections_list)

    tickets = TicketsConfig(
        quantity=int(tickets_raw.get("quantity", 2)),
        strategy=tickets_raw.get("strategy", "cheapest"),
        section_target=SectionTargetConfig(
            section=section_target_raw.get("section"),
            row_range=row_range_raw,
            price_level_id=section_target_raw.get("price_level_id"),
        ),
        price_range=price_range,
        multi_section=multi_section,
        max_price=_coerce_optional_float(tickets_raw.get("max_price"), "tickets.max_price"),
        accessible_seats=bool(tickets_raw.get("accessible_seats", False)),
    )

    valid_strategies = {
        "cheapest",
        "best_available",
        "section_target",
        "price_range",
        "multi_section",
    }
    if tickets.strategy not in valid_strategies:
        raise ValueError(
            f"Invalid tickets.strategy: {tickets.strategy!r}. "
            f"Must be one of: {', '.join(sorted(valid_strategies))}"
        )

    if (
        tickets.strategy == "price_range"
        and tickets.price_range.min_price is None
        and tickets.price_range.max_price is None
    ):
        raise ValueError(
            "tickets.strategy='price_range' requires at least one of "
            "tickets.price_range.min_price or tickets.price_range.max_price"
        )
    if (
        tickets.strategy == "price_range"
        and tickets.price_range.min_price is not None
        and tickets.price_range.max_price is not None
        and tickets.price_range.min_price > tickets.price_range.max_price
    ):
        raise ValueError(
            "tickets.price_range.min_price must be <= tickets.price_range.max_price"
        )

    if tickets.strategy == "multi_section" and not tickets.multi_section.sections:
        raise ValueError(
            "tickets.strategy='multi_section' requires "
            "tickets.multi_section.sections to be a non-empty list"
        )

    if not 1 <= tickets.quantity <= 8:
        raise ValueError(
            f"tickets.quantity must be between 1 and 8, got {tickets.quantity}"
        )
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
        queue_check_interval_seconds=float(
            timing_raw.get("queue_check_interval_seconds", 5.0)
        ),
        action_delay_seconds=[delay_min, delay_max],
        max_total_runtime_seconds=float(
            timing_raw.get("max_total_runtime_seconds", 3600.0)
        ),
        hold_open_seconds=float(timing_raw.get("hold_open_seconds", 600.0)),
        humanize=bool(timing_raw.get("humanize", False)),
    )


def _parse_logging(raw: dict[str, Any]) -> LoggingConfig:
    logging_raw = raw.get("logging", {}) or {}
    level = str(logging_raw.get("level", "INFO")).upper()
    if level not in _VALID_LOG_LEVELS:
        raise ValueError(
            f"logging.level must be one of {sorted(_VALID_LOG_LEVELS)}, got {level!r}"
        )
    return LoggingConfig(
        level=level,
        file=logging_raw.get("file", "logs/bot.log"),
    )


def _parse_notifications(raw: dict[str, Any]) -> NotificationsConfig:
    notif_raw = raw.get("notifications", {}) or {}
    return NotificationsConfig(
        desktop=bool(notif_raw.get("desktop", True)),
        sound=bool(notif_raw.get("sound", True)),
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
    accounts = _load_accounts(accounts_path)

    return BotConfig(
        events=events,
        tickets=tickets,
        checkout=checkout,
        timing=timing,
        logging=logging_cfg,
        notifications=notifications,
        browser=browser,
        accounts=accounts,
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
    "BotConfig",
    "BrowserConfig",
    "CheckoutConfig",
    "DeliveryConfig",
    "EventConfig",
    "LoggingConfig",
    "MultiSectionConfig",
    "NotificationsConfig",
    "PaymentConfig",
    "PriceRangeConfig",
    "SectionTargetConfig",
    "StealthConfig",
    "TicketsConfig",
    "TimingConfig",
    "config_to_yaml",
    "load_config",
]
