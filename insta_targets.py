#!/usr/bin/env python3
"""Account-targeted Instagram helper — validate / follow / like / view stories.

Complements insta_bot.py (which is hashtag + AI-comment driven). This one takes a
LIST OF ACCOUNTS (your follow list) and, per account, can:
  - validate  : read-only — confirm the profile exists + follower count (safe)
  - engage    : follow (if not already), like N recent posts, view stories
It never comments and needs no OpenAI key.

Config (accounts/<account>/config.yaml):
    credentials: {username, password}
    targets: [wildswimmingfinland, visitkalajoki, ...]   # or accounts/<account>/targets.txt
    target_engagement:
      follow: true
      like_recent: 2
      view_stories: true
      max_targets_per_run: 15
      action_delay_ms: 4000        # slower = safer than the hashtag bot

Run:
    python insta_targets.py <account> validate     # read-only (recommended first)
    python insta_targets.py <account> engage       # follow/like/story

⚠️ Browser automation may violate Instagram's ToS. FOLLOWING is the most rate-limited
   action — keep max_targets_per_run low and action_delay_ms high. Run headful first.
"""
import argparse, asyncio, csv, random, re, sys
from datetime import datetime
from pathlib import Path

import yaml
from playwright.async_api import async_playwright

from insta_bot import perform_login, ensure_account_paths   # reuse working login + paths

ACCOUNTS_ROOT = Path("accounts")
# aria-labels / button text differ by UI language (English + Finnish)
FOLLOW_TXT = ("Follow", "Seuraa")
FOLLOWING_TXT = ("Following", "Seurataan", "Requested", "Pyydetty", "Message", "Viesti")
LIKE_LBL = ("Like", "Tykkää")
UNLIKE_LBL = ("Unlike", "Poista tykkäys")


