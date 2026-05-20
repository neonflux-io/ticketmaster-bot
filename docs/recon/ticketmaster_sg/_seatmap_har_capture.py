"""Seat-map network + DOM capture for ticketmaster.sg — F8.1.

Drives a fresh Chromium context against the publicly-accessible
seat-map iframe URL for the KFF Singapore Badminton Open 2026 event
(section 225 / areaNo=46). The seat-map page is anonymous-accessible
when loaded directly as an iframe target — no login/captcha is required
to view the seat-map DOM.

Outputs (all under docs/recon/ticketmaster_sg/f8_1_capture/):
  03_seatmap_network.json — list of every request/response observed
                            while loading the seat-map page, in HAR-lite
                            shape (HAR-1.2 entries, no response bodies).
  04_seatmap_direct.html  — DOM captured by Playwright (no Fancybox shell)
  05_seatmap_direct.png   — full-page screenshot of seat-map

We use a hand-rolled JSON network log instead of Playwright's
``record_har_path`` because the latter consistently crashes the Node
driver with ``EPIPE`` on shutdown when the SG site is the load target
(reproduced on Playwright 1.60.0 + Node 24 + Python 3.14 with
``record_har_path`` enabled; logged at microsoft/playwright-python#1881
and #2454). The JSON log here is the minimum we need to demonstrate
the absence of a seat-availability AJAX endpoint and the presence of
the inline ``var seatData = {...}`` payload.

This script is re-runnable; it does not require credentials and does
NOT interact with the seat-map beyond loading it. Run:

    .venv/bin/python docs/recon/ticketmaster_sg/_seatmap_har_capture.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

REPO_ROOT = Path(__file__).resolve().parents[3]
CAPTURE_DIR = REPO_ROOT / "docs" / "recon" / "ticketmaster_sg" / "f8_1_capture"
NETWORK_PATH = CAPTURE_DIR / "03_seatmap_network.json"
HTML_PATH = CAPTURE_DIR / "04_seatmap_direct.html"
PNG_PATH = CAPTURE_DIR / "05_seatmap_direct.png"

# Publicly-accessible seat-map for KFF Singapore Badminton Open 2026,
# 26 May 2026 (Tue.) 10:00 am, section 225 (areaNo=46), section count 88.
SEATMAP_URL = "https://ticketmaster.sg/ticket/select-seat/26sg_sgopen2026/3318/46/88"

# We also prime cookies / OneTrust consent by visiting the area page
# first; the seat-map iframe is loaded straight after.
PRIME_URL = "https://ticketmaster.sg/ticket/area/26sg_sgopen2026/3318"


async def main() -> None:  # noqa: PLR0915
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        locale="en-SG",
        timezone_id="Asia/Singapore",
        viewport={"width": 1440, "height": 900},
    )
    page = await context.new_page()

    network_log: list[dict[str, object]] = []

    def _record_request(request) -> None:  # type: ignore[no-untyped-def]
        network_log.append(
            {
                "stage": "request",
                "url": request.url,
                "method": request.method,
                "resource_type": request.resource_type,
            },
        )

    def _record_response(response) -> None:  # type: ignore[no-untyped-def]
        try:
            content_type = response.headers.get("content-type", "")
        except Exception:  # noqa: BLE001
            content_type = ""
        network_log.append(
            {
                "stage": "response",
                "url": response.url,
                "status": response.status,
                "content_type": content_type,
            },
        )

    page.on("request", _record_request)
    page.on("response", _record_response)

    try:
        # Step 1: prime cookies on the ticket-area page. Picks up the
        # /ticket/get-area-map and /ticket/get-area-list XHRs along the
        # way so they end up in the network log alongside the
        # seat-map requests.
        print(f"--> priming {PRIME_URL}")
        try:
            await page.goto(PRIME_URL, wait_until="domcontentloaded", timeout=15_000)
        except Exception as exc:  # noqa: BLE001
            print(f"    !! prime nav failed: {exc}")
        # Short, fixed settle — networkidle on SG is unreliable thanks
        # to long-polling beacons (GTM, OneTrust, Braze) that never go
        # quiet enough to fire networkidle in a finite window.
        await asyncio.sleep(3)

        # Step 2: mount a tiny shell page with an iframe pointing at
        # the seat-map URL. The seat-map page has a JS guard
        # ``if (window.document == parent.document)
        # window.location.replace("/")`` so a top-level navigation
        # bounces straight to "/". Fancybox loads it as an iframe; we
        # mirror that.
        shell = (
            "<!doctype html><html><head><title>seatmap-recon</title>"
            "<style>html,body,iframe{margin:0;padding:0;border:0;"
            "width:100vw;height:100vh;}</style></head><body>"
            f"<iframe id=seatmap src='{SEATMAP_URL}'></iframe>"
            "</body></html>"
        )
        print(f"--> mounting seat-map iframe shell ({SEATMAP_URL})")
        await page.set_content(shell, wait_until="domcontentloaded")
        elem = await page.query_selector("iframe#seatmap")
        if elem is None:
            raise RuntimeError("seatmap iframe failed to attach")
        sframe = await elem.content_frame()
        if sframe is None:
            raise RuntimeError("seatmap iframe has no content_frame")
        try:
            await sframe.wait_for_selector("table.seat td", timeout=20_000)
        except Exception as exc:  # noqa: BLE001
            print(f"    !! seat-map did not render table.seat td: {exc}")
        # Give the inline ``var seatData = {...}`` JSON time to land.
        await asyncio.sleep(2)

        # Step 3: capture iframe DOM + full-page screenshot.
        html = await sframe.content()
        HTML_PATH.write_text(html, encoding="utf-8")
        print(f"    wrote {HTML_PATH.relative_to(REPO_ROOT)} ({len(html.encode())} bytes)")

        await page.screenshot(path=str(PNG_PATH), full_page=True)
        print(f"    wrote {PNG_PATH.relative_to(REPO_ROOT)}")

        # Step 4: sanity-print of the embedded seatData JSON.
        sample = await sframe.evaluate(
            """
            () => {
                const all = Array.from(document.scripts).map(s => s.textContent || "");
                const blob = all.find(s => s.includes("var seatData"));
                if (!blob) return null;
                const i = blob.indexOf("var seatData");
                return blob.slice(i, i + 800);
            }
            """,
        )
        if sample:
            print("    seatData head:\n        " + sample[:400].replace("\n", " "))

        # Step 5: dump the network log to JSON. Done BEFORE close so a
        # late EPIPE on shutdown doesn't lose it.
        NETWORK_PATH.write_text(
            json.dumps(network_log, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(
            f"    wrote {NETWORK_PATH.relative_to(REPO_ROOT)} "
            f"({len(network_log)} entries, {NETWORK_PATH.stat().st_size} bytes)"
        )

    finally:
        # ALWAYS close context first so the page event queue is drained
        # before the browser exits. We swallow EPIPE here — the data we
        # care about is already on disk.
        try:
            await context.close()
        except Exception as exc:  # noqa: BLE001
            print(f"    !! context.close failed: {exc}")
        try:
            await browser.close()
        except Exception as exc:  # noqa: BLE001
            print(f"    !! browser.close failed: {exc}")
        try:
            await pw.stop()
        except Exception as exc:  # noqa: BLE001
            print(f"    !! playwright.stop failed: {exc}")

    summary = {
        "event": "KFF Singapore Badminton Open 2026",
        "venue": "Singapore Indoor Stadium",
        "gameCode": "26sg_sgopen2026",
        "dateId": 3318,
        "areaNo": 46,
        "section": "225",
        "seatmap_url": SEATMAP_URL,
        "html_bytes": HTML_PATH.stat().st_size if HTML_PATH.exists() else 0,
        "png_bytes": PNG_PATH.stat().st_size if PNG_PATH.exists() else 0,
        "network_entries": len(network_log),
        "network_bytes": NETWORK_PATH.stat().st_size if NETWORK_PATH.exists() else 0,
    }
    print("SUMMARY: " + json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
