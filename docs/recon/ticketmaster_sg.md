# ticketmaster.sg — F7.1 recon report

This document is the **committed source of truth** for everything we
learned by driving real headed Chromium against
`https://ticketmaster.sg/` and the PGL CS2 Major Singapore 2026 event
page on 2026-05-20. Subsequent SG-vendor features (F7.2 through F7.7)
must agree with the facts recorded here — if the live site contradicts
this doc, the doc is wrong and must be updated, not the code patched
around it.

All artefacts referenced below are committed alongside this report:

| Path | What it is |
| --- | --- |
| `docs/recon/ticketmaster_sg/01_home.{html,png}` | Real `https://ticketmaster.sg/` markup + screenshot. |
| `docs/recon/ticketmaster_sg/02_event_detail.{html,png}` | `/activity/detail/26sg_pglcs2major`. |
| `docs/recon/ticketmaster_sg/03_seat_selection.{html,png}` | `/ticket/area/26sg_pglcs2major/3239` (date 1). |
| `docs/recon/ticketmaster_sg/04_captcha.{html,png}` | `/ticket/check-captcha/26sg_pglcs2major/3239/1/21`. |
| `docs/recon/ticketmaster_sg/05_login.{html,png}` | `auth.ticketmaster.com/as/authorization.oauth2?client_id=…tmsg…`. |
| `docs/recon/ticketmaster_sg/06_listing.{html,png}` | `/activity/list/concerts`. |
| `docs/recon/ticketmaster_sg/ticketmaster_sg.har` | HAR for home, event-detail, ticket-area and listing pages (binary bodies stripped, oversize text bodies truncated; original 61 MB compressed to ~2 MB). |
| `docs/recon/ticketmaster_sg/_har_capture.py` | Standalone Playwright script that produced the HAR (re-runnable). |
| `tests/fixtures/vendors/ticketmaster_sg/*.html` | Distilled, self-contained fixtures for the four pages the SG adapter must drive. |
| `config/selectors/ticketmaster_sg.yaml` | Draft selector registry for the SG flow (F7.2 will finalise). |

## Method

We drove the live site with a real headed Chromium controlled by
`agent-browser` (session `8341b4540e30`), with a human (the user)
available to solve captchas live. A second pass produced the HAR via
the standalone Playwright script `_har_capture.py`. No mocks, no
stubs, no fake DOMs — every byte in these files came off the wire.

The recon flow was:

1. `https://ticketmaster.sg/` → home
2. `https://ticketmaster.sg/activity/detail/26sg_pglcs2major` → event detail
3. Click "Find tickets" for **10 Dec 2026 (Thu.)** → ticket-area landing
4. Pick **quantity = 2** in `select#TicketForm_count`
5. Click **Best Available** (`button#autoMode`)
6. Got redirected to `/ticket/check-captcha/26sg_pglcs2major/3239/1/21`
7. Human solved the Yii image captcha + invisible reCAPTCHA + ticked terms,
   clicked Submit
8. Site redirected to
   `https://auth.ticketmaster.com/as/authorization.oauth2?client_id=1a554b2c04dc.web.ticketmaster.sg…`
   (the OAuth login)

Real Add-to-Cart with credentials was **deferred**: the F7.1 brief says
"attempt real Add to Cart (no payment)" but the SG flow gates cart
behind a full account login that the user did not provide credentials
for. We did, however, observe and document the exact next URL the flow
goes to after captcha — `auth.ticketmaster.com` — which is what F7.3
(SG login) will drive.

## Purchase flow — observed URL pattern

```
ticketmaster.sg/                                                            # home
ticketmaster.sg/activity/list/<category>                                    # event listing (e.g. concerts)
ticketmaster.sg/activity/detail/<gameCode>                                  # event detail (multi-date)
ticketmaster.sg/ticket/area/<gameCode>/<dateId>                             # date selected, pick section/qty
ticketmaster.sg/ticket/get-area-map/<gameCode>/<dateId>                     # XHR: SVG/PNG venue map + zone JSON
ticketmaster.sg/ticket/get-area-list/<gameCode>/<dateId>                    # XHR: text fallback list of sections
ticketmaster.sg/ticket/ticket/<gameCode>/<dateId>/<areaNo>/<qty>            # XHR: ticket-type chooser fragment
ticketmaster.sg/ticket/check-captcha/<gameCode>/<dateId>/<areaNo>/<qty>     # Yii image captcha + terms + reCAPTCHA
auth.ticketmaster.com/as/authorization.oauth2?client_id=1a554b2c04dc.web.ticketmaster.sg
  &redirect_uri=https%3A%2F%2Fidentity.ticketmaster.sg%2Fexchange
  &visualPresets=tmsg                                                       # PingFederate OAuth login
identity.ticketmaster.sg/exchange?code=…&state=…                            # OAuth token exchange (sets SG cookies)
ticketmaster.sg/ticket/select-seat/<gameCode>/<dateId>/<areaNo>/<qty>       # interactive seat picker (Fancybox iframe)
ticketmaster.sg/ticket/checkout/…                                           # cart + payment
```