def load_cfg(account: str) -> dict:
    path = ACCOUNTS_ROOT / account / "config.yaml"
    if not path.exists():
        raise SystemExit(f"Missing config: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cred = raw.get("credentials") or {}
    if not cred.get("username") or not cred.get("password"):
        raise SystemExit("credentials.username/password required in config.yaml")
    targets = raw.get("targets") or []
    if isinstance(targets, str):
        targets = [t.strip() for t in targets.replace(",", "\n").splitlines() if t.strip()]
    tfile = ACCOUNTS_ROOT / account / "targets.txt"
    if not targets and tfile.exists():
        targets = [l.strip().lstrip("@") for l in tfile.read_text().splitlines() if l.strip() and not l.startswith("#")]
    te = raw.get("target_engagement") or {}
    return {
        "username": cred["username"], "password": cred["password"],
        "targets": [t.lstrip("@") for t in targets],
        "follow": bool(te.get("follow", True)),
        "like_recent": int(te.get("like_recent", 2)),
        "view_stories": bool(te.get("view_stories", True)),
        "max_targets": int(te.get("max_targets_per_run", 15)),
        "delay": int(te.get("action_delay_ms", 4000)),
        "headless": bool(te.get("headless", False)),
    }


def _done_csv(account: str) -> Path:
    return ACCOUNTS_ROOT / account / "targets_done.csv"


def load_done(account: str) -> set:
    p = _done_csv(account)
    if not p.exists():
        return set()
    with p.open() as f:
        return {row[0] for row in csv.reader(f) if row}


def mark_done(account: str, handle: str, action: str):
    with _done_csv(account).open("a", newline="") as f:
        csv.writer(f).writerow([handle, action, datetime.now().isoformat(timespec="seconds")])


async def jitter(page, ms: int):
    await page.wait_for_timeout(ms + random.randint(0, ms // 2))


async def follower_count(page, handle: str):
    """Read-only: navigate to a profile and parse the follower count from og:description.
    Returns (exists: bool, followers: str|None)."""
    await page.goto(f"https://www.instagram.com/{handle}/", timeout=25000, wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)
    if "/accounts/login" in page.url:
        return None, "not-logged-in"
    # "Page Not Found" for missing accounts
    body = (await page.title()) or ""
    try:
        meta = await page.locator('meta[property="og:description"]').first.get_attribute("content", timeout=4000)
    except Exception:
        meta = None
    if meta:
        m = re.search(r"([\d.,KMkm ]+)\s+Followers", meta) or re.search(r"([\d.,KMkm ]+)\s+seuraaja", meta)
        if m:
            return True, m.group(1).strip()
    if "Page Not Found" in body or "Sivua ei löytynyt" in body:
        return False, None
    return True, "?"           # exists but count not parseable


async def _primary_button(page):
    for txt in FOLLOW_TXT + FOLLOWING_TXT:
        loc = page.locator(f"header button:has-text('{txt}'), main header button:has-text('{txt}')")
        if await loc.count() > 0:
            return loc.first, (await loc.first.inner_text()).strip()
    return None, None


async def do_follow(page) -> str:
    btn, label = await _primary_button(page)
    if not btn:
        return "no-button"
    if any(s.lower() in (label or "").lower() for s in FOLLOWING_TXT):
        return "already"
    if any(s.lower() == (label or "").lower() for s in FOLLOW_TXT):
        await btn.click()
        await page.wait_for_timeout(1500)
        return "followed"
    return f"skip({label})"


async def like_recent(page, handle: str, n: int) -> int:
    if n <= 0:
        return 0
    links = page.locator("main a[href*='/p/']")
    hrefs = []
    try:
        for i in range(min(await links.count(), n)):
            h = await links.nth(i).get_attribute("href")
            if h:
                hrefs.append(h)
    except Exception:
        return 0
    liked = 0
    for h in hrefs:
        try:
            await page.goto("https://www.instagram.com" + h, timeout=20000, wait_until="domcontentloaded")
            await page.wait_for_timeout(1200)
            btn = None
            for lbl in LIKE_LBL:
                loc = page.locator(f"section svg[aria-label='{lbl}']")
                if await loc.count() > 0:
                    btn = loc.first; break
            if btn:
                await btn.click()
                liked += 1
                await page.wait_for_timeout(900)
        except Exception as e:
            print(f"    · like fail: {str(e)[:50]}")
    await page.goto(f"https://www.instagram.com/{handle}/", timeout=20000, wait_until="domcontentloaded")
    return liked


async def view_stories(page, handle: str) -> bool:
    """Open the profile's story ring (if any) and step through a few segments."""
    try:
        ring = page.locator("header img, main header img").first
        await ring.click(timeout=4000)
        await page.wait_for_timeout(2500)
        if "/stories/" not in page.url:
            return False
        for _ in range(4):
            await page.keyboard.press("ArrowRight")
            await page.wait_for_timeout(1800)
        await page.keyboard.press("Escape")
        return True
    except Exception:
        return False


async def run(account: str, mode: str):
    cfg = load_cfg(account)
    if not cfg["targets"]:
        raise SystemExit("No targets configured (config.yaml `targets:` or accounts/<account>/targets.txt)")
    paths = ensure_account_paths(cfg["username"])
    done = load_done(account)
    todo = [t for t in cfg["targets"] if mode == "validate" or f"{t}" not in done][:cfg["max_targets"] if mode == "engage" else len(cfg["targets"])]
    print(f"account @{cfg['username']} · mode {mode} · {len(todo)} target(s)")
    if mode == "engage":
        print("⚠️ engage mode performs follows/likes/story-views. Ctrl-C to abort.")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(str(paths["user_data"]), headless=cfg["headless"])
        page = context.pages[0] if context.pages else await context.new_page()

        async def dismiss_cookie():
            try:
                b = page.locator("button:has-text('Allow all cookies'), button:has-text('Salli kaikki evästeet'), "
                                 "button:has-text('Only allow essential'), button:has-text('Decline optional')")
                if await b.count() > 0:
                    await b.first.click(force=True); await page.wait_for_timeout(800)
            except Exception:
                pass

        async def logged_in() -> bool:
            # /accounts/edit/ REQUIRES auth — it redirects to /accounts/login/ when logged out.
            # This is a real login check (unlike the public home page, which fooled the old code).
            try:
                await page.goto("https://www.instagram.com/accounts/edit/", timeout=25000, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                return "/accounts/login" not in page.url and "/accounts/edit" in page.url
            except Exception:
                return False

        async def do_login():
            await page.goto("https://www.instagram.com/accounts/login/", timeout=25000, wait_until="domcontentloaded")
            await dismiss_cookie()
            try:                                              # wait for the form (SPA renders late)
                pw = page.locator("input[type='password'], input[name='pass']")
                await pw.first.wait_for(state="visible", timeout=12000)
            except Exception:
                print("⚠️ Login form did not appear.")
                return
            # IG's actual DOM: fields are name=email / name=pass; button is a <div role=button aria-label="Log in">
            user = page.locator("input[name='email'], input[name='username'], "
                                "input[autocomplete='username'], input[type='text']").first
            pw = pw.first
            print("🔑 Filling login form…")
            await user.click()
            await user.press_sequentially(cfg["username"], delay=40)   # per-key so React registers it
            await page.wait_for_timeout(300)
            await pw.click()
            await pw.press_sequentially(cfg["password"], delay=40)
            await page.wait_for_timeout(400)
            btn = page.locator("div[role='button'][aria-label='Log in'], "
                               "div[role='button'][aria-label='Kirjaudu'], "
                               "button[type='submit']:has-text('Log in'), "
                               "button:has-text('Log in')")
            clicked = False
            if await btn.count() > 0:
                try:
                    await btn.first.click(timeout=4000)
                    clicked = True
                except Exception:
                    pass
            if not clicked:
                await pw.press("Enter")                                # fallback: submit via keyboard
            await page.wait_for_timeout(6000)

        # --- HARD LOGIN GATE: nothing touches targets until login is truly confirmed ---
        if await logged_in():
            print("✅ already logged in (saved session)")
        else:
            await do_login()
            ok = False
            for attempt in range(1, 21):
                if await logged_in():
                    ok = True
                    break
                print(f"⚠️ NOT logged in yet (2FA / checkpoint / wrong creds?). Finish login in the "
                      f"OPEN BROWSER via remote desktop. Re-checking in 15s… [{attempt}/20]")
                await page.wait_for_timeout(15000)
            if not ok:
                print("❌ Login not confirmed — aborting WITHOUT touching any targets.")
                await context.close()
                return
            print("✅ login confirmed")

        for i, handle in enumerate(todo, 1):
            try:
                exists, followers = await follower_count(page, handle)
            except Exception as e:
                if "closed" in str(e).lower():
                    print(f"⚠️ Browser/page closed at [{i}/{len(todo)}] — stopping cleanly. Re-run to finish.")
                    break
                print(f"[{i}/{len(todo)}] @{handle}: error {str(e)[:60]}")
                continue
            tag = "OK" if exists else ("MISSING" if exists is False else "?")
            print(f"[{i}/{len(todo)}] @{handle:24} {tag:8} followers={followers}")
            if mode == "validate" or exists is not True:
                await jitter(page, cfg["delay"] // 2)
                continue
            acts = []
            if cfg["follow"]:
                r = await do_follow(page); acts.append(f"follow:{r}")
                if r == "followed":
                    mark_done(account, handle, "follow")
            if cfg["like_recent"]:
                liked = await like_recent(page, handle, cfg["like_recent"]); acts.append(f"liked:{liked}")
            if cfg["view_stories"]:
                sv = await view_stories(page, handle); acts.append(f"story:{'y' if sv else 'n'}")
            print(f"       → {' '.join(acts)}")
            mark_done(account, handle, "engage")
            await jitter(page, cfg["delay"])
        await context.close()
    print("done.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("account")
    ap.add_argument("mode", nargs="?", default="validate", choices=["validate", "engage"])
    a = ap.parse_args()
    asyncio.run(run(a.account, a.mode))


if __name__ == "__main__":
    main()
