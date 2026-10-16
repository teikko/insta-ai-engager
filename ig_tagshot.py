#!/usr/bin/env python3
"""Read-only diagnostic: open a hashtag page with the saved session and save a screenshot.
    .venv/bin/python ig_tagshot.py mirahaldane writingcommunity out.png
Prints the final URL and how many post/reel links the grid exposes. Performs no actions."""
import asyncio
import sys

from playwright.async_api import async_playwright

acct, tag, out = sys.argv[1], sys.argv[2], sys.argv[3]


async def main():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            f"accounts/{acct}/user_data", headless=False, viewport={"width": 1280, "height": 900})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(f"https://www.instagram.com/explore/tags/{tag}/",
                        wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(6000)
        print("url:", page.url)
        print("links /p/:", await page.locator("a[href*='/p/']").count(),
              " /reel/:", await page.locator("a[href*='/reel/']").count())
        await page.screenshot(path=out)
        await ctx.close()


asyncio.run(main())
