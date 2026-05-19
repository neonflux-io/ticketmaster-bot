# Ticketmaster Bot

A modular, fully configurable Python bot that automates the Ticketmaster
ticket-buying flow using Playwright browser automation. It handles login,
auto-refresh before sale, the virtual waiting room (queue), ticket selection,
add-to-cart, and (optionally) full checkout.

> **DISCLAIMER**: This tool is for educational and personal use only. Using
> automated tools on Ticketmaster may violate their Terms of Service and the
> US BOTS Act. Use at your own risk. Do not use to scalp tickets.

## Features

- **Auto-refresh before on-sale time** - sleeps until shortly before sale, then polls
- **Virtual waiting room (queue) handling** - waits for queue release without losing the release token
- **Pluggable selection strategies** - cheapest / best available / target section (with row range + price-level filters)
- **Pre-purchase cart verification** - when `auto_purchase: true`, the bot re-reads the order summary and aborts if section/quantity/price drift
- **Configurable ticket quantity** (1-8) via YAML
- **Configurable delivery preference** (mobile / e-ticket first; never silently picks an expensive option)
- **Optional full checkout automation** (off by default for safety)
- **Multi-account support** (run with `--account-name`)
- **Session persistence** - logs in once per account, reuses cookies
- **Browser stealth** - patches `navigator.webdriver`, WebGL vendor, UA, plugins, languages
- **Desktop notifications** when tickets land in your cart
- **Retry with exponential backoff** on flaky operations
- **Captcha handling** at login, add-to-cart, and place-order - pauses for manual completion
- **Structured logging** to console + file (Rich markup auto-stripped from disk)

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Configure

```bash
# 1. Edit config/config.yaml - set the event URL and your preferences
# 2. Add credentials, either:

cp .env.example .env && $EDITOR .env
# or
cp config/accounts.yaml.example config/accounts.yaml && $EDITOR config/accounts.yaml
```

`${VAR}` placeholders are expanded from your environment in every account
field (`email`, `password`, `name`).

## Run

```bash
python run.py
```

Common flags:

```bash
python run.py --config config/config.yaml      # custom config
python run.py --account-name primary           # pick a specific account
python run.py --headless                       # force headless
python run.py --no-headless                    # force visible (overrides config)
python run.py --auto-purchase                  # force final-click on (DANGEROUS)
python run.py --no-auto-purchase               # force stop-at-cart
python run.py --dry-run                        # validate config and exit
```

## How It Works

```
INIT -> LOGIN -> NAVIGATE -> [WAIT_SALE|QUEUE] -> SELECT -> CART -> CHECKOUT -> DONE
```

1. **INIT** loads `config/config.yaml` and `config/accounts.yaml`.
2. **LOGIN** opens a persistent Chromium profile under `sessions/`. If you're
   already logged in (cookies present and no challenge page), it skips login.
3. **NAVIGATE** opens the event URL.
4. **WAIT_SALE** (optional) sleeps until your `on_sale_time` (naive datetimes
   are localized using `browser.timezone`), then refreshes until tickets appear.
5. **QUEUE** detects Ticketmaster's virtual waiting room and waits for release.
   After release the bot re-detects state on the *same page* - it never
   re-`goto`s the event URL because that would invalidate the queue token.
6. **SELECT** uses your configured strategy:
   - `cheapest` - lowest price quick-pick under `max_price`
   - `best_available` - clicks TM's "Best Available" button and re-validates
     the picked price against `max_price` before accepting
   - `section_target` - filters by section + row range + price-level id
7. **CART** sets the ticket quantity, ticks the *terms* checkbox (never
   marketing opt-ins), and clicks Add to Cart.
8. **CHECKOUT** selects a preferred delivery method (mobile / e-ticket) and
   saved card. If `auto_purchase: true` the bot first verifies the order
   summary matches (qty + section + price within tolerance) before clicking
   Place Order; otherwise it stops for manual review.

When the browser is visible and `auto_purchase: false`, the window is held
open for `timing.hold_open_seconds` (default 10 minutes) - or until you close
it - so you can finish checkout yourself.

## Project Layout

```
ticketmaster-bot/
├── config/
│   ├── config.yaml                # Main config
│   └── accounts.yaml.example      # Copy -> accounts.yaml
├── src/
│   ├── main.py                    # Entry point + CLI
│   ├── bot/
│   │   ├── core.py                # State machine / orchestrator
│   │   ├── auth.py                # Login + session + captcha-wait
│   │   ├── navigator.py           # Page navigation + on-sale polling
│   │   ├── queue.py               # Waiting-room handler
│   │   ├── cart.py                # Quantity + add-to-cart
│   │   └── checkout.py            # Delivery + payment + verify + place order
│   ├── strategies/
│   │   ├── base.py                # Strategy interface + parsers
│   │   ├── cheapest.py
│   │   ├── best_available.py
│   │   ├── section_target.py
│   │   └── factory.py             # Build from config
│   └── utils/
│       ├── config_loader.py       # YAML loading + validation
│       ├── logger.py              # Rich console + file logger
│       ├── retry.py               # Exponential backoff
│       ├── notifier.py            # Desktop notifications
│       └── stealth.py             # Browser-fingerprint hardening
├── tests/                         # pytest + pytest-asyncio
├── run.py                         # Top-level CLI
├── requirements.txt
├── pyproject.toml
└── README.md
```

## Development

```bash
pip install pytest pytest-asyncio ruff mypy
pytest                # unit tests
ruff check src tests  # lint
mypy src              # type check
```

## Tips

- **First run with a visible browser** (`headless: false` in config) so you
  can complete login and any captchas. The session is saved in `sessions/`
  and reused next time.
- **Test on a non-critical event** first to verify your selectors and timing.
- **Leave `auto_purchase: false`** until you have full confidence in the flow.
- **Stealth isn't magic.** It removes the most obvious automation tells but
  modern bot detection (Imperva, Datadome, Akamai) still has many other
  signals. Run from a residential IP, use a real account with purchase history,
  and don't fan out parallel sessions from the same IP.

## Troubleshooting

- **Login fails / treated as logged out repeatedly** - the bot detects
  challenge pages by title and iframe URL. Run with `headless: false`, log in
  once manually, then the persistent session in `sessions/default-*/` will
  reuse cookies on subsequent runs.
- **Bot can't find tickets** - selectors may have changed. Set
  `logging.level: DEBUG` and inspect `logs/bot.log`. Update selectors in
  `src/bot/navigator.py` / `src/strategies/base.py`.
- **Captcha during cart/checkout** - the bot now pauses for up to 5 minutes
  while you solve it in the visible browser, then continues.
- **`event.on_sale_time` rejected as naive** - either include a timezone
  offset (`2026-06-01T10:00:00-05:00`) or set `browser.timezone` so the
  loader can localize it.

## License

For personal/educational use. No warranty of any kind.
