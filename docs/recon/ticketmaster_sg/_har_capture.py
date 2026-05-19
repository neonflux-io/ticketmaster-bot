"""HAR capture for ticketmaster.sg recon - F7.1.

Runs a headed Chromium in a fresh persistent context, captures HAR for:
 - home page
 - event detail page
 - ticket area page
 - check-captcha page (no submission, just navigation)

Outputs: docs/recon/ticketmaster_sg/ticketmaster_sg.har
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

RECON_DIR = Path(__file__).resolve().parent
HAR_PATH = RECON_DIR / "ticketmaster_sg.har"

URLS = [
    "https://ticketmaster.sg/",
    "https://ticketmaster.sg/activity/detail/26sg_pglcs2major",
    "https://ticketmaster.sg/ticket/area/26sg_pglcs2major/3239",
    "https://ticketmaster.sg/activity/list/concerts",
]


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            locale="en-SG",
            timezone_id="Asia/Singapore",
            viewport={"width": 1440, "height": 900},
            record_har_path=str(HAR_PATH),
            record_har_omit_content=False,
        )
        page = await ctx.new_page()
        for url in URLS:
            print(f"--> {url}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                # Best-effort network settle.
                try:
                    await page.wait_for_load_state("networkidle", timeout=8_000)
                except Exception:  # noqa: BLE001
                    pass
            except Exception as exc:  # noqa: BLE001
                print(f"    !! {exc}")
            await asyncio.sleep(2)
        await ctx.close()
        await browser.close()
    print(f"HAR written to {HAR_PATH} ({HAR_PATH.stat().st_size} bytes)")


if __name__ == "__main__":
    asyncio.run(main())
