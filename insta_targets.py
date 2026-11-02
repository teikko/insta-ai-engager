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
    # Username is required. Password is OPTIONAL: once a session is saved in user_data/
    # the persistent context handles auth, so we don't keep the (leaked-prone) password in
    # the file. If no session exists and password is blank, the login gate pauses for a
    # manual login instead of failing here.
    if not cred.get("username"):
        raise SystemExit("credentials.username required in config.yaml")
    targets = raw.get("targets") or []
    if isinstance(targets, str):
        targets = [t.strip() for t in targets.replace(",", "\n").splitlines() if t.strip()]
    tfile = ACCOUNTS_ROOT / account / "targets.txt"
    if not targets and tfile.exists():
        targets = [l.strip().lstrip("@") for l in tfile.read_text().splitlines() if l.strip() and not l.startswith("#")]
    te = raw.get("target_engagement") or {}
    return {
        "username": cred["username"], "password": cred.get("password", ""),
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


UNFOLLOW_TXT = ("Unfollow", "Lopeta seuraaminen", "Lopeta seuranta")
UNFOLLOW_AFTER_DAYS = 5


def _unfollowed_csv(account: str) -> Path:
    return ACCOUNTS_ROOT / account / "unfollowed.csv"


def load_unfollowed(account: str) -> set:
    p = _unfollowed_csv(account)
    if not p.exists():
        return set()
    with p.open() as f:
        return {row[0] for row in csv.reader(f) if row}


def mark_unfollowed(account: str, handle: str, note: str):
    with _unfollowed_csv(account).open("a", newline="") as f:
        csv.writer(f).writerow([handle, note, datetime.now().isoformat(timespec="seconds")])


def unfollow_candidates(account: str, protect: set, days: int = UNFOLLOW_AFTER_DAYS) -> list:
    """Handles WE followed via the engage run (targets_done.csv 'follow' rows) at least `days` ago.
    Accounts followed for credibility at the start never appear in that CSV, so they are structurally
    protected; `never_unfollow` in config is an extra explicit guard."""
    p = _done_csv(account)
    if not p.exists():
        return []
    cutoff = datetime.now().timestamp() - days * 86400
    already = load_unfollowed(account)
    out = []
    with p.open() as f:
        for row in csv.reader(f):
            if len(row) >= 3 and row[1] == "follow":
                h = row[0].lstrip("@")
                if h in already or h in protect or h in out:
                    continue
                if datetime.fromisoformat(row[2]).timestamp() <= cutoff:
                    out.append(h)
    return out


async def own_followers(page, me: str):
    """Set of handles that follow @me, read from the followers dialog. None = could not verify."""
    await page.goto(f"https://www.instagram.com/{me}/", wait_until="domcontentloaded", timeout=40000)
    await page.wait_for_timeout(3500)
    try:
        await page.locator("header").get_by_text(re.compile(r"followers|seuraajaa", re.I)).first.click(timeout=15000)
        dlg = page.locator('div[role="dialog"]')
        await dlg.wait_for(timeout=15000)
    except Exception:
        return None
    seen = set()
    for _ in range(25):
        hrefs = await dlg.locator('a[role="link"]').evaluate_all("els => els.map(e => e.getAttribute('href'))")
        before = len(seen)
        for h in hrefs:
            if h and re.match(r"^/[^/]+/$", h):
                u = h.strip("/")
                if u and u != me:
                    seen.add(u)
        await dlg.evaluate("el => { const s=[...el.querySelectorAll('div')].find(d=>d.scrollHeight>d.clientHeight+50); if(s) s.scrollTop=s.scrollHeight; }")
        await page.wait_for_timeout(900)
        if len(seen) == before and _ > 2:
            break
    await page.keyboard.press("Escape")
    return seen


async def do_unfollow(page) -> str:
    header = page.locator("header").first
    btns = header.locator("button, div[role='button']")
    btn = None
    for txt in ("Following", "Seurataan"):
        loc = btns.filter(has_text=txt)
        if await loc.count() > 0:
            btn = loc.first
            break
    if btn is None:
        for txt in FOLLOW_TXT:        # button says Follow -> we are NOT following them, nothing to do
            loc = btns.filter(has_text=re.compile(rf"^\s*{txt}\s*$"))
            if await loc.count() > 0:
                return "not-following"
        return "no-button"
    await btn.click()
    await page.wait_for_timeout(1500)
    item = page.get_by_text(re.compile(r"^\s*(Unfollow|Lopeta seuraaminen|Lopeta seuranta)\s*$"))
    if await item.count() == 0:
        await page.keyboard.press("Escape")
        return "no-unfollow-option"
    await item.first.click()
    await page.wait_for_timeout(1800)
    conf = page.locator("[role='dialog'] button").filter(has_text=re.compile(r"^\s*(Unfollow|Lopeta seuraaminen)\s*$"))
    if await conf.count() > 0:
        await conf.first.click()
        await page.wait_for_timeout(1200)
    # verify: header button should now say Follow
    for txt in FOLLOW_TXT:
        if await btns.filter(has_text=re.compile(rf"^\s*{txt}\s*$")).count() > 0:
            return "unfollowed"
    return "unverified"


async def jitter(page, ms: int):
    await page.wait_for_timeout(ms + random.randint(0, ms // 2))


async def human_scroll(page, rounds: int = None):
    """Idle, human-ish scrolling on the current page — a few small wheel nudges
    with pauses, then scroll back up a bit. Makes engagement look less robotic."""
    rounds = rounds if rounds is not None else random.randint(2, 4)
    for _ in range(rounds):
        try:
            await page.mouse.wheel(0, random.randint(350, 900))
        except Exception:
            return
        await page.wait_for_timeout(random.randint(700, 2200))
    if random.random() < 0.6:                     # sometimes scroll back toward top
        try:
            await page.mouse.wheel(0, -random.randint(300, 700))
            await page.wait_for_timeout(random.randint(500, 1400))
        except Exception:
            pass


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
    # Match the CTA by ARIA role + EXACT accessible name, scoped to the profile header.
    # role=button covers both <button> and <div role=button>; exact=True avoids matching
    # bios that merely contain the word "Follow" (e.g. "Follow us for local tips").
    header = page.locator("header").first
    for txt in FOLLOW_TXT + FOLLOWING_TXT:
        loc = header.get_by_role("button", name=txt, exact=True)
        if await loc.count() > 0:
            return loc.first, txt
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
            # already liked? then the Unlike icon is present — skip (don't unlike)
            already = False
            for lbl in UNLIKE_LBL:
                if await page.locator(f"section svg[aria-label='{lbl}']").count() > 0:
                    already = True; break
            if already:
                print(f"    · like SKIP (already liked)  {h}")
                await page.wait_for_timeout(700)
                continue
            btn = None
            for lbl in LIKE_LBL:
                loc = page.locator(f"section svg[aria-label='{lbl}']")
                if await loc.count() > 0:
                    btn = loc.first; break
            if btn:
                await btn.click()
                liked += 1
                print(f"    · LIKED  {h}")
                await page.wait_for_timeout(900)
            else:
                print(f"    · like NO-BUTTON  {h}")
        except Exception as e:
            print(f"    · like FAIL ({str(e)[:40]})  {h}")
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
        segments = 0
        for _ in range(4):
            await page.keyboard.press("ArrowRight")
            await page.wait_for_timeout(1800)
            segments += 1
        await page.keyboard.press("Escape")
        print(f"    · stories viewed (~{segments} segments)")
        return True
    except Exception:
        return False


async def run(account: str, mode: str):
    cfg = load_cfg(account)
    if not cfg["targets"] and not mode.startswith("unfollow"):
        raise SystemExit("No targets configured (config.yaml `targets:` or accounts/<account>/targets.txt)")
    paths = ensure_account_paths(cfg["username"])
    done = load_done(account)
    if mode.startswith("unfollow"):
        raw = yaml.safe_load((ACCOUNTS_ROOT / account / "config.yaml").read_text(encoding="utf-8")) or {}
        protect = {h.lstrip("@") for h in (raw.get("never_unfollow") or [])}
        un_cands = unfollow_candidates(account, protect)
        un_max = int((raw.get("target_engagement") or {}).get("max_unfollows_per_run", 5))
        print(f"unfollow: {len(un_cands)} candidate(s) followed >= {UNFOLLOW_AFTER_DAYS} days ago via engage; cap {un_max}/run")
        todo = []
    else:
        un_cands = []
    if not mode.startswith("unfollow"):
        todo = [t for t in cfg["targets"] if mode == "validate" or f"{t}" not in done][:cfg["max_targets"] if mode == "engage" else len(cfg["targets"])]
    print(f"account @{cfg['username']} · mode {mode} · {len(todo)} target(s)")
    if mode == "engage":
        print("⚠️ engage mode performs follows/likes/story-views. Ctrl-C to abort.")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(str(paths["user_data"]), headless=cfg["headless"])
        page = context.pages[0] if context.pages else await context.new_page()

        async def dismiss_cookie():
            # The consent modal is a React PORTAL that renders LATE (last node in body),
            # so a single immediate call misses it — poll for up to ~16s. Buttons are real
            # <button> elems with exact text "Decline optional cookies" / "Allow all cookies".
            # Prefer declining optional (privacy) and fall back to allow-all so login isn't blocked.
            rx = re.compile(r"Decline optional cookies|Only allow essential|Kiellä valinnaiset|"
                            r"Allow all cookies|Salli kaikki evästeet|Hyväksy kaikki", re.I)
            for _ in range(4):                         # ~2.8s max — enough for a late portal,
                try:                                   # but no long stall once cookies are accepted
                    b = page.get_by_role("button", name=rx)
                    if await b.count() > 0:
                        await b.first.click(timeout=3000)
                        await page.wait_for_timeout(600)
                        print("🍪 cookie-dialogi kuitattu.")
                        return True
                except Exception:
                    pass
                await page.wait_for_timeout(700)
            return False                               # no banner (already accepted) — carry on

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
            await page.wait_for_timeout(2500)                          # brief settle; the human finishes captcha/2FA
            print("📤 Kirjautuminen lähetetty. Instagram näyttää nyt todennäköisesti captchan/2FA:n.")

        # --- MANUAL LOGIN MODE: just open the browser, let the human log in at their own pace ---
        if mode == "login":
            if await logged_in():
                print("✅ already logged in — session is saved, nothing to do.")
                await context.close()
                return
            await page.goto("https://www.instagram.com/accounts/login/", timeout=25000,
                            wait_until="domcontentloaded")
            await dismiss_cookie()
            print("👉 Kirjaudu KÄSIN avautuvassa selaimessa (täytä tunnus+salasana, klikkaa captcha/2FA).")
            print("   Odotan enintään 15 min ja tarkistan 10 s välein…")
            for attempt in range(1, 91):                        # 90 × 10s = 15 min
                await page.wait_for_timeout(10000)
                if await logged_in():
                    print(f"✅ Kirjautuminen onnistui! Sessio tallennettu (accounts/{account}/user_data).")
                    print("   Aja nyt:  insta_targets.py {} validate".format(account))
                    await context.close()
                    return
                if attempt % 3 == 0:
                    print(f"   …odotan yhä ({attempt*10}s), kirjaudu loppuun selaimessa.")
            print("❌ Ei kirjautunut 15 min sisällä — aja uudelleen kun ehdit.")
            await context.close()
            return

        # --- HARD LOGIN GATE: nothing touches targets until login is truly confirmed ---
        if await logged_in():
            print("✅ already logged in (saved session)")
        else:
            await do_login()
            # PAUSE for the human. We must NOT poll logged_in() here — it navigates to
            # /accounts/edit/ and would yank the page away from the captcha/2FA every time.
            # Instead we block on Enter (terminal is visible on the same remote desktop),
            # so the user finishes cookies + captcha + 2FA at their own pace, uninterrupted.
            print("\n" + "=" * 64)
            print("👉 VIIMEISTELE KIRJAUTUMINEN avoimessa selaimessa:")
            print("   1) kuittaa mahdollinen eväste-dialogi")
            print("   2) ratkaise captcha jos se pyytää")
            print("   3) syötä 2FA-koodi jos se pyytää")
            print("   Ota rauhassa aikaa — sivu EI liiku ennen kuin painat Enteriä.")
            print("=" * 64)
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, input, "   ↵  Paina Enter TÄSSÄ kun olet kirjautunut sisään: ")
            except Exception:
                pass
            if await logged_in():
                print("✅ login confirmed — sessio tallennettu.")
            else:
                # one grace retry in case IG was still settling right after Enter
                await page.wait_for_timeout(4000)
                if await logged_in():
                    print("✅ login confirmed — sessio tallennettu.")
                else:
                    print("❌ Login not confirmed — aborting WITHOUT touching any targets.")
                    print("   (Jos olit varmasti sisällä, aja komento uudelleen — sessio saattaa jo olla tallessa.)")
                    await context.close()
                    return

        if mode.startswith("unfollow"):
            dry = mode == "unfollow-dry"
            if not un_cands:
                print("nothing to unfollow yet.")
                await context.close()
                return
            fol = await own_followers(page, cfg["username"])
            if fol is None:
                print("❌ could not read the followers list - aborting WITHOUT unfollowing anyone.")
                await context.close()
                return
            print(f"followers of @{cfg['username']} read: {len(fol)} -> {sorted(fol)}")
            done_n = 0
            for h in un_cands:
                if h in fol:
                    print(f"  @{h}: follows back - KEEP")
                    mark_unfollowed(account, h, "kept-follows-back")
                    continue
                if done_n >= un_max:
                    break
                if dry:
                    print(f"  @{h}: would UNFOLLOW (no follow-back)")
                    done_n += 1
                    continue
                try:
                    await page.goto(f"https://www.instagram.com/{h}/", wait_until="domcontentloaded", timeout=40000)
                    await page.wait_for_timeout(3000)
                    await human_scroll(page, rounds=1)
                    r = await do_unfollow(page)
                except Exception as e:
                    print(f"  @{h}: error {str(e)[:60]}")
                    continue
                print(f"  @{h}: {r}")
                if r == "unfollowed":
                    mark_unfollowed(account, h, r)
                if r == "unfollowed":
                    done_n += 1
                elif r == "no-unfollow-option":
                    print("  ⚠️ unfollow menu not found - stopping to be safe.")
                    break
                await jitter(page, cfg["delay"] * 2)
            await context.close()
            print("done.")
            return

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
            # land on the profile, look around a bit before doing anything
            await human_scroll(page)
            acts = []
            if cfg["view_stories"]:                       # stories first (least committal)
                sv = await view_stories(page, handle); acts.append(f"story:{'y' if sv else 'n'}")
                await jitter(page, cfg["delay"] // 2)
            if cfg["like_recent"]:
                liked = await like_recent(page, handle, cfg["like_recent"]); acts.append(f"liked:{liked}")
                await human_scroll(page, rounds=random.randint(1, 3))
                await jitter(page, cfg["delay"] // 2)
            if cfg["follow"]:                             # follow last — most rate-limited action
                r = await do_follow(page); acts.append(f"follow:{r}")
                print(f"    · follow → {r}")
                if r == "followed":
                    mark_done(account, handle, "follow")
            print(f"       → {' '.join(acts)}")
            mark_done(account, handle, "engage")
            await human_scroll(page, rounds=random.randint(1, 2))
            await jitter(page, cfg["delay"])
        await context.close()
    print("done.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("account")
    ap.add_argument("mode", nargs="?", default="validate", choices=["login", "validate", "engage", "unfollow", "unfollow-dry"])
    a = ap.parse_args()
    asyncio.run(run(a.account, a.mode))


if __name__ == "__main__":
    main()
