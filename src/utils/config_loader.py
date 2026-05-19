"""Configuration loader and validator."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
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
class TicketsConfig:
    quantity: int = 2
    strategy: str = "cheapest"
    section_target: SectionTargetConfig = field(default_factory=SectionTargetConfig)
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
    event: EventConfig
    tickets: TicketsConfig
    checkout: CheckoutConfig
    timing: TimingConfig
    logging: LoggingConfig
    notifications: NotificationsConfig
    browser: BrowserConfig
    accounts: list[AccountConfig]


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


def load_config(
    config_path: str | Path = "config/config.yaml",
    accounts_path: str | Path = "config/accounts.yaml",
) -> BotConfig:
    """Load and validate config from YAML files."""
    load_dotenv()

    config_path = Path(config_path)
    accounts_path = Path(accounts_path)

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    browser_raw = raw.get("browser", {})
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
    browser = BrowserConfig(
        headless=bool(browser_raw.get("headless", False)),
        slow_mo_ms=int(browser_raw.get("slow_mo_ms", 0)),
        user_data_dir=browser_raw.get("user_data_dir", "sessions/default"),
        locale=browser_raw.get("locale", "en-US"),
        timezone=browser_raw.get("timezone", "America/New_York"),
        stealth=stealth,
    )

    event_raw = raw.get("event", {})
    if "url" not in event_raw:
        raise ValueError("config.event.url is required")
    strict_host = bool(event_raw.get("strict_host", False))
    _validate_event_url(event_raw["url"], strict=strict_host)

    event = EventConfig(
        url=event_raw["url"],
        on_sale_time=_parse_datetime(
            event_raw.get("on_sale_time"), fallback_tz=browser.timezone
        ),
        refresh_interval_seconds=float(event_raw.get("refresh_interval_seconds", 2.0)),
        strict_host=strict_host,
    )

    tickets_raw = raw.get("tickets", {})
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
    tickets = TicketsConfig(
        quantity=int(tickets_raw.get("quantity", 2)),
        strategy=tickets_raw.get("strategy", "cheapest"),
        section_target=SectionTargetConfig(
            section=section_target_raw.get("section"),
            row_range=row_range_raw,
            price_level_id=section_target_raw.get("price_level_id"),
        ),
        max_price=_coerce_optional_float(tickets_raw.get("max_price"), "tickets.max_price"),
        accessible_seats=bool(tickets_raw.get("accessible_seats", False)),
    )

    if tickets.strategy not in {"cheapest", "best_available", "section_target"}:
        raise ValueError(
            f"Invalid tickets.strategy: {tickets.strategy!r}. "
            "Must be one of: cheapest, best_available, section_target"
        )

    if not 1 <= tickets.quantity <= 8:
        raise ValueError(
            f"tickets.quantity must be between 1 and 8, got {tickets.quantity}"
        )

    checkout_raw = raw.get("checkout", {})
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
    checkout = CheckoutConfig(
        auto_purchase=bool(checkout_raw.get("auto_purchase", False)),
        payment=PaymentConfig(card_last_four=payment_raw.get("card_last_four")),
        delivery=delivery_cfg,
        price_tolerance=float(checkout_raw.get("price_tolerance", 0.05)),
    )

    timing_raw = raw.get("timing", {})
    delays = timing_raw.get("action_delay_seconds", [0.5, 2.0])
    if not isinstance(delays, list) or len(delays) != 2:
        raise ValueError("timing.action_delay_seconds must be [min, max] list")
    try:
        delay_min, delay_max = float(delays[0]), float(delays[1])
    except (TypeError, ValueError) as exc:
        raise ValueError("timing.action_delay_seconds must be numeric") from exc
    if delay_min < 0 or delay_max < delay_min:
        raise ValueError("timing.action_delay_seconds must satisfy 0 <= min <= max")
    timing = TimingConfig(
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

    logging_raw = raw.get("logging", {})
    level = str(logging_raw.get("level", "INFO")).upper()
    if level not in _VALID_LOG_LEVELS:
        raise ValueError(
            f"logging.level must be one of {sorted(_VALID_LOG_LEVELS)}, got {level!r}"
        )
    logging_cfg = LoggingConfig(
        level=level,
        file=logging_raw.get("file", "logs/bot.log"),
    )

    notif_raw = raw.get("notifications", {})
    notifications = NotificationsConfig(
        desktop=bool(notif_raw.get("desktop", True)),
        sound=bool(notif_raw.get("sound", True)),
    )

    accounts = _load_accounts(accounts_path)

    return BotConfig(
        event=event,
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

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

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
