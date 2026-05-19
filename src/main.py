"""Entry point for the Ticketmaster bot.

The argparse surface lives in :mod:`src.cli`. This module is responsible
for wiring the parsed namespace into config loading, vendor resolution,
account selection, and the runner launch path.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

# Allow `python src/main.py` and `python -m src.main` both to work
_here = Path(__file__).resolve().parent
if str(_here.parent) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from src.cli import parse_args, parse_events_flag, parse_set_overrides  # noqa: E402
from src.registry import vendors as vendor_registry  # noqa: E402
from src.utils.config_loader import config_to_yaml, load_config  # noqa: E402
from src.utils.logger import setup_logger  # noqa: E402


def _resolve_vendor_adapter(name: str) -> Any:
    """Look up the vendor adapter class for ``name`` or raise ValueError.

    The shipped Ticketmaster adapter is registered as a side effect of
    importing :mod:`src.vendors` (which we import here lazily so a CLI
    failure path doesn't pay the import cost twice).
    """
    import src.vendors  # noqa: F401  (registers TicketmasterAdapter)

    try:
        return vendor_registry.get(name)
    except Exception as exc:
        available = ", ".join(sorted(vendor_registry.all())) or "<none>"
        raise ValueError(f"Unknown vendor {name!r}. Registered vendors: {available}") from exc


async def main_async(args: argparse.Namespace | None = None) -> int:
    if args is None:
        args = parse_args()

    # Bootstrap a minimal logger so config-load errors are visible.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    bootstrap_log = logging.getLogger("ticketmaster-bot")

    # --- 1. Vendor validation up front ---------------------------------
    try:
        adapter_cls = _resolve_vendor_adapter(args.vendor)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # --- 2. Parse --set overrides --------------------------------------
    try:
        overrides = parse_set_overrides(args.set_overrides)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # --- 3. Apply --events as an overlay so it wins over file values ---
    event_urls = parse_events_flag(args.events)
    if event_urls:
        # Replace any baseline events / legacy event key wholesale.
        overrides["events"] = [{"url": url} for url in event_urls]
        # The legacy single-event key in the base file would otherwise
        # still win when 'events:' is absent from the merged tree.
        # Setting events:[...] in the overrides supersedes it.

    # --- 4. Load config (handles --profile and ${VAR} expansion) --------
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

    # --- 5. --explain prints resolved YAML and exits --------------------
    # Logging is intentionally NOT reconfigured first so that stdout
    # contains only the resolved YAML.
    if args.explain:
        sys.stdout.write(config_to_yaml(config))
        sys.stdout.flush()
        return 0

    log = setup_logger(
        level=config.logging.level,
        log_file=config.logging.file,
        format=config.logging.format,
    )
    log.info("=" * 60)
    log.info("Ticketmaster Bot starting")
    log.info("Vendor: %s", args.vendor)
    for i, evt in enumerate(config.events, start=1):
        log.info("Event %d: %s", i, evt.url)
    log.info(
        "Strategy: %s | Quantity: %d | Auto-purchase: %s",
        config.tickets.strategy,
        config.tickets.quantity,
        config.checkout.auto_purchase,
    )
    if args.parallel or args.max_parallel is not None or args.stagger_seconds is not None:
        log.info(
            "Parallel: %s | max_parallel: %s | stagger_seconds: %s",
            args.parallel,
            args.max_parallel,
            args.stagger_seconds,
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

    # --- Parallel multi-account path ----------------------------------
    # The orchestrator races one runner per configured account. We keep
    # this path opt-in (--parallel) so single-account users get the
    # historic single-context behaviour untouched.
    if args.parallel:
        from src.orchestrator.parallel import ParallelCoordinator

        if not config.accounts:
            log.error("--parallel requires at least one account in accounts.yaml")
            return 2
        if args.account_name and account is not None:
            parallel_accounts = [account]
        else:
            parallel_accounts = list(config.accounts)
        max_parallel = args.max_parallel if args.max_parallel is not None else 3
        stagger_seconds = args.stagger_seconds if args.stagger_seconds is not None else 5.0

        adapter_cls()  # validate the vendor adapter is constructible

        def _factory(acct, cfg, _stop):  # noqa: ANN001 - inner closure
            return adapter_cls().build_runner(cfg, account=acct)

        coordinator = ParallelCoordinator(
            accounts=parallel_accounts,
            config=config,
            max_parallel=max_parallel,
            stagger_seconds=stagger_seconds,
            runner_factory=_factory,
        )
        try:
            success = await coordinator.run()
        except KeyboardInterrupt:
            log.warning("Interrupted by user")
            return 130
        except Exception:  # noqa: BLE001
            log.exception("Parallel coordinator crashed")
            return 1
        return 0 if success else 1

    adapter = adapter_cls()
    runner = adapter.build_runner(config, account=account)
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
