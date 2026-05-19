"""Entry point for the Ticketmaster bot."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Allow `python src/main.py` and `python -m src.main` both to work
_here = Path(__file__).resolve().parent
if str(_here.parent) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from src.bot.core import BotRunner  # noqa: E402
from src.cli import parse_set_overrides  # noqa: E402
from src.utils.config_loader import config_to_yaml, load_config  # noqa: E402
from src.utils.logger import setup_logger  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ticketmaster ticket-buying bot")
    p.add_argument(
        "--config",
        "-c",
        default="config/config.yaml",
        help="Path to config file (default: config/config.yaml)",
    )
    p.add_argument(
        "--accounts",
        "-a",
        default="config/accounts.yaml",
        help="Path to accounts file (default: config/accounts.yaml)",
    )
    p.add_argument(
        "--account-name",
        default=None,
        help="Run only the account with this name (default: first account)",
    )
    p.add_argument(
        "--profile",
        default=None,
        help=(
            "Apply config/profiles/<name>.yaml on top of the main config. "
            "Ships with 'fast' and 'safe'."
        ),
    )
    p.add_argument(
        "--set",
        dest="set_overrides",
        action="append",
        default=[],
        metavar="KEY.PATH=VALUE",
        help=(
            "Override a config value at a dotted path. "
            "Repeatable, e.g. --set tickets.quantity=4 --set checkout.auto_purchase=false"
        ),
    )

    headless_group = p.add_mutually_exclusive_group()
    headless_group.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        default=None,
        help="Force headless mode regardless of config",
    )
    headless_group.add_argument(
        "--no-headless",
        dest="headless",
        action="store_false",
        help="Force a visible browser regardless of config",
    )

    purchase_group = p.add_mutually_exclusive_group()
    purchase_group.add_argument(
        "--auto-purchase",
        dest="auto_purchase",
        action="store_true",
        default=None,
        help="Force auto_purchase=true regardless of config (USE WITH CAUTION)",
    )
    purchase_group.add_argument(
        "--no-auto-purchase",
        dest="auto_purchase",
        action="store_false",
        help="Force auto_purchase=false regardless of config",
    )

    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate config and exit without launching a browser",
    )
    p.add_argument(
        "--explain",
        action="store_true",
        help=(
            "Dump the fully resolved config as YAML to stdout and exit. "
            "Useful for verifying profile/--set overlays before a real run."
        ),
    )
    return p.parse_args(argv)


async def main_async() -> int:
    args = parse_args()

    # Bootstrap a minimal logger so config-load errors are visible.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    bootstrap_log = logging.getLogger("ticketmaster-bot")

    try:
        overrides = parse_set_overrides(args.set_overrides)
    except ValueError as exc:
        # argparse-style error: print to stderr and exit 2.
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        config = load_config(
            args.config,
            args.accounts,
            profile=args.profile,
            overrides=overrides,
        )
    except Exception:
        bootstrap_log.exception("Failed to load config")
        return 2

    if args.headless is not None:
        config.browser.headless = args.headless
    if args.auto_purchase is not None:
        config.checkout.auto_purchase = args.auto_purchase

    # --explain runs before logging is reconfigured so the resolved YAML is
    # the *only* thing on stdout. Rich/RichHandler writes to stderr by
    # default, so callers can safely `python run.py --explain | yq ...`.
    if args.explain:
        sys.stdout.write(config_to_yaml(config))
        sys.stdout.flush()
        return 0

    log = setup_logger(level=config.logging.level, log_file=config.logging.file)
    log.info("=" * 60)
    log.info("Ticketmaster Bot starting")
    for i, evt in enumerate(config.events, start=1):
        log.info("Event %d: %s", i, evt.url)
    log.info(
        "Strategy: %s | Quantity: %d | Auto-purchase: %s",
        config.tickets.strategy,
        config.tickets.quantity,
        config.checkout.auto_purchase,
    )
    log.info("=" * 60)

    account = None
    if args.account_name:
        for a in config.accounts:
            if a.name == args.account_name:
                account = a
                break
        if account is None:
            log.error("No account named %r found", args.account_name)
            return 2

    if args.dry_run:
        log.info("Dry run: config validated successfully. Exiting before launch.")
        return 0

    runner = BotRunner(config, account=account)
    try:
        success = await runner.run()
    except KeyboardInterrupt:
        log.warning("Interrupted by user")
        return 130
    except Exception:  # noqa: BLE001
        log.exception("Bot crashed")
        return 1
    return 0 if success else 1


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