`<gameCode>` is the human-readable event slug Ticketmaster SG uses
internally (`26sg_pglcs2major`). `<dateId>` is the numeric primary key
of a specific show (3239 for the 10 Dec show, 3240 for 11 Dec, …) —
also exposed in the event-detail page as the `data-key` attribute on
each schedule row.

The pattern `/ticket/<gameCode>/<dateId>/<areaNo>/<qty>` is **shared**
between the AJAX `ticket/ticket` endpoint, the `check-captcha` page,
and the `select-seat` interactive picker. We can build any of these
URLs from values the bot already knows.

## Regional differences vs ticketmaster.com

| Concern | ticketmaster.com (US) | **ticketmaster.sg (SG)** |
| --- | --- | --- |
| Frontend stack | React + custom design system | Yii (PHP) + Bootstrap 5 + jQuery 3 + jQuery UI |
| Ticket selectors | `[data-bdd='quick-pick-row']` etc. | `tr[id^='pt-']` / `select[id^='TicketForm_ticketPrice_']`. **No `data-bdd` attributes anywhere on the SG site.** |
| Best-Available trigger | "Find Tickets" → quick-pick row click | `<button id="autoMode">` "Best Available" |
| Manual seat-pick | Inline SVG seat map | Fancybox iframe at `/ticket/select-seat/…` |
| Date selector on event page | Multi-event React tabs | `<select>` + bootstrap table (`table.auto-game-list`) |
| Quantity selector | `<select name="quantity">` on cart | `<select name="TicketForm[count]">` on area page (pre-cart) |
| Cart URL prefix | `cart.ticketmaster.com` | `ticketmaster.sg/ticket/checkout/...` (same origin) |
| Currency | USD (`$89.50`, sometimes `US$`) | **SGD** — but rendered with a bare `$` (no `S$` or `SGD` prefix). Sample: `$144.00` on the PGL event. |
| Date format | `Sat, Aug 5 · 8:00 PM` | `10 Dec 2026 (Thu.) 05:00 pm` (day-month-year, parenthesised day-of-week, lowercase am/pm) |
| Locale tag | `en-US` (default) | `en-SG`, accept-language `en-SG,en` |
| Payment methods | Visa/MC/Amex/Discover, Apple Pay, Affirm | Visa/MC/Amex/Apple Pay **plus PayNow** (SG QR payment) **and GrabPay** ([per Help Center](https://help.ticketmaster.sg/) — payment selector is rendered later in the checkout step, gated behind login; not observed first-hand in F7.1). |
| Login flow | `auth.ticketmaster.com` OAuth, then back to `.com` cookies | **Same** `auth.ticketmaster.com` host (PingFederate); only `client_id=1a554b2c04dc.web.ticketmaster.sg`, `redirect_uri=identity.ticketmaster.sg/exchange`, `visualPresets=tmsg` differ. After login cookies land on `.ticketmaster.sg`. |
| Auth cookie names | `SID`, `BID-tm`, `eps_sid`, `MUID`, `azk-track` | `BID`, `eps_sid`, `tmpt`, `TIXPUISID`, `_csrf`. (No `SID` / `MUID`. `BID` here is the SG variant — the live cookie value we captured was `BID=RESF4rBa14k-…`.) |
| Queue vendor | TM-internal "Smart Queue" / TM waiting-room | **Queue-It** (`/js/queue-it/queueclient.min.js`, `/js/queue-it/queueconfigloader.min.js`, customer id `ticketmasterasia`). Active queues redirect the user to `<event>.queue-it.net`. |

## Captcha vendor identification

Two separate captcha systems run side-by-side on the SG site. **Both
trigger on the same page** (`/ticket/check-captcha/...`):

1. **Yii image CAPTCHA** (server-side, in-DOM)
   - Image URL: `https://ticketmaster.sg/ticket/captcha?v=<token>`
   - Refresh URL: `https://ticketmaster.sg/ticket/captcha?refresh=1`
   - Input field: `<input id="TicketForm_verifyCode" name="TicketForm[verifyCode]">`
   - Image element: `<img id="TicketForm_verifyCode-image">`
   - Bound by inline JS: `jQuery('#TicketForm_verifyCode-image').yiiCaptcha({...})`
   - Characters: alphabetic only (verified by inline help text on the
     page: *"The verification code consists of only alphabetic letters
     and can be refreshed by clicking on its image."*).

2. **Google reCAPTCHA Enterprise** (invisible / score-based)
   - Site key: `6LcvL3UrAAAAAO_9u8Seiuf-I6F_tP_jSS-zndXV`
   - Anchor iframe src:
     `https://recaptcha.net/recaptcha/enterprise/anchor?ar=1&k=6LcvL3UrAAAAAO_9u8Seiuf-I6F_tP_jSS-zndXV&size=invisible&...`
   - Companion script: `https://recaptcha.net/recaptcha/enterprise.js?render=6LcvL3UrAAAAAO_9u8Seiuf-I6F_tP_jSS-zndXV`
   - Hidden response input: `<input name="g-recaptcha-response">` (token written by Google's JS just before submit).

The SG login page (`auth.ticketmaster.com/as/authorization.oauth2?…tmsg…`)
**also** mounts two invisible reCAPTCHA Enterprise iframes (site keys
`6LcvL3UrAAAAAO_9u8Seiuf-I6F_tP_jSS-zndXV` and
`6LdoaXQrAAAAADQviABd-eByJu6kPL8awKDyc1zb` — the second key is
account-takeover-specific, used at the password step).

Additionally — these are not user-facing challenges but they ride
alongside the captchas in the request envelope:

- **Ticketmaster EPS** (Encrypted Pixel Stream) — first-party
  `/epsf/<build>/asset/…` JS + `/epsf/gec/v3/SGHome` POST. Same
  anti-abuse subsystem the US site runs.
- **NuData / NuDetect** — hidden `nds-pmd` field on every form on
  `auth.ticketmaster.com` carries the device-fingerprint payload.
- **Imperva** — observed by the `tm-bl: 1` response header on
  `curl`-class clients without a browser fingerprint.

## Auth cookie inventory

After the user solved the captcha and the bot landed on the OAuth login
page, the persistent cookie store contained (subset, sensitive values
trimmed):

| Cookie | Domain | Purpose |
| --- | --- | --- |
| `BID` | `.ticketmaster.sg` | Browser identity (SG variant; replaces US `BID-tm`). |
| `eps_sid` | `.ticketmaster.sg` | TM Encrypted Pixel Stream session id. |
| `tmpt` | `.ticketmaster.sg` | TM "trace" token (also seen on US). |
| `TIXPUISID` | `ticketmaster.sg` | Yii / Tixcraft application session id. Bound to the `_csrf` token used in form POSTs. |
| `_csrf` | `ticketmaster.sg` | Yii double-submit CSRF cookie. |
| `_GRECAPTCHA` | `.recaptcha.net` | Google reCAPTCHA challenge state. |
| `OptanonConsent` | `.ticketmaster.sg` | OneTrust cookie-consent payload. |
| `OptanonAlertBoxClosed` | `.ticketmaster.sg` | OneTrust banner dismissal timestamp. |

After successful OAuth login on `auth.ticketmaster.com` we expect the
SSO/PingFederate session cookies (`SID`, `eps_sid`, …) to land on
`.ticketmaster.com`, with `.ticketmaster.sg`-scoped cookies set by the
`identity.ticketmaster.sg/exchange` redirect. We did not capture this
half of the flow in F7.1 (no live credentials available); F7.3 will
characterise it from a logged-in session.

For the SG `is_logged_in` check, the practical signal is the presence
of cookies in this set:

```
{"BID", "eps_sid", "tmpt", "TIXPUISID"}
```

…**combined with** the absence of "Sign In/Register" in the page DOM.
`BID` alone is not enough — it's set on first visit, anonymous or not.

## Selector inventory

The full draft mapping lives in
[`config/selectors/ticketmaster_sg.yaml`](../../config/selectors/ticketmaster_sg.yaml).
The high-impact selectors observed on the live DOM are:

| Logical name | Real CSS / pattern | Page |
| --- | --- | --- |
| `event_date_row` | `table.auto-game-list tbody tr[data-key]` | event detail |
| `event_find_tickets_link` | `tr[data-key] a[href*='/ticket/area/']` | event detail |
| `event_date_select` | `select#date-list` | event detail |
| `ticket_area_event_select` | `select#Game_gameId` | ticket area |
| `ticket_area_quantity_select` | `select#TicketForm_count` (name=`TicketForm[count]`) | ticket area |
| `ticket_area_best_available_button` | `button#autoMode` | ticket area / ticket-form |
| `ticket_area_map_container` | `div#mapContainer` | ticket area |
| `ticket_area_section_group` | `div#mapContainer svg g#sections > g[id]` | ticket area (interactive map) |
| `ticket_area_section_link` | `div.area_list a[id]` | ticket area (text fallback) |
| `ticket_type_row` | `table#ticketPriceList tbody tr[id^='pt-']` | ticket-type fragment |
| `ticket_type_quantity_select` | `select[id^='TicketForm_ticketPrice_']` | ticket-type fragment |
| `ticket_type_price_size_input` | `input[type='hidden'][id^='TicketForm_priceSize_']` | ticket-type fragment |
| `ticket_form_submit_button` | `button#autoMode` inside `form#form-ticket-ticket` | ticket-type fragment |
| `ticket_form_manual_seat_button` | `button#manualMode` (only when venue supports it) | ticket-type fragment |
| `csrf_input` | `input[type='hidden'][name='_csrf']` | every Yii form |
| `check_captcha_form` | `form#form-ticket-check-captcha` | check-captcha |
| `captcha_image` | `img#TicketForm_verifyCode-image` | check-captcha |
| `captcha_input` | `input#TicketForm_verifyCode` | check-captcha |
| `captcha_terms_checkbox` | `input#TicketForm_agree` | check-captcha |
| `captcha_submit_button` | `form#form-ticket-check-captcha button[type='submit']` | check-captcha |
| `recaptcha_response_input` | `input#g-recaptcha-response` | check-captcha + login |
| `captcha_iframe` | `iframe[title='reCAPTCHA']` | check-captcha + login |
| `login_email_input` | `input#email-input` (name=`email`) | login |
| `login_continue_button` | `button[name='sign-in']` | login |
| `login_nds_pmd_input` | `input[type='hidden'][name='nds-pmd']` | login |

## URL-pattern detection (for `navigator.detect_state`)

The SG flow has very stable URL prefixes; the state detector should use
these **first** and fall back to DOM markers only when the URL alone
is ambiguous:

| URL prefix / substring | `detect_state` should return |
| --- | --- |
| `queue-it.net` (any subdomain) anywhere in `page.url` or any frame URL | `"queue"` |
| `/activity/detail/` | `"event_detail"` (not on-sale gate yet) |
| `/ticket/area/` (without `/ticket/select-seat/`) | `"tickets"` |
| `/ticket/check-captcha/` | `"captcha"` (new SG-only state) |
| `auth.ticketmaster.com/as/authorization.oauth2` | `"login_required"` |
| `identity.ticketmaster.sg/exchange` | `"login_in_progress"` (transient) |
| `/ticket/checkout/` | `"checkout"` |
| `/ticket/select-seat/` (in an iframe src) | `"interactive_seatmap"` |

## Currency parsing notes

The site renders all prices with a bare `$` sign (no `S$` or `SGD`
prefix in user-facing text). The "Standard" ticket on the PGL CS2 Major
shows as `$144.00`. The SG vendor adapter must:

1. Parse the bare `$` as SGD, **not** USD. The dollar sign is a
   regional ambiguity — relying on context (host = `ticketmaster.sg`)
   is the only reliable disambiguation.
2. Treat `S$`, `SGD `, `SGD$`, and bare `$` as equivalent for budget
   matching (the broader SG site uses all four variants depending on
   the page; the recon target only used bare `$`).
3. Format prices for user-facing messages as `S$144.00` to avoid
   ambiguity in notifier outputs.

## Date / time parsing notes

The SG event-detail page uses this exact format for show times:

```
10 Dec 2026 (Thu.) 05:00 pm
```

i.e. day-month-year, three-letter month abbreviation, parenthesised
three-letter weekday with a trailing dot, lowercase 12-hour clock with
`am`/`pm`. The ticket-area page additionally embeds the venue inline:

```
10 Dec 2026 (Thu.) 05:00 pm <Singapore Indoor Stadium> PGL CS 2 Major Singapore 2026
```

The bot should treat the `<Venue>` segment as informational and key
off the `data-key` / option `value` (the numeric `<dateId>`) for state
tracking.

## Payment methods (regional)

Observed on the live SG help-center FAQ and Conditions of Entry pages
linked from `/ticket/check-captcha/...`:

- Visa, Mastercard, American Express — international cards accepted.
- Apple Pay.
- **PayNow** — Singapore QR-code transfer scheme. SG-only.
- **GrabPay** — Southeast-Asia e-wallet. SG-only.

These payment-method selectors are **not** observable until past
login + into the checkout flow, which F7.1 did not enter (no user
credentials). F7.4 (cart + checkout) will need to drive an authenticated
session to capture them; the recon doc will be amended at that point.

## Anti-bot / detection surface

In addition to the two captchas above, the SG site runs:

| System | Evidence | Implication |
| --- | --- | --- |
| **Ticketmaster EPS** ("Encrypted Pixel Stream") | First-party `/epsf/<build>/asset/eps.js`, `/epsf/gec/v3/SGHome` POSTs, `eps_sid` cookie. | Same JS-rendered fingerprint subsystem TM uses on `.com`. Stealth tooling that already evades `.com` EPS will work here. |
| **NuData / NuDetect** | Hidden `nds-pmd` input on `auth.ticketmaster.com` forms. | Existing `humanize.typing` + `humanize.mouse` paths cover this. |
| **Imperva** | `tm-bl: 1` response header on bare `curl` requests with no JS. | Requests must go through a JS-executing browser (already our model). |
| **Queue-It** | `queueclient.min.js` on every page, `data-queueit-c="ticketmasterasia"`. | When TM enables queueing for an event, the user is sent to `ticketmasterasia.queue-it.net` — the bot must follow that redirect and poll there. |
| **OneTrust** | `cdn.cookielaw.org`, `OptanonConsent` cookie. | Cookie-consent banner — not anti-bot but the bot should pre-accept it via a startup hook to avoid the modal blocking clicks. |

## What we could *not* verify in F7.1 (deferred to later features)

| Item | Why deferred | Picked up by |
| --- | --- | --- |
| Cart DOM after live Add-to-Cart | Cart requires authenticated session; F7.1 had no live credentials. We did observe the post-captcha redirect (to `auth.ticketmaster.com`). | F7.4 (cart + checkout) |
| Exact `is_logged_in` cookie set | Same reason. | F7.3 (auth) |
| PayNow / GrabPay selectors on checkout | Same reason. | F7.4 |
| Live queue DOM | The recon target was not on a queue at recon time. | F7.3 (queue handler) — needs to drive a Queue-It test event or a captured fixture. |

These deferrals are explicit; the validation contract assertion
`sg.recon-completed` covers what F7.1 *was* asked to verify (URL
patterns, page selectors, captcha vendor, auth-cookie shape, regional
differences). The remaining four assertions (`sg.auth-works`,
`sg.cart-works`, etc.) are owned by F7.3 / F7.4 / F7.6.

## How to re-run this recon

```bash
# 1. Headed Chromium driven by agent-browser, human in the loop for captcha:
agent-browser --session "<session-id>" --headed open https://ticketmaster.sg/
agent-browser --session "<session-id>" snapshot -i
agent-browser --session "<session-id>" screenshot --full docs/recon/ticketmaster_sg/NN_page.png
agent-browser --session "<session-id>" get html body > docs/recon/ticketmaster_sg/NN_page.html

# 2. HAR capture (no human needed; runs through public pages only):
.venv/bin/python docs/recon/ticketmaster_sg/_har_capture.py

# 3. Verify the distilled fixtures still match real DOM:
.venv/bin/python -m pytest tests/vendors/ticketmaster_sg/ -v
```
