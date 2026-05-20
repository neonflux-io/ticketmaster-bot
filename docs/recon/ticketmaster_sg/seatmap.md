# ticketmaster.sg seat-map — F8.1 recon report

This is the recon report for the interactive seat-map step on
ticketmaster.sg. The bot did not previously drive this step because the
F7.* SG-vendor work targeted the GA / Best-Available flow, which never
opens the seat-map iframe. F8.x adds support for venues with reserved
seating that present a "Pick Your Own Seat" picker after the user has
chosen a section.

All artefacts referenced below are committed alongside this report:

| Path | What it is |
| --- | --- |
| `docs/recon/ticketmaster_sg/f8_1_capture/01_seatmap_iframe.html` | Raw iframe DOM captured during the headed `agent-browser` exploration session. |
| `docs/recon/ticketmaster_sg/f8_1_capture/02_seatmap_screenshot.png` | Above-the-fold screenshot of the headed Fancybox seat-map. |
| `docs/recon/ticketmaster_sg/f8_1_capture/02_seatmap_full.png` | Full-page screenshot of the parent area page with the Fancybox open. |
| `docs/recon/ticketmaster_sg/f8_1_capture/03_seatmap_network.json` | HAR-lite network log (175 entries) from the unattended Playwright re-capture. |
| `docs/recon/ticketmaster_sg/f8_1_capture/04_seatmap_direct.html` | DOM captured by the unattended Playwright script (matches the headed capture; CSRF token scrubbed). |
| `docs/recon/ticketmaster_sg/f8_1_capture/05_seatmap_direct.png` | Full-page screenshot from the unattended capture. |
| `tests/fixtures/vendors/ticketmaster_sg/seatmap.html` | Test-suite fixture (committed real DOM with a banner header + scrubbed CSRF). |
| `docs/recon/ticketmaster_sg/_seatmap_har_capture.py` | Re-runnable Playwright script that produces the network log + the direct-capture HTML/PNG. |

## tl;dr for F8.2/F8.3 implementers

- **Captcha / login were NOT required to view the seat-map DOM.** The
  capture script ran completely anonymously (no `.env`, no cookies)
  and produced 173 KB of valid HTML with 116 available seats. This is
  the F8.1a path — no F8.1b human-attended split is needed.
- **Seat-map structure: `<table class="seat">`**, NOT SVG `<rect>`,
  NOT `<canvas>`. Each seat is a `<td>` cell.
- **Cell classes** are the strategy's primary signal:
  `empty` (available), `sold` (taken), `noseat` (aisle / structural
  gap), `checked` (user-selected). Pre-tagged-as-yours cells use
  `tagseat`.
- **Per-cell data**: `data-coordinate="<row_idx>_<col_idx>"`,
  `data-seatno="<visible-seat-number>"`,
  `data-seatrow="<visible-row-number>"`. Coordinates are 1-based
  grid indices (NOT pixel coords); the human row/seat labels are the
  ones to match against `tickets.interactive_seatmap.row` /
  `tickets.interactive_seatmap.seat`.
- **The seat-map is rendered inside a Fancybox iframe**, so the F8.2/3
  Playwright path will need `page.frame_locator()` (or the equivalent
  `element_handle.content_frame()` chain). The iframe `src` is
  `/ticket/select-seat/<gameCode>/<dateId>/<areaNo>/<count>`.
- **Top-level navigation to that URL bounces to `/`** thanks to a
  client-side guard:
  `if (window.document == parent.document) window.location.replace("/")`.
  Tests must load the seat-map either via `file://` + iframe shell or
  via the parent area page → "Pick Your Own" click.
- **There is NO seat-availability XHR** — seat state is baked into the
  initial HTML response as `var seatData = {<coord>: {seat, status, tag}}`.
  Status `01` = available, `02` = noseat, the rest map to sold /
  tagged variants (see "Seat status enum" below).
- **There IS a submit XHR**: `POST /ticket/select-seat/<...same args>`
  with form data `{ seats: JSON.stringify([coord, ...]), _csrf: <token> }`.
  Success returns `{ error: null }` which Fancybox-closes and submits
  the parent page's `<form>` to advance to the captcha gate.

## Source event

