#!/usr/bin/env python3
"""Open the bot's persistent Instagram profile in a plain visible window for a MANUAL login.
No automation: the script only opens instagram.com and waits until you close the window,
so the session cookies land in the same user_data the bots use.

    .venv/bin/python open_login_window.py mirahaldane
"""
import asyncio, sys
from pathlib import Path
from playwright.async_api import async_playwright

async def main(account):
    ud = Path(__file__).parent / "accounts" / account / "user_data"
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(str(ud), headless=False,
                                                         ignore_default_args=["--enable-automation"],
                                                         viewport=None)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.instagram.com/accounts/login/", wait_until="domcontentloaded")
        print(f"Login window open for @{account}. Log in, then just close the window.", flush=True)
        closed = asyncio.Event()
        ctx.on("close", lambda *_: closed.set())
        await closed.wait()
        print("Window closed - session saved.", flush=True)

asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "mirahaldane"))
