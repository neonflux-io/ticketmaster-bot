# ticketmaster.sg — user guide

This guide is the operator-facing companion to the recon report at
[docs/recon/ticketmaster_sg.md](recon/ticketmaster_sg.md). It documents
how to run the bot against the Singapore site (`ticketmaster.sg`), what
the SG-specific configuration knobs do, how the captcha workflow
unfolds, and what to expect from the regional payment surface.

For the higher-level multi-vendor mechanics (`VendorAdapter` ABC,
plugin discovery, the adapter registry) see
[docs/vendors.md](vendors.md). For the per-step source layout of the
SG adapter itself, see `src/vendors/ticketmaster_sg/`.

## When to use the SG adapter

Use the SG adapter when the event URL host is `ticketmaster.sg`
(typically `https://ticketmaster.sg/activity/detail/<gameCode>`). The
bot will auto-detect the vendor by the URL's host — see
[Vendor selection](#vendor-selection) below — so most users never have
to pass `--vendor` explicitly. The two practical situations where you
*must* call out the vendor are:

- Running `--dry-run` / `--explain` without a `ticketmaster.sg` URL in
  the loaded config (auto-detection sees the placeholder
  `ticketmaster.com` URL in `config/config.yaml` and selects the US
  adapter; `--vendor ticketmaster_sg` overrides that).
- Driving the SG adapter against a non-standard host (e.g. a
  staging clone for testing).

The SG adapter cannot drive `ticketmaster.com` URLs — the two sites
share an OAuth front-end but everything else (DOM, queue vendor,
selectors, payment surface) diverges.

## Vendor selection

The bot uses two channels to pick a vendor adapter (`src/main.py`):

1. **Explicit:** `--vendor ticketmaster_sg` short-circuits any
   detection. Invalid names exit with code 2 and the name appears in
   stderr (`refactor.cli-vendor` assertion).
2. **Auto-detect:** When `--vendor` is omitted, `src/main.py` reads the
   host of `config.events[0].url` and matches against
   `_HOST_VENDOR_MAP`. `ticketmaster.sg` (and any subdomain like
   `www.ticketmaster.sg`) resolves to `ticketmaster_sg`; everything
   else — including `ticketmaster.com` — falls through to the default
   `ticketmaster` (US/CA) adapter.

```bash
# Auto-detect (recommended).
python run.py --events https://ticketmaster.sg/activity/detail/26sg_pglcs2major --explain

# Explicit override.
python run.py --vendor ticketmaster_sg --events https://ticketmaster.sg/activity/detail/26sg_pglcs2major --dry-run

# Dry-run against a placeholder URL — explicit vendor still needed.
python run.py --vendor ticketmaster_sg --dry-run
```

`--explain` prints the fully resolved YAML (defaults → profile →
events → env → `--set`) without launching a browser. Running it with
the SG URL in `events` is the cheapest way to confirm the adapter
auto-detects correctly before you start a real session.

## Quickstart

```bash
# 1. venv + Playwright (one-time, shared with the US flow).
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# 2. Configure credentials. ticketmaster.sg accepts the same
#    TM_EMAIL / TM_PASSWORD as ticketmaster.com — the OAuth front-end
#    is shared (see recon).
cp config/accounts.yaml.example config/accounts.yaml
$EDITOR config/accounts.yaml          # or set TM_EMAIL / TM_PASSWORD in .env

# 3. Point the bot at the SG event URL via --events (or edit
#    config/config.yaml). Vendor is auto-detected.
python run.py \
  --events https://ticketmaster.sg/activity/detail/26sg_pglcs2major \
  --explain                            # confirm vendor: ticketmaster_sg

# 4. First real run — keep the browser visible so you can solve the
#    Yii image captcha when it appears.
python run.py \
  --no-headless --no-auto-purchase \
  --events https://ticketmaster.sg/activity/detail/26sg_pglcs2major
```

The SG flow is gated by a server-side image captcha (see
[Captcha workflow](#captcha-workflow)) — running headless without a
solver is unlikely to succeed. The bot pauses automatically for up to
five minutes whenever a captcha is detected so you can solve it in the
visible browser.

## SG configuration knobs

The SG adapter reuses the same `BotConfig` shape as the US adapter; no
new top-level config keys are needed. The fields below are the ones
that behave differently or that you'll most often want to tune for an
SG event.

### `events`

```yaml
events:
  - url: "https://ticketmaster.sg/activity/detail/26sg_pglcs2major"
    on_sale_time: "10 Dec 2026 05:00 pm"     # SG-format string OK
    refresh_interval_seconds: 2
    strict_host: false                       # see "Host allowlist"
```

The SG adapter accepts on-sale strings in either form:

- **ISO 8601 with offset:** `"2026-12-10T17:00:00+08:00"`.
- **SG event-detail format:** `"10 Dec 2026"`, `"10 Dec 2026 05:00 pm"`,
  or with the parenthesised weekday `"10 Dec 2026 (Thu.) 05:00 pm"`.
  Parsed via `src.vendors.ticketmaster_sg.navigator.parse_sg_date` and
  always localised to Asia/Singapore (UTC+08:00, no DST).

### `tickets`

```yaml
tickets:
  quantity: 2
  strategy: cheapest                    # any registered strategy
  max_price: 300                        # SGD when running against ticketmaster.sg
```

Prices are SGD on the SG site even though the DOM renders them with a
bare `$` glyph. The SG cart-match verifier
(`src.vendors.ticketmaster_sg.checkout.verify_cart_matches`) parses
prices through `parse_sgd_price` which understands all four shapes the
SG site emits (`$144.00`, `S$144.00`, `SGD 144.00`, `SGD$1,234.56`)
and falls back to the shared US extractor for compatibility.

### `checkout`

```yaml
checkout:
  auto_purchase: false
  price_tolerance: 0.05
  payment:
    card_last_four: "4242"              # selects "ending in 4242" or "**** 4242"
  delivery:
    preferred:
      - "mobile entry"
      - "mobile ticket"
      - "e-ticket"
      - "venue collection"
    allow_any: false
```

`checkout.delivery.preferred` controls which SG delivery option the
adapter picks. The matcher is case-insensitive substring over the
visible label text. The SG-default ordering (see
`SG_DEFAULT_DELIVERY_PREFERENCE` in
`src/vendors/ticketmaster_sg/checkout.py`) is `mobile entry` →
`mobile ticket` → `e-ticket` → `eticket` → `venue collection` because
that ordering minimises post-purchase touchpoints.

`checkout.delivery.allow_any: true` falls back to the first delivery
radio when no preferred keyword matches. Leave it `false` unless you
have positively confirmed that "any delivery option" cannot pick an
unexpectedly expensive mail-delivery method on the SG site.

`checkout.payment.card_last_four` selects a saved card whose label
contains the four-digit suffix. SG renders these labels as either
`Visa ending in 4242` or `Mastercard **** 4242`; the YAML selector
template at `checkout_saved_card_label_template` matches both. The
field is validated as four ASCII digits; anything else is silently
ignored.

### `browser`

```yaml
browser:
  headless: false
  user_data_dir: "sessions/sg-default"   # per-account persistence
  locale: "en-SG"                        # SG site advertises Accept-Language en-SG,en
  timezone: "Asia/Singapore"
  profile: desktop                       # desktop | mobile (per humanize.profile)
```

The SG site renders show times in SGT — setting `browser.timezone:
"Asia/Singapore"` makes the bot localise any naive on-sale strings
correctly. Keep `browser.user_data_dir` SG-specific so SG and US
session cookies do not stomp on each other.

### `timing`

```yaml
timing:
  page_timeout_seconds: 30
  queue_check_interval_seconds: 5
  action_delay_seconds: [0.5, 2.0]
  humanize:
    enabled: true
    mouse:
      enabled: true
    typing:
      enabled: true
      mean_ms: 110
      std_ms: 35
      min_ms: 35
  max_total_runtime_seconds: 3600
  hold_open_seconds: 600
```

`timing.humanize.typing.enabled: true` is recommended for the SG flow
because the OAuth password step is fronted by NuData / NuDetect device
fingerprinting (see recon report). The character-by-character
`human_type` from `src/humanize/typing.py` is the same path the US
adapter takes; enabling it cleanly satisfies the NuData JS that
captures keystroke cadence.

### Host allowlist

`event.strict_host: true` is enforced by
`src/utils/config_loader.py`. The default trusted host list already
includes `ticketmaster.sg`, so SG event URLs pass the strict check.
You only need `strict_host: false` if you're driving a non-standard
host (e.g. a staging clone). The check rejects URLs from the broader
internet so a typo in the event URL fails fast at config-load time
instead of in the browser.

## Captcha workflow

The SG site mounts **two** captcha systems side-by-side. The
adapter's `wait_for_human_if_captcha` helper pauses when either is
detected; you solve the visible one in the browser, the invisible
one Google decides about silently. See
[Captcha vendor identification](recon/ticketmaster_sg.md#captcha-vendor-identification)
in the recon report for the gory details.

### Where captchas fire

| Step | Captcha shape | Adapter behaviour |
| --- | --- | --- |
| `/ticket/check-captcha/<game>/<date>/<area>/<qty>` after picking quantity + Best Available | Yii image (alphabetic only) + invisible reCAPTCHA Enterprise + required terms checkbox. | `cart.add_to_cart` pauses for human via `auth.wait_for_human_if_captcha`. |
| OAuth password step on `auth.ticketmaster.com` (after typing the email) | Invisible reCAPTCHA Enterprise; high risk score sometimes throws an image challenge. | `auth.login` pauses before submitting the password. |
| Final review on `/ticket/checkout/...` (rare; only for high-traffic events) | Re-thrown reCAPTCHA Enterprise. | `checkout.run_checkout` calls `auth.wait_for_human_if_captcha` before clicking Place Order. |

### Practical workflow

1. **Run visible.** Set `browser.headless: false` (or pass
   `--no-headless`). The captcha image is rendered server-side by
   Yii and is not solvable headless without an OCR/solver service —
   we ship neither.
2. **Wait for the pause.** Console logs `SG captcha detected -
   waiting up to 300s for human to solve` (warning level). The bot
   then polls the DOM every 1.5s for the captcha to disappear and
   resumes automatically.
3. **Type the alphabetic code into `#TicketForm_verifyCode`**, tick
   the terms checkbox, then click Submit. Google's invisible
   reCAPTCHA Enterprise validates silently; when its risk score is
   acceptable the form posts cleanly. If Google escalates to a
   tile-picker challenge, solve that in-place too — the adapter
   keeps waiting until either the page transitions away from
   `/ticket/check-captcha/` or the 300-second timeout expires.
4. **Login is the next page.** The bot reads the SG-scoped cookie
   set (`eps_sid`, `tmpt`, `TIXPUISID`) once login completes; the
   persistent profile at `browser.user_data_dir` re-uses those
   cookies on the next run so subsequent runs typically skip the
   captcha-then-login dance entirely.

### Tuning the captcha timeout

`captcha_timeout_seconds` is exposed on `auth.login`,
`auth.wait_for_human_if_captcha`, and `cart.add_to_cart`. The
default is 300 s (5 minutes), which matches the lifetime of the
Google reCAPTCHA Enterprise token. If you need a longer pause for
attended runs, override it at the call-site — there's no
top-level YAML knob for this because the SG adapter inherits the
same lifecycle as the US adapter and the default is generous.

## Regional payment notes

Payment methods are picked on the SG `/ticket/checkout/...` page,
after login. Per the SG help-centre pages linked from the captcha
form, the regional payment surface is:

| Method | Notes |
| --- | --- |
| Visa / Mastercard / American Express | International cards accepted. Saved cards select via `checkout.payment.card_last_four`. |
| Apple Pay | Surfaced as a separate radio on Safari / iOS contexts. The adapter does not currently auto-tick Apple Pay; if you want Apple Pay add the matching label keyword to `checkout.delivery.preferred` and lift the radio via the saved-card-template label match. |
| **PayNow** (SG-only) | Singapore QR-code transfer scheme. SG-only. |
| **GrabPay** (SG-only) | Southeast-Asia e-wallet. SG-only. |

The SG adapter selects payment via `select_saved_card(page,
last_four)` — i.e. only saved Visa/MC/Amex cards on file are
auto-selected. PayNow and GrabPay are **manual review** today: with
`checkout.auto_purchase: false` (the default), the bot stops at the
review step so you can click PayNow / GrabPay yourself and complete
payment.

Auto-purchase against PayNow / GrabPay is not implemented because
both schemes require a per-transaction QR scan on a mobile device,
which the bot cannot drive. The recon report (
[Payment methods](recon/ticketmaster_sg.md#payment-methods-regional))
documents this constraint; it is not a deferred feature, it is a
property of the regional payment surface.

## Persistent sessions

Per-account persistence works the same way as on the US adapter
(`browser.user_data_dir`). The SG cookies the adapter checks for in
`is_logged_in` are:

```
eps_sid, tmpt, TIXPUISID
```

`BID` is deliberately excluded: the SG site stamps it on every first
visit (anonymous or not) so its presence cannot distinguish
"logged in" from "loaded the home page once". `_csrf` is also
excluded because Yii double-submits it even on anonymous form GETs.

After a successful login the persistent profile under
`browser.user_data_dir` holds the full SG cookie jar; subsequent
runs reuse it without going through the OAuth bounce at all (until
the cookies expire server-side, typically 90 days).

## Anti-detection profile

The SG site runs the same EPS / NuData / Imperva stack as the US
site (see
[Anti-bot surface](recon/ticketmaster_sg.md#anti-bot--detection-surface)).
Recommendations:

- Keep `timing.humanize.enabled: true` so NuData's keystroke /
  cursor capture sees realistic events. The `bezier_move` in
  `src/humanize/mouse.py` plus `human_type` in
  `src/humanize/typing.py` cover the same telemetry as the US flow.
- Apply the stealth init script (`src/utils/stealth.py`) — it
  removes `navigator.webdriver` and the obvious automation tells
  that EPS and Imperva sniff for.
- Run from a residential or mobile IP. Imperva flags datacentre
  ASNs hard. The `--proxy` plumbing in `src/proxy/manager.py`
  ships unchanged for SG — same `proxy.urls` and `proxy.policy`
  config keys.
- Watch for the Queue-It interceptor on `<event>.queue-it.net`
  (customer id `ticketmasterasia`). The SG queue handler in
  `src/vendors/ticketmaster_sg/queue.py` polls the Queue-It hosted
  UI until released and logs queue-position text whenever the
  page exposes it.

## Selectors and fixtures

All SG selectors live in
[`config/selectors/ticketmaster_sg.yaml`](../config/selectors/ticketmaster_sg.yaml).
They are loaded by the SG-scoped registry in
`src/vendors/ticketmaster_sg/selectors.py`, not the shared US
registry, so SG and US selector logical names with the same string
(e.g. `login_email_input`, `page_body`) do not collide.

If the live SG site changes, update the YAML — no Python file under
`src/vendors/ticketmaster_sg/` should be edited to add a CSS string,
and the inline-selector grep gate (`rg "data-bdd=" src/ --type py`)
must continue to return zero lines.

Distilled HTML fixtures for the SG flow live under
`tests/fixtures/vendors/ticketmaster_sg/`. They are the source of
truth for the unit tests under `tests/vendors/ticketmaster_sg/`.
When the live site changes, refresh the fixture by running the
real-Chromium capture script at
`docs/recon/ticketmaster_sg/_har_capture.py` (no-auth pages) or
`docs/recon/ticketmaster_sg/_cart_capture.py` (cart / checkout —
requires an attended human-in-the-loop run).

## Troubleshooting

- **`error: Unknown vendor 'ticketmaster_sg'`.** The vendor registry
  did not pick up the SG adapter. Verify
  `.venv/bin/python -c "from src.registry import vendors; \
  print(sorted(vendors.all()))"` prints both `ticketmaster` and
  `ticketmaster_sg`. Importing `src.vendors` is what registers the
  adapter; if you've started the bot via an alternate path (e.g. a
  custom entry-point) confirm it still imports `src.vendors`.
- **Captcha timeout expired.** Increase the wait time by calling the
  affected step with a larger `captcha_timeout_seconds` (currently
  exposed on `auth.login`, `cart.add_to_cart`, and
  `checkout.run_checkout`).
- **`SG OAuth flow finished but required cookies are missing`.**
  Either the OAuth bounce did not complete (the captcha or password
  step was abandoned) or the SG cookie jar is being written to a
  different `user_data_dir`. Re-run with `browser.user_data_dir`
  unchanged and `browser.headless: false` to see exactly where the
  flow stalled.
- **`SG queue wait exceeded maximum runtime`.** The
  `wait_through_queue` deadline (default 1 hour) was reached. SG
  queues for high-demand events sometimes exceed an hour; raise
  `timing.max_total_runtime_seconds` and the wait will continue
  alongside it.
- **`Not a recognised SG date string`.** Either the on-sale string
  is an unsupported shape (use ISO 8601 with offset, or one of the
  forms documented under `events.on_sale_time` above) or
  `browser.timezone` is naive — set it to `Asia/Singapore`.
- **Price tolerance flagged on an SGD event.** The shared US
  price-tolerance check (`checkout.price_tolerance`) reads SGD
  prices via `parse_sgd_price`, so the tolerance is dimensionless —
  it does *not* need to be scaled for SGD vs USD. A failure here is
  a real cart-mismatch, not a currency parsing bug.