| Field | Value |
| --- | --- |
| Event title | **KFF Singapore Badminton Open 2026** |
| Event landing | `https://ticketmaster.sg/activity/detail/26sg_sgopen2026` |
| `gameCode` | `26sg_sgopen2026` |
| Show date | 26 May 2026 (Tue.) 10:00 am |
| `dateId` (Yii primary key) | `3318` |
| Venue | Singapore Indoor Stadium |
| Total dates with seat-map flow | 6 (`3318` ‥ `3323`) |
| Ticket-area URL | `https://ticketmaster.sg/ticket/area/26sg_sgopen2026/3318` |
| Section captured | **225** (`STANDARD - RESTRICTED VIEW`) |
| `areaNo` | `46` |
| Section seat-count cap | `88` |
| Seat-map iframe URL | `https://ticketmaster.sg/ticket/select-seat/26sg_sgopen2026/3318/46/88` |

We picked this event because:

1. It has reserved seating (the F7.* PGL CS:2 event was Standing /
   Best-Available only — `button#manualMode` is absent from that area
   page). Loading the badminton event's ticket-area page, clicking
   any of the 64 SVG `<g id="field_*">` section markers, then
   examining the per-section "Ticket Type" form reveals **both**
   `button#autoMode` ("Best Available") **and** `button#manualMode`
   ("Pick Your Own"). The presence of `button#manualMode` is the
   on-page indicator that the section supports interactive seat
   picking.
2. Section `225` is `AVAILABLE` (per the `window.zone` JSON loaded
   alongside the area-map SVG), so the seat-map iframe is fully
   populated rather than greyed out.
3. The Singapore Indoor Stadium uses one of the SG site's two seat-map
   templates (the other is the smaller Esplanade Concert Hall stamp).
   Recon notes against both venues match — the table-per-section
   structure is shared.

## How to find seat-map-capable events on ticketmaster.sg

There is no `:has-text("Pick Your Own")` filter on the SG listing
pages, but the marker is easy to find programmatically:

1. From an event detail page (`/activity/detail/<gameCode>`), click
   one of the `a.btn-primary` "Find tickets" links — they go to
   `/ticket/area/<gameCode>/<dateId>`.
2. The area page mounts an SVG inside `div#mapContainer` containing
   `g#sections > g[id^="field_"]` for each numbered section. The
   inline `window.zone` JSON keys those by `sectionCode` and includes
   `areaUrl`, `areaStatus`, `groupName`, and the per-section
   `price[]` array.
3. Click one section's `<g>` — the page injects a `#priceList` ticket-type
   form via AJAX (`POST /ticket/ticket/<gameCode>/<dateId>/<areaNo>/<sectionCount>`).
4. If the resulting form contains `<button id="manualMode">`, that
   section supports the seat-map flow. (If only `<button id="autoMode">`
   appears, the section is Best-Available-only.)

For F8.x integration tests we hard-code the badminton event above so
the recon is reproducible; F8.2 will not need to discover new events.

## Anonymous vs authenticated access

The original feature brief asked us to capture the seat-map using the
user's `TM_EMAIL` / `TM_PASSWORD`, in case the page required
authentication. **It does not.** The unattended Playwright capture
script (`_seatmap_har_capture.py`) runs with no credentials, no
persistent profile, and a fresh cookie jar; it produces a 173 KB HTML
response with 116 available seats and the full inline `seatData` JSON.

The OAuth gate / captcha gate is enforced AFTER `Confirm Seats` is
clicked, not before the seat-map loads — the POST handler at
`/ticket/select-seat/<...>` is the first server-side step that
requires a logged-in `BID`+`eps_sid` session. Until then the iframe
is purely client-side rendering of the per-section seat grid.

**Implication for F8.1b**: not needed. There is no human-attended
split. The whole seat-map recon ran headless and unattended in 6
seconds end-to-end.

## Capture method

Two passes:

### Pass 1: headed `agent-browser` exploration

Drove a single headed Chromium session via `agent-browser` (session
`38d458809212`):

```bash
agent-browser --session "38d458809212" open "https://ticketmaster.sg/"
agent-browser --session "38d458809212" open "https://ticketmaster.sg/activity/detail/26sg_sgopen2026"
agent-browser --session "38d458809212" open "https://ticketmaster.sg/ticket/area/26sg_sgopen2026/3318"
# JS: clicked g#field_225 inside #mapContainer
# JS: set TicketForm_ticketPrice_057 to '1'
# JS: dismissed OneTrust banner
# JS: clicked #manualMode → Fancybox opens iframe at /ticket/select-seat/26sg_sgopen2026/3318/46/88
agent-browser --session "38d458809212" eval --stdin <<EVALEOF > 01_seatmap_iframe.html
(() => {
  const iframe = document.querySelector('iframe[src*="select-seat"]');
  return iframe.contentDocument.documentElement.outerHTML;
})()
EVALEOF
agent-browser --session "38d458809212" screenshot 02_seatmap_screenshot.png
agent-browser --session "38d458809212" screenshot --full 02_seatmap_full.png
agent-browser --session "38d458809212" close
```

