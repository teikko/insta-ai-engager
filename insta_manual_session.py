import argparse
import asyncio
import os
from getpass import getpass
from playwright.async_api import async_playwright

def _load_credentials() -> tuple[str, str]:
    parser = argparse.ArgumentParser(
        description="Open a manual Instagram session for login troubleshooting."
    )
    parser.add_argument("--username", default=os.getenv("IG_USERNAME"))
    parser.add_argument("--password", default=os.getenv("IG_PASSWORD"))
    args = parser.parse_args()

    username = (args.username or "").strip()
    password = args.password or ""

    if not username:
        raise ValueError("Missing username. Use --username or set IG_USERNAME.")
    if not password:
        password = getpass("Instagram password: ")

    if not password:
        raise ValueError("Missing password. Use --password or set IG_PASSWORD.")

    return username, password


async def main():
    username, password = _load_credentials()

    async with async_playwright() as p:
        chrome_like_options = {
            "viewport": {"width": 1440, "height": 900},
            "device_scale_factor": 2,
            "is_mobile": False,
            "has_touch": False,
            "user_agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/129.0.0.0 Safari/537.36"
            ),
            "geolocation": {"latitude": 61.4991, "longitude": 23.7872},
            "permissions": ["geolocation"],
        }
        context = await p.chromium.launch_persistent_context(
            "user_data",
            headless=False,
            **chrome_like_options,
        )
        page = context.pages[0] if context.pages else await context.new_page()

        print("🌐 Opening Instagram login page…")
        await page.goto("https://www.instagram.com/accounts/login/", timeout=20000)
        await page.wait_for_timeout(1000)

        # Attempt to accept cookies if shown
        try:
            allow_button = page.locator("button:has-text('Allow all cookies')")
            await allow_button.wait_for(state="visible", timeout=5000)
            await allow_button.click(force=True)
            print("✅ Accepted cookies.")
        except Exception:
            print("ℹ️ No cookie prompt detected.")

        if "/accounts/login/" in page.url:
            print("🔑 Logging in…")
            username_field = page.locator("input[name='username'], input[name='email']")
            password_field = page.locator("input[name='password'], input[name='pass']")

            try:
                await username_field.wait_for(state="visible", timeout=5000)
                await password_field.wait_for(state="visible", timeout=5000)
            except Exception:
                print("⚠️ Could not locate login inputs.")
            else:
                await username_field.first.fill(username)
                await password_field.first.fill(password)

                login_button = page.locator("button[type='submit'], div[role='button']:has-text('Log in')")
                if await login_button.count() > 0:
                    await login_button.first.click()
                else:
                    await password_field.first.press("Enter")

                await page.wait_for_timeout(6000)
                print("✅ Login attempt complete.")
        else:
            print("✅ Session already authenticated.")

        print("🕒 Keeping browser open for manual testing. Press Ctrl+C to exit.")
        try:
            while True:
                await asyncio.sleep(60)
        finally:
            await context.close()


if __name__ == "__main__":
    asyncio.run(main())
