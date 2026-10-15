#!/usr/bin/env python3
"""Read-only: print an account's followers / following / posts using its saved session.
    .venv/bin/python ig_counts.py mirahaldane
Reads og:description on the profile page; performs no actions."""
import asyncio, re, sys
from playwright.async_api import async_playwright
acct = sys.argv[1] if len(sys.argv) > 1 else "mirahaldane"
async def main():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(f"accounts/{acct}/user_data", headless=False, viewport={"width": 1200, "height": 900})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(f"https://www.instagram.com/{acct}/", wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(5000)
        m = re.search(r'content="([^"]*Followers[^"]*)"', await page.content())
        print("META:", m.group(1).split(" - ")[0] if m else None)
        await ctx.close()
asyncio.run(main())