This pass confirmed the user-visible UI (Seat Orientation arrows,
Icon Legend with Available / Unavailable / Seats Selected, Close /
Reset / Confirm Seats buttons, the seat grid itself) and gave us the
unscrubbed iframe DOM. The CSRF token in this capture has since been
scrubbed (`REDACTED_CSRF_FOR_FIXTURE`) since CSRF rotation makes the
captured token useless anyway.

### Pass 2: unattended Playwright capture for the test fixture + network log

```bash
.venv/bin/python docs/recon/ticketmaster_sg/_seatmap_har_capture.py
```

The script:

1. Launches headless Chromium with `en-SG` locale + Singapore TZ.
2. Hits the ticket-area page first to prime cookies + load
   `/epsf/...` (the SG site's Encrypted Pixel Stream JS) so the
   subsequent `/ticket/select-seat/` request has a realistic referer.
3. Sets a one-off shell HTML containing `<iframe id=seatmap src="
   <SEATMAP_URL>">` so the seat-map JS guard
   (`window.document == parent.document → window.location.replace("/")`)
   does not bounce us to `/`.
4. Waits for `table.seat td` to appear inside the iframe.
5. Dumps the iframe HTML to `04_seatmap_direct.html` and a full-page
   PNG to `05_seatmap_direct.png`.
6. Writes a HAR-lite JSON of every request/response observed by the
   `page.on("request" / "response", ...)` handlers to
   `03_seatmap_network.json`.

Why a hand-rolled network log rather than Playwright's
`record_har_path`: enabling `record_har_path` against the SG seat-map
target reproduces a long-standing Playwright Python issue
([playwright-python#1881](https://github.com/microsoft/playwright-python/issues/1881),
[#2454](https://github.com/microsoft/playwright-python/issues/2454))
where the Node driver crashes with `EPIPE` on `context.close()` before
the HAR file is finalised. The hand-rolled log fits in 30 KB,
captures the same URL list we care about, and survives the EPIPE
because it's flushed before `context.close()` runs.

## Seat-map DOM structure

Top-level layout inside the iframe:

```
<html>
 <head>
   <link href="/css/ui_select_seat.css?v=6">     <!-- seat icon CSS -->
   <link href="/css/iv_switch.css?v=1">
   <style>
     /* sprites for seat states */
     .empty   { background: url(/images/seat-open.png) center center no-repeat; background-size:16px; }
     .empty:hover { background: url(/images/seat-hover.png) center center no-repeat; ... }
     .sold, .tagseat { background: url(/images/seat-sold.png) center center no-repeat; ... }
     .checked { background: url(/images/seat-selected.png) center center no-repeat; ... }
   </style>
 </head>
 <body>
   <!-- Header / section label -->
   <div class="stage">↑ Seat Orientation ↑</div>
   <div class="area-name">225</div>

   <!-- Legend -->
   <div class="icon-info">
     <ul id="legend">
       <li>Icon Legend:</li>
       <li><img src="/images/seat-open.png">Available</li>
       <li><img src="/images/seat-sold.png">Unavailable</li>
       <li><img src="/images/seat-selected.png">Seats Selected</li>
     </ul>
   </div>

   <!-- Action buttons -->
   <button id="exitSeat">Close</button>
   <button id="resetSeat">Reset</button>
   <button id="submitSeat">Confirm Seats</button>

   <!-- The seat grid itself -->
   <table class="seat">
     <tr>
       <th>14</th>  <!-- row label cell on the left -->
       <td class="sold"   data-coordinate="1_1"  data-seatno="3" data-seatrow="14"><div>3</div></td>
       <td class="noseat" data-coordinate="1_2"  data-seatrow=""></td>
       ...
     </tr>
     ...
   </table>

   <!-- Hover popup -->
   <div id="seatPopup">...</div>

   <!-- Inline JS: seatData JSON + click/submit handlers (see below) -->
   <script>var seatData = { "1_1": {"seat":"Row14,Seat3","status":"01","tag":null}, ... };
           $(".seat").on("click", ".gridc > .empty", function() { ... });
   </script>
 </body>
</html>
```

Counts on the captured fixture (section 225, total 238 cells = 14 rows × 17 columns):

| Class | Count | Meaning |
| --- | --- | --- |
| `empty` | 116 | Available — user can click to select |
| `noseat` | 110 | Aisle / structural gap (not a seat) |
| `sold` | 12 | Already booked (or held by another buyer) |
| `tagseat` | 0 | (Pre-tagged-to-you seat, used by V-Fan / hold codes; not present in this capture) |
| `checked` | 0 | (User selection; absent on initial render) |

### Per-cell schema

```html
<td class="<state>" data-coordinate="<row_idx>_<col_idx>" data-seatno="<n>" data-seatrow="<n>">
  <div><n></div>
</td>
```

- `data-coordinate` is the **internal grid coordinate** (1-based row,
  1-based column). This is what gets POSTed when the user confirms
  their seats.
- `data-seatno` and `data-seatrow` are the **visible seat / row
  labels**. These are what F8.2/3 strategy should match against the
  config's `tickets.interactive_seatmap.row` / `.seat` values.
- The inner `<div>` text duplicates `data-seatno` for the visible
  number, which is what the user sees.
- Aisle cells (`class="noseat"`) carry `data-coordinate` but have
  empty `data-seatrow` and no `data-seatno`. They must be filtered
  out by class, not by attribute presence alone.

### Seat status enum (`seatData[coord].status`)

Observed values:

| status | What it means | Cell class |
| --- | --- | --- |
| `"01"` | Available | `empty` |
| `"02"` | Not a seat (aisle / gap) | `noseat` |
| (not observed in this fixture) | Sold / held / tagged-to-you | `sold` / `tagseat` |

The exact code for `sold` / `tagseat` is not in this capture (every
sold cell in section 225 had status `01` in the inline JSON, which is
plausible because `seatData` is a snapshot of the **layout** while
`class` carries the **state** — the inline JS reads both). F8.2 should
rely on the CSS classes (`sold` / `tagseat` / `empty` / `noseat` /
`checked`) as the source of truth, not on `seatData[coord].status`.

## Inline `seatData` payload

The seat layout + labels are emitted inline at the bottom of the
iframe HTML, NOT fetched via XHR. Sample (truncated to the first 8 cells):

```js
var seatData = {
  "1_1": {"seat": "Row14,Seat3", "status": "01", "tag": null},
  "1_2": {"seat": "",            "status": "02", "tag": null},
  "1_3": {"seat": "",            "status": "02", "tag": null},
  "1_4": {"seat": "",            "status": "02", "tag": null},
  "1_5": {"seat": "",            "status": "02", "tag": null},
  "1_6": {"seat": "",            "status": "02", "tag": null},
  "1_7": {"seat": "",            "status": "02", "tag": null},
  "1_8": {"seat": "",            "status": "02", "tag": null},
  ...
};
```

The mapping is `seatData[<data-coordinate>] = { seat, status, tag }`,
where `seat` is the human-readable `"Row<n>,Seat<m>"` label and
`tag` is non-null for tagged-to-user seats (V-Fan / hold codes).

## Submission flow

When the user clicks `#submitSeat` ("Confirm Seats"), the iframe runs
this inline jQuery (paraphrased from the captured HTML):

```js
var coordinateList = [];
$(".seat td.checked").each(function(_, el) {
    coordinateList.push($(el).attr("data-coordinate"));
});
$.ajax({
    type: "post",
    data: {
        "seats":  JSON.stringify(coordinateList),
        "_csrf":  "<rotating Yii token>"
    },
    dataType: "json",
    success: function(data) {
        $.fancybox.close();
        if (data.error) { /* show modal, reload */ }
        else {
            parent.$.fancybox.close();
            parent.$("form").submit();   // -> /ticket/check-captcha/...
        }
    },
    error:   function() { /* show "couldn't reach the server" modal, reload */ }
});
```

So the bot's seat-map step looks like this end-to-end:

1. `page.click("button#manualMode")` on the parent ticket-area page
   (after a section + ticket-type have been chosen).
2. Wait for the Fancybox iframe to attach: `iframe[src*="/ticket/select-seat/"]`.
3. Switch into the iframe (`page.frame_locator(...)` in the
   strategy).
4. Identify the wanted cell by `data-seatrow` / `data-seatno`:
   `frame.locator("table.seat td.empty[data-seatrow='17'][data-seatno='5']").click()`.
   After click, the cell class flips from `empty` to `checked` (the
   `.gridc > .empty` mouseover/click handlers add/remove `checked`).
5. Click `#submitSeat` inside the iframe.
6. The Fancybox closes, the parent `<form id="form-ticket-ticket">`
   submits, and the URL transitions to `/ticket/check-captcha/<...>` —
   which is the existing SG captcha state already wired up in
   `src/vendors/ticketmaster_sg/navigator.py`.

## Network observations

The `03_seatmap_network.json` log shows 175 entries across the prime
navigation + seat-map render. The SG-host requests of note (response
status in left column):

```
401  /ticket/area/26sg_sgopen2026/3318           # initial nav, server demands cookies
200  /epsf/asset/shared.js                       # EPS
200  /epsf/1f4d8a80/asset/eps.js
200  /epsf/1f4d8a80/asset/iamNotaRobot.js
200  /epsf/1f4d8a80/asset/abuse-component.js
200  /epsf/gec/v3/SGTicket                       # EPS GEC POST
200  /ticket/area/26sg_sgopen2026/3318           # retry succeeds
200  /js/queue-it/queueclient.min.js             # Queue-It client
200  /js/queue-it/queueconfigloader.min.js
200  /ticket/select-seat/26sg_sgopen2026/3318/46/88   # ← THE SEAT MAP
200  /css/ui_select_seat.css?v=6
200  /css/iv_switch.css?v=1
200  /images/seat-open.png
200  /images/seat-sold.png
200  /images/seat-selected.png
200  /assets/bba9a1aa/yii.js                      # Yii client framework
```

There is **no `/ticket/get-seat-availability` or similar XHR**. The
entire seat state is in the inline `seatData` JSON; the seat sprites
are static PNGs swapped via the CSS classes. The only XHR on the
seat-map iframe is the `POST /ticket/select-seat/<...>` submit
described above, which we did NOT exercise during recon to avoid
unintentionally reserving a seat.

## Implications for F8.2 / F8.3

These are the F8.2/F8.3-facing facts F8.1 establishes, in priority
order:

1. **Selectors to add to `config/selectors/ticketmaster_sg.yaml`**
   (suggested logical names; F8.2 will finalise):
   - `seatmap_iframe`: `iframe[src*='/ticket/select-seat/']`
   - `seatmap_grid`: `table.seat`
   - `seatmap_seat_available`: `table.seat td.empty[data-seatrow][data-seatno]`
   - `seatmap_seat_sold`: `table.seat td.sold`
   - `seatmap_seat_selected`: `table.seat td.checked`
   - `seatmap_seat_by_label_template`:
     `table.seat td.empty[data-seatrow='{row}'][data-seatno='{seat}']`
   - `seatmap_submit_button`: `button#submitSeat`
   - `seatmap_close_button`: `button#exitSeat`
   - `seatmap_reset_button`: `button#resetSeat`
   - `seatmap_section_label`: `div.area-name` (the `<div class="area-name">225</div>` element inside `#float`)
   - `seatmap_legend`: `ul#legend`

2. **Frame handling**: every selector above must be resolved through
   the iframe — F8.2 should add an SG-specific `with_seatmap_frame()`
   helper to `src/vendors/ticketmaster_sg/` that yields a
   `FrameLocator` rooted at the Fancybox iframe.

3. **State detection**: `src/vendors/ticketmaster_sg/navigator.py`
   already maps `/ticket/select-seat/` → `"interactive_seatmap"` via
   `_URL_RULES` (line 271). However, the **top-level URL stays on
   `/ticket/area/<...>` while the Fancybox iframe loads
   `/ticket/select-seat/<...>`** — `page.url` won't see the
   transition. F8.2 will need an iframe-presence check on top of the
   URL rule: `if any(f.url.find('/ticket/select-seat/') >= 0 for f in
   page.frames): return "interactive_seatmap"`. The frame-URL check
   is already partially scaffolded in `detect_state()` for queue
   detection (`for frame in page.frames`), so the addition is
   one-liner-shaped.

4. **No synthetic SVG needed**: the existing shared
   `InteractiveSeatmapStrategy` from F2.6 will need an SG variant
   because the SG DOM uses `<table class="seat">` cells, not the
   `<svg><rect data-section/data-row/data-seat>` shape the US strategy
   expects. F8.2 should either (a) parameterise the strategy on a
   "selector dialect" or (b) ship an
   `src/vendors/ticketmaster_sg/seatmap.py` that overrides `.pick()`.
   Either is fine; option (b) is closer to the existing per-vendor
   convention and is the F7.x baseline.

5. **No captcha solve was needed for recon**. The brief asked the
   worker to split into F8.1a / F8.1b if a human-attended capture was
   required; that split is **not** needed. F8.2/F8.3 can proceed
   directly. The captcha gate at `/ticket/check-captcha/<...>` only
   appears after `#submitSeat`, which the strategy will hit anyway as
   part of the existing SG state machine.

## What we did NOT verify in F8.1 (deferred)

| Item | Why deferred | Picked up by |
| --- | --- | --- |
| The `POST /ticket/select-seat/<...>` submit response payload | We deliberately did not click `#submitSeat` to avoid reserving a seat. | F8.2 will exercise this via the committed fixture (the inline JS handlers run client-side, so a `file://`-loaded fixture is enough). |
| `tagseat` cell appearance | No V-Fan / hold code was active for this account; no cell had class `tagseat`. | F8.2 fixture work — can be staged in a test-only HTML variant if `vfan_aware` integration ever needs it. |
| Other venues' seat-map templates (Esplanade, KKBOX Arena) | One venue was sufficient for F8.2's selector wiring. | Future recon if a new SG venue is observed to ship a different DOM. |
| Numbered-row-but-not-section layouts (GA-row hybrids) | Out of scope; the only candidates we found in F8.1 were either pure GA or fully reserved. | Operator can re-run the discovery flow at the top of this doc. |

## How to re-run this recon

```bash
# Headless, unattended (~6s, produces the test fixture):
.venv/bin/python docs/recon/ticketmaster_sg/_seatmap_har_capture.py

# Headed exploration (for poking at the UI live):
agent-browser --session "f8-recon" open https://ticketmaster.sg/activity/detail/26sg_sgopen2026
# ... click "Find tickets" for 3318, click any section, click "Pick Your Own" ...
agent-browser --session "f8-recon" close
```

## How F8.2 tests should load the fixture

The fixture is the live DOM unchanged, including the inline JS guard
that self-redirects when `window.document == parent.document`. A
straight `await page.goto(f"file://{fixture}")` will be redirected
away from the seat-map by that guard.

The two viable load patterns for the F8.2 pytest suite are:

1. **Read-as-text and `page.set_content` with the guard neutered**
   (simplest; this is what the F8.1 verification used):

   ```python
   raw = (FIXTURES / "seatmap.html").read_text(encoding="utf-8")
   # Neuter the if (window.document == parent.document) ... block.
   munged = raw.replace(
       "if (window.document == parent.document) {",
       "if (false) {",
   )
   await page.set_content(munged, wait_until="domcontentloaded")
   await page.wait_for_selector("table.seat td.empty")
   ```

2. **Mount the fixture inside an iframe shell** so the guard's
   `parent.document == window.document` check is false (mirrors the
   real Fancybox layout). Note `--allow-file-access-from-files` is
   required for the iframe to read the file:// child:

   ```python
   shell = (
       "<!doctype html><html><body>"
       f"<iframe id=seatmap src='file://{FIXTURES / 'seatmap.html'}'></iframe>"
       "</body></html>"
   )
   await page.set_content(shell)
   seat_frame = page.frame_locator("iframe#seatmap")
   await seat_frame.locator("table.seat td.empty").first.wait_for()
   ```

F8.2 will pick whichever fits the existing `chromium_context` /
`fixture_url` harness best. The F8.1 verification used pattern 1
and successfully asserted on:

| Assertion | Value seen on the committed fixture |
| --- | --- |
| `td_total` | 238 |
| `td.empty` | 116 |
| `td.sold` | 12 |
| `td.noseat` | 110 |
| `button#submitSeat` count | 1 |
| `button#resetSeat` count | 1 |
| `button#exitSeat` count | 1 |
| `div.area-name` text | `"225"` |
| `td.empty[data-seatrow='18'][data-seatno='5']` count | 1 (cell at coord `5_3`) |
| `td.empty[data-seatrow='17'][data-seatno='5']` count | 0 (row 17 in this fixture has seats 1, 2, 3, 4, 6 — verify against fixture rather than assuming a contiguous numbering when writing strategy tests) |
