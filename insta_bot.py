import argparse
import asyncio
import base64
import csv
import json
import random
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import aiohttp
from openai import AsyncOpenAI
from playwright.async_api import async_playwright, TimeoutError
import yaml

# ============ PATHS & SETTINGS ============
ACCOUNTS_ROOT = Path("accounts")
ACCOUNT_CONFIG_FILENAME = "config.yaml"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "max_posts_per_tag": 5,
    "scroll_rounds": 1,
    "post_order": "top",
    "like_posts": True,
    "save_posts": True,
    "like_comments": True,
    "enable_comment_replies": True,
    "max_comment_replies": 2,
    "max_comment_likes": 3,
    "min_comment_words": 4,
    "enable_story_viewing": True,
    "story_view_delay_ms": 5000,
    "max_story_views": 5,
    "story_like_probability": 50.0,
    "max_story_likes_per_user": 3,
    "max_story_views_per_user": 5,
    "action_delay_ms": 1500,
    "debug_reply_plan": False,
    "headless": False,
}

GENERIC_PHRASES = {
    "nice",
    "great",
    "love",
    "awesome",
    "cool",
    "amazing",
    "wow",
    "yes",
    "yess",
    "yesss",
    "🔥",
    "💯",
    "🙌",
    "👏",
}

client: Optional[AsyncOpenAI] = None


# ============ CONFIG HELPERS ============
def ensure_account_paths(account_name: str) -> Dict[str, Path]:
    account_dir = ACCOUNTS_ROOT / account_name
    user_data_dir = account_dir / "user_data"
    comments_csv = account_dir / "commented_posts.csv"
    stories_csv = account_dir / "viewed_stories.csv"
    account_dir.mkdir(parents=True, exist_ok=True)
    return {
        "root": account_dir,
        "user_data": user_data_dir,
        "comments_csv": comments_csv,
        "stories_csv": stories_csv,
    }


def load_account_settings(account_name: str) -> Dict[str, Any]:
    account_dir = ACCOUNTS_ROOT / account_name
    config_path = account_dir / ACCOUNT_CONFIG_FILENAME
    if not config_path.exists():
        raise FileNotFoundError(
            f"Missing config file for account '{account_name}' at {config_path}"
        )

    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
        if raw is None:
            raw = {}

    credentials = raw.get("credentials") or {}
    username = credentials.get("username", account_name)
    password = credentials.get("password")
    api_key = credentials.get("openai_api_key")

    if not password:
        raise ValueError(f"Password missing in config for '{account_name}'.")
    if not api_key:
        raise ValueError(f"OpenAI API key missing in config for '{account_name}'.")

    settings: Dict[str, Any] = DEFAULT_SETTINGS.copy()
    engagement = raw.get("engagement") or {}
    commenting = raw.get("commenting") or {}
    stories = raw.get("stories") or {}

    for key in ("max_posts_per_tag", "scroll_rounds"):
        if key in engagement:
            settings[key] = int(engagement[key])
    if "max_comment_replies" in engagement:
        settings["max_comment_replies"] = int(engagement["max_comment_replies"])
    if "post_order" in engagement:
        settings["post_order"] = str(engagement["post_order"]).strip().lower()
    for bool_key in ("like_posts", "save_posts", "like_comments", "enable_comment_replies"):
        if bool_key in engagement:
            settings[bool_key] = bool(engagement[bool_key])

    for key in ("max_comment_likes", "min_comment_words"):
        if key in commenting:
            settings[key] = int(commenting[key])

    if "enable_story_viewing" in stories:
        settings["enable_story_viewing"] = bool(stories["enable_story_viewing"])

    for key in ("story_view_delay_ms", "max_story_views", "max_story_likes_per_user"):
        if key in stories:
            settings[key] = int(stories[key])

    if "story_like_probability" in stories:
        settings["story_like_probability"] = float(stories["story_like_probability"])

    hashtags = raw.get("hashtags") or []
    if isinstance(hashtags, str):
        hashtags = [h.strip() for h in hashtags.split(",") if h.strip()]
    if not isinstance(hashtags, list):
        raise ValueError("Expected 'hashtags' to be a list or comma-separated string in config.")

    settings.update(
        {
            "account_name": account_name,
            "username": username,
            "password": password,
            "openai_api_key": api_key,
            "hashtags": hashtags,
        }
    )

    if "debug_reply_plan" in raw:
        settings["debug_reply_plan"] = bool(raw["debug_reply_plan"])
    if "headless" in raw:
        settings["headless"] = bool(raw["headless"])

    return settings


# ============ CSV TRACKING ============
def _load_ids(csv_path: Path) -> Set[str]:
    if not csv_path.exists():
        return set()
    with csv_path.open("r", newline="", encoding="utf-8") as csvfile:
        return {
            row[0].strip()
            for row in csv.reader(csvfile)
            if row and row[0].strip()
        }


def _append_with_date(csv_path: Path, value: str) -> None:
    with csv_path.open("a", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow([value, datetime.utcnow().date().isoformat()])


def load_commented_posts(csv_path: Path) -> Set[str]:
    return _load_ids(csv_path)


def save_commented_post(csv_path: Path, post_id: str) -> None:
    _append_with_date(csv_path, post_id)


def load_viewed_users(csv_path: Path) -> Set[str]:
    return _load_ids(csv_path)


def save_viewed_user(csv_path: Path, username: str) -> None:
    _append_with_date(csv_path, username)


# ============ OPENAI HELPERS ============
async def describe_image(image_url: str) -> str:
    if client is None:
        raise RuntimeError("OpenAI client is not initialized.")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(image_url) as resp:
                if resp.status != 200:
                    raise Exception(f"Failed to download image: HTTP {resp.status}")
                image_bytes = await resp.read()

        base64_image = base64.b64encode(image_bytes).decode("utf-8")
        data_uri = f"data:image/jpeg;base64,{base64_image}"

        print("📤 Sending image to GPT‑4o for visual analysis...")

        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Describe what you see in this Instagram post image.",
                        },
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
            max_tokens=300,
        )

        vision_text = response.choices[0].message.content.strip()
        print("✅ GPT Vision Response:", vision_text)
        return vision_text

    except Exception as e:
        print(f"❌ Error analyzing image with GPT: {e}")
        return ""


def _build_comments_summary(
    comments_data: List[Dict[str, Any]], limit: int = 20
) -> (str, Dict[int, Dict[str, Any]]):
    lines: List[str] = []
    mapping: Dict[int, Dict[str, Any]] = {}
    for idx, comment in enumerate(comments_data[:limit], start=1):
        username = comment.get("username", "").strip()
        text = (comment.get("comment") or "").replace("\n", " ").strip()
        if not username and not text:
            continue
        lines.append(f"{idx}. {username}: {text}")
        mapping[idx] = comment
    return ("\n".join(lines) if lines else "No other comments yet."), mapping


async def generate_comment_plan(
    caption_text: str,
    vision_text: str,
    comments_summary: str,
    max_replies: int,
    allow_replies: bool = True,
) -> Dict[str, Any]:
    if client is None:
        raise RuntimeError("OpenAI client is not initialized.")

    instruction = (
        "You are a kind, emotionally intelligent Instagram creator. "
        "Craft supportive, friendly messages that feel natural and concise. "
        "For the main comment, reference the caption/image context and add fresh insight or encouragement—"
        "never repeat call-to-actions like 'comment DETOX' or restate single-word prompts. "
        "For replies, speak as the post author—acknowledge the commenter, celebrate their effort, relate to what they shared, "
        "and avoid stealing the spotlight."
    )

    reply_instructions = (
        "Return a JSON object with the keys:\n"
        '- "main_comment": string, under 20 words, no quotes at ends.\n'
        '- "replies": list of objects with keys "comment_index" (number referencing the list below) '
        'and "reply" (string under 25 words). Provide at most '
        f"{max(0, max_replies)} replies. Use unique indices.\n"
        "If no replies, use an empty list.\n"
        "Only choose replies where you can encourage or acknowledge the commenter in a helpful way. Prioritize questions, shared experiences, or meaningful appreciation that deserves a response. Skip generic triggers (e.g., 'Printable', single-word submissions, emojis) or comments that feel very personal/private to the original creator.\n"
        "Replies must feel like thoughtful responses to the commenter—show appreciation, relate to their note, "
        "and keep the focus on encouraging them. If a comment feels very personal or directed specifically to the post creator, do not reply to it.\n"
        "Do not include any text outside the JSON."
    )

    if not allow_replies or max_replies <= 0:
        reply_instructions += "\nThere are no eligible comments to reply to; set replies to []."

    extra_note = ""
    if not allow_replies or max_replies <= 0:
        extra_note = "\nNo replies are needed for the existing comments—simply return an empty replies list."

    user_content = (
        f"{reply_instructions}\n\n"
        f"Post caption:\n{caption_text or 'No caption.'}\n\n"
        f"Image insights:\n{vision_text or 'No vision details.'}\n\n"
        f"Existing comments (numbered):\n{comments_summary}\n\n"
        f"Only reference comments from this list."
        f"{extra_note}"
    )

    try:
        if not allow_replies or max_replies <= 0:
            print("🧪 Debug Prompt (replies disabled):")
        print("--- GPT Prompt: ---")
        print(user_content)
        print("--- End Prompt ---")

        print("\n🧠 Generating main comment + reply plan from GPT‑4o...")
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": instruction},
                {"role": "user", "content": user_content},
            ],
            max_tokens=300,
        )

        raw = response.choices[0].message.content.strip()
        print("📨 Raw AI plan:", raw)

        json_text = raw
        if "```" in raw:
            json_text = "\n".join(
                line for line in raw.splitlines() if not line.strip().startswith("```")
            )

        plan = json.loads(json_text)
        main_comment = (plan.get("main_comment") or "").strip()
        if not main_comment:
            raise ValueError("Missing main_comment in AI response.")

        replies = plan.get("replies") or []
        cleaned_replies: List[Dict[str, Any]] = []
        for item in replies:
            try:
                idx = int(item.get("comment_index"))
                reply_text = (item.get("reply") or "").strip()
                if not reply_text:
                    continue
                cleaned_replies.append({"comment_index": idx, "reply": reply_text})
            except Exception:
                continue

        if max_replies >= 0:
            cleaned_replies = cleaned_replies[: max_replies or 0]

        return {
            "main_comment": main_comment.strip('"').strip("'"),
            "replies": cleaned_replies,
            "raw": raw,
        }

    except Exception as e:
        print(f"❌ Error generating comment plan: {e}")
        return {
            "main_comment": "",
            "replies": [],
            "raw": "",
            "skip": True,
        }


async def fetch_comments(page) -> List[Dict[str, Any]]:
    comments_data: List[Dict[str, Any]] = []
    try:
        comment_elements = await page.locator(
            "div[role='dialog'] ul > div:last-child > div > div > div"
        ).all()

        for el in comment_elements:
            try:
                username_elem = el.locator("h3")
                comment_text_elem = el.locator("h3 + div")

                await username_elem.wait_for(state="attached", timeout=5000)
                await comment_text_elem.wait_for(state="attached", timeout=5000)

                username_text = ((await username_elem.text_content()) or "").strip()
                comment_text = ((await comment_text_elem.text_content()) or "").strip()

                handle_text = ""
                try:
                    username_anchor = username_elem.locator("a").first
                    if await username_anchor.count() > 0:
                        href = await username_anchor.get_attribute("href")
                        if href:
                            handle_text = href.strip("/").split("?")[0].split("/")[-1]
                except Exception:
                    handle_text = ""

                story_container = el.locator("div[aria-disabled]").first
                has_story = False
                if await story_container.count() > 0:
                    disabled_attr = await story_container.get_attribute("aria-disabled")
                    has_story = disabled_attr != "true"
                else:
                    has_story = True

                if not username_text and not comment_text:
                    continue

                comments_data.append(
                    {
                        "username": username_text,
                        "handle": handle_text,
                        "comment": comment_text,
                        "locator": el,
                        "has_story": has_story,
                    }
                )
            except Exception as e:
                print(f"⚠️ Skipping comment element: {e}")
                continue
    except Exception as e:
        print(f"⚠️ Failed to extract comments: {e}")

    return comments_data


def _comment_signature(comment: Dict[str, Any]) -> tuple:
    handle = (comment.get("handle") or comment.get("username") or "").strip().lower()
    text = (comment.get("comment") or "").replace("\u200b", "").strip()
    return handle, text


# ============ COMMENT HELPERS ============
def _clean_word(word: str) -> str:
    return "".join(ch for ch in word if ch.isalnum())


def is_quality_comment(text: str, min_words: int) -> bool:
    if not text:
        return False

    stripped = text.strip()
    if len(stripped) < 10:
        return False

    words = [_clean_word(part) for part in stripped.split()]
    meaningful_words = [word for word in words if len(word) > 2]
    return len(meaningful_words) >= min_words


async def like_thoughtful_comments(
    comments_data: List[Dict[str, Any]],
    max_comment_likes: int,
    min_comment_words: int,
) -> List[str]:
    if max_comment_likes <= 0:
        return []

    eligible = [
        comment
        for comment in comments_data
        if comment.get("locator")
        and comment.get("username")
        and is_quality_comment(comment.get("comment", ""), min_comment_words)
    ]

    if not eligible:
        return []

    random.shuffle(eligible)
    liked_users: List[str] = []

    for comment in eligible[:max_comment_likes]:
        try:
            heart = comment["locator"].locator("button svg[aria-label='Like']")
            if await heart.count() == 0:
                continue

            await heart.first.click()
            liked_users.append(comment["username"])
            print(f"💗 Liked comment from {comment['username']}")
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"⚠️ Could not like comment from {comment['username']}: {e}")

    return liked_users


async def view_commenter_stories(
    context,
    page,
    story_comments: List[Dict[str, Any]],
    settings: Dict[str, Any],
    viewed_users: Set[str],
    stories_csv_path: Path,
    action_delay_ms: int,
) -> None:
    max_views = int(settings.get("max_story_views", 0) or 0)
    if max_views <= 0:
        print("ℹ️ Story viewing disabled by limit.")
        return

    delay_ms = int(settings.get("story_view_delay_ms", 5000))
    like_probability = max(
        0.0,
        min(1.0, float(settings.get("story_like_probability", 0.0)) / 100.0),
    )
    max_likes_per_user = int(settings.get("max_story_likes_per_user", 0))
    max_views_per_user = int(settings.get("max_story_views_per_user", 0))

    filtered_comments = []
    for comment in story_comments:
        username = comment["username"].split()[0].lstrip("@")
        normalized = username.lower()
        if normalized in viewed_users:
            continue
        filtered_comments.append((comment, username, normalized))

    if not filtered_comments:
        print("ℹ️ All commenter stories already viewed previously.")
        return

    for idx, (story_comment, username, normalized) in enumerate(filtered_comments, start=1):
        if idx > max_views:
            print(f"ℹ️ Reached story view limit ({max_views}).")
            break

        story_url = f"https://www.instagram.com/stories/{username}/"
        print(f"👀 Opening story for {username}: {story_url}")

        story_page = None
        try:
            story_page = await context.new_page()
            await story_page.goto(story_url, timeout=20000, wait_until="domcontentloaded")
            # Detect Instagram "not available" screen
            unavailable_banner = story_page.locator("text='The link may be broken or the page may have been removed.'")
            if await unavailable_banner.count() > 0:
                print(f"⚠️ Story unavailable for {username}, skipping.")
                continue

            try:
                view_story_button = story_page.locator(
                    "div[role='button']:has-text('View story')"
                )
                await view_story_button.wait_for(state="visible", timeout=5000)
                await view_story_button.click()
                print("👉 Clicked 'View story' prompt.")
            except Exception:
                print("ℹ️ 'View story' prompt not detected.")

            await story_page.wait_for_timeout(500)

            likes_given = 0
            slide_count = 0
            while True:
                error_span = story_page.locator(
                    "span:has-text(\"Sorry, we're having trouble with playing this video.\")"
                )
                error_detected = False
                try:
                    await error_span.wait_for(state="visible", timeout=1000)
                    print("⚠️ Story slide failed to play, moving on.")
                    await story_page.wait_for_timeout(200)
                    error_detected = True
                except TimeoutError:
                    await story_page.wait_for_timeout(delay_ms)

                like_button = story_page.locator("svg[aria-label='Like']").first
                next_button = story_page.locator("svg[aria-label='Next']").first

                try:
                    has_next = await next_button.is_visible()
                except Exception:
                    has_next = False

                if max_views_per_user > 0 and slide_count >= max_views_per_user:
                    print(
                        f"ℹ️ Reached per-user story slide view limit "
                        f"({max_views_per_user}) for {username}."
                    )
                    break

                if (
                    like_probability > 0
                    and (max_likes_per_user <= 0 or likes_given < max_likes_per_user)
                    and not error_detected
                    and random.random() < like_probability
                ):
                    try:
                        has_like = await like_button.count() > 0
                        if has_like:
                            has_like = await like_button.is_visible()
                    except Exception:
                        has_like = False

                    if has_like:
                        try:
                            await like_button.click()
                            print("❤️ Liked this story slide.")
                            likes_given += 1
                        except Exception as e:
                            print(f"⚠️ Failed to like story slide: {e}")

                if not has_next:
                    print("ℹ️ No further story slides, closing story.")
                    break

                try:
                    await next_button.click()
                    print("➡️ Moved to next story slide.")
                    slide_count += 1
                except Exception as e:
                    print(f"⚠️ Failed to advance story: {e}")
                    break
        except Exception as e:
            print(f"⚠️ Failed to view story for {username}: {e}")
        finally:
            if story_page:
                await story_page.close()

        viewed_users.add(normalized)
        save_viewed_user(stories_csv_path, normalized)
        await pause_after(page, action_delay_ms, "Before next story viewer")


async def pause_after(page, delay_ms: int, label: str) -> None:
    if delay_ms > 0:
        await page.wait_for_timeout(delay_ms)
        print(f"⏳ {label}: waited {delay_ms}ms.")


async def close_modal(page, delay: int = 2000) -> None:
    try:
        await page.keyboard.press("Escape")
        await pause_after(page, delay, "After closing modal")
        print(f"🧭 Closed modal.")
    except Exception as e:
        print(f"⚠️ Failed to close modal: {e}")


# ============ LOGIN ============
async def perform_login(page, username: str, password: str) -> None:
    print("🔑 Logging in...")

    username_field = page.locator("input[name='username'], input[name='email']")
    password_field = page.locator("input[name='password'], input[name='pass']")

    try:
        await username_field.wait_for(state="visible", timeout=5000)
        await password_field.wait_for(state="visible", timeout=5000)
    except Exception:
        print("⚠️ Could not locate login inputs.")
        return

    await username_field.first.fill(username)
    await password_field.first.fill(password)

    login_button = page.locator("button[type='submit'], div[role='button']:has-text('Log in')")
    if await login_button.count() > 0:
        await login_button.first.click()
    else:
        await password_field.first.press("Enter")

    await page.wait_for_timeout(6000)
    print("✅ Login attempt complete.")


# ============ ACCOUNT RUNNER ============
async def process_account(settings: Dict[str, Any]) -> None:
    username = settings["username"]
    password = settings["password"]
    hashtags = settings.get("hashtags", [])

    if not hashtags:
        print(f"⚠️ No hashtags configured for @{username}, skipping account.")
        return

    paths = ensure_account_paths(username)
    commented_posts = load_commented_posts(paths["comments_csv"])
    viewed_story_users = load_viewed_users(paths["stories_csv"])
    print(
        f"\n🚀 Starting session for @{username} "
        f"(loaded {len(commented_posts)} previously engaged posts, "
        f"{len(viewed_story_users)} previously viewed story authors)."
    )

    async with async_playwright() as p:
        headless_flag = bool(settings.get("headless", False))
        print("🕶️ Running in headless mode…" if headless_flag else "🪟 Running with visible browser window…")
        context = await p.chromium.launch_persistent_context(
            str(paths["user_data"]),
            headless=headless_flag,
        )
        page = context.pages[0] if context.pages else await context.new_page()

        print("🌐 Opening Instagram home page...")
        await page.goto(
            "https://www.instagram.com/", timeout=20000, wait_until="domcontentloaded"
        )

        # --- Handle cookies ---
        try:
            button = page.locator("button:has-text('Allow all cookies')")
            if await button.count() > 0:
                await button.first.click(force=True)
                print("✅ Clicked 'Allow all cookies'")
            else:
                print("ℹ️ No cookie prompt detected.")
        except Exception:
            print("⚠️ Cookie popup interaction failed.")

        # --- Login ---
        await page.wait_for_timeout(1000)
        login_inputs = page.locator("input[name='username'], input[name='email']")
        try:
            has_inputs = await login_inputs.count() > 0
        except Exception:
            has_inputs = False

        needs_login = (
            any(token in page.url for token in ("/accounts/login/", "/accounts/onetap/"))
            or has_inputs
        )

        if needs_login:
            await perform_login(page, username, password)
            # Navigate back to home feed after login
            await page.goto(
                "https://www.instagram.com/", timeout=20000, wait_until="domcontentloaded"
            )
        else:
            print("✅ Session remembered, already logged in.")

        # --- Process hashtags ---
        max_posts_per_tag = int(settings.get("max_posts_per_tag", 0))
        scroll_rounds = int(settings.get("scroll_rounds", 0))
        max_comment_likes = int(settings.get("max_comment_likes", 0))
        min_comment_words = int(settings.get("min_comment_words", 0))

        story_settings = {
            "enabled": bool(settings.get("enable_story_viewing", False)),
            "story_view_delay_ms": int(settings.get("story_view_delay_ms", 0)),
            "max_story_views": int(settings.get("max_story_views", 0)),
            "story_like_probability": float(settings.get("story_like_probability", 0.0)),
            "max_story_likes_per_user": int(settings.get("max_story_likes_per_user", 0)),
            "max_story_views_per_user": int(settings.get("max_story_views_per_user", 0)),
        }
        action_delay_ms = int(settings.get("action_delay_ms", 0))

        post_order = settings.get("post_order", "top")

        for tag in hashtags:
            print(f"\n🏷️ Visiting hashtag: #{tag}")
            await page.goto(
                f"https://www.instagram.com/explore/tags/{tag}/", timeout=20000
            )
            await page.wait_for_timeout(3000)

            for i in range(max(scroll_rounds, 0)):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(4000)
                print(f"🔽 Scrolled page ({i + 1}/{scroll_rounds})")

            posts_locator = page.locator("main a[href^='/p/']")
            posts = await posts_locator.all()
            print(f"🔍 Found {len(posts)} posts under #{tag}")

            if post_order == "bottom":
                posts = list(reversed(posts))

            save_posts = bool(settings.get("save_posts", False))

            if not posts:
                print(f"⚠️ No posts found for #{tag}, skipping.")
                continue

            for index, post in enumerate(posts[:max_posts_per_tag], start=1):
                try:
                    href = await post.get_attribute("href")
                    if not href:
                        continue

                    post_id = href.split("/")[-2]
                    if post_id in commented_posts:
                        print(f"⏭️ Post {post_id} already engaged with, skipping.")
                        continue

                    print(f"\n📸 Opening post {index}/{max_posts_per_tag}: {post_id}")
                    await post.click()
                    await page.wait_for_timeout(4000)

                    # --- Like the post if not already liked ---
                    try:
                        if settings.get("like_posts", False):
                            like_button = page.locator(
                                "div[role='dialog'] article > div > div:last-child "
                                "section:first-child svg[aria-label='Like']"
                            )

                            if await like_button.count() > 0:
                                await like_button.click()
                                print("❤️ Liked the post.")
                            else:
                                print("👍 Post already liked or like button not found.")
                        else:
                            print("🛑 Post liking disabled by config.")
                    except Exception as e:
                        print(f"⚠️ Failed to like post: {e}")

                    if save_posts:
                        try:
                            save_button = page.locator(
                                "div[role='dialog'] article svg[aria-label='Save']"
                            )
                            if await save_button.count() > 0:
                                await save_button.first.click()
                                print("📌 Saved the post.")
                            else:
                                print("📎 Post already saved or save button not found.")
                        except Exception as e:
                            print(f"⚠️ Failed to save post: {e}")

                    # 🗨️ Extract comments
                    comments_data = await fetch_comments(page)
                    for comment in comments_data:
                        print(
                            f"💬 {comment.get('username')}: {comment.get('comment')} "
                            f"(Story: {comment.get('has_story')})"
                        )

                    comment_box = page.locator(
                        "form textarea[aria-label='Add a comment…']"
                    )
                    if await comment_box.count() == 0:
                        print("❌ Commenting not available on this post. Skipping.")
                        save_commented_post(paths["comments_csv"], post_id)
                        commented_posts.add(post_id)
                        await close_modal(page, action_delay_ms or 0)
                        continue

                    if any(comment["username"] == username for comment in comments_data):
                        print(f"⏭️ Already commented as {username}, skipping.")
                        save_commented_post(paths["comments_csv"], post_id)
                        commented_posts.add(post_id)
                        await close_modal(page, action_delay_ms or 0)
                        continue

                    image_elems = await page.locator(
                        "article > .html-div > div:first-child img"
                    ).all()
                    image_urls = [
                        await img.get_attribute("src")
                        for img in image_elems
                        if await img.get_attribute("src")
                    ]
                    if not image_urls:
                        print("⚠️ No images found. Skipping.")
                        await close_modal(page, action_delay_ms or 0)
                        continue

                    first_image = image_urls[0]
                    print(f"🧠 Sending first image to GPT for analysis: {first_image}")
                    vision_text = await describe_image(first_image)

                    caption = ""
                    try:
                        caption_elem = page.locator("div[role='dialog'] ul h1")
                        if await caption_elem.count() > 0:
                            caption = await caption_elem.inner_text()
                            print(f"📝 Caption: {caption[:120]}")
                    except Exception as e:
                        print(f"⚠️ Could not read caption: {e}")

                    liked_users = await like_thoughtful_comments(
                        comments_data,
                        max_comment_likes if settings.get("like_comments", False) else 0,
                        min_comment_words,
                    )
                    if liked_users:
                        print(f"💗 Liked comments from: {', '.join(liked_users)}")
                    else:
                        print("ℹ️ No meaningful comments to like.")

                    self_username_lower = username.lower()

                    quality_comments = [
                        comment
                        for comment in comments_data
                        if comment.get("username")
                        and comment.get("comment")
                        and (comment.get("username") or "").strip().lower()
                        != self_username_lower
                        and is_quality_comment(
                            comment.get("comment", ""), min_comment_words
                        )
                    ]

                    max_reply_targets = (
                        int(settings.get("max_comment_replies", 0))
                        if settings.get("enable_comment_replies", False)
                        else 0
                    )
                    summary_source = (
                        quality_comments
                        if quality_comments
                        else [
                            c
                            for c in comments_data
                            if (c.get("username") or "").strip().lower()
                            != self_username_lower
                        ]
                    )
                    quality_map = (
                        {id(comment): True for comment in quality_comments}
                        if quality_comments
                        else {}
                    )

                    if not quality_comments:
                        max_reply_targets = 0

                    summary_text, comment_index_map = _build_comments_summary(
                        summary_source
                    )
                    print("🧾 Comment index map:")
                    for idx, data in comment_index_map.items():
                        print(
                            f"   #{idx} -> user: {data.get('username')} "
                            f"(@{data.get('handle')}) comment: {data.get('comment')}"
                        )
                    plan = await generate_comment_plan(
                        caption,
                        vision_text,
                        summary_text,
                        max_reply_targets,
                        allow_replies=bool(quality_comments),
                    )
                    main_comment = plan.get("main_comment", "")
                    if not main_comment:
                        print("⚠️ Skipping commenting due to empty AI plan.")
                        await close_modal(page, action_delay_ms or 0)
                        continue
                    reply_plan = plan.get("replies", [])

                    if quality_comments:
                        reply_plan = [
                            item
                            for item in reply_plan
                            if (
                                comment_index_map.get(item.get("comment_index"))
                                and comment_index_map[item["comment_index"]] in quality_comments
                            )
                        ]
                    else:
                        reply_plan = []

                    if settings.get("debug_reply_plan"):
                        print("🧪 Debug Reply Plan:", json.dumps(plan, indent=2))

                    comment = main_comment

                    try:
                        await comment_box.click()
                        await comment_box.fill(comment)
                        await page.keyboard.press("Enter")
                        print(f"✅ Comment submitted: {comment}")
                        save_commented_post(paths["comments_csv"], post_id)
                        commented_posts.add(post_id)
                        await page.wait_for_timeout(5000)
                    except Exception as e:
                        print(f"❌ Failed to post comment: {e}")
                        await close_modal(page, action_delay_ms or 0)
                        continue

                    if reply_plan:
                        print("🔄 Refreshing comment references before posting replies…")
                        updated_comments = await fetch_comments(page)
                        signature_map = {
                            _comment_signature(comment): comment
                            for comment in updated_comments
                        }
                        for idx, data in comment_index_map.items():
                            sig = _comment_signature(data)
                            if sig in signature_map:
                                matched = signature_map[sig]
                                data["locator"] = matched.get("locator")
                                if matched.get("handle"):
                                    data["handle"] = matched.get("handle")
                        print("🧾 Updated comment index map after refresh:")
                        for idx, data in comment_index_map.items():
                            print(
                                f"   #{idx} -> user: {data.get('username')} "
                                f"(@{data.get('handle')}) comment: {data.get('comment')}"
                            )

                    replies_done = 0
                    for item in reply_plan:
                        if replies_done >= max_reply_targets:
                            break

                        comment_idx = item.get("comment_index")
                        reply_text = (item.get("reply") or "").strip()
                        if not comment_idx or not reply_text:
                            continue

                        target_comment = comment_index_map.get(comment_idx)
                        if not target_comment:
                            continue

                        comment_locator = target_comment.get("locator")
                        if not comment_locator:
                            continue

                        try:
                            handle_value = target_comment.get("handle") or target_comment.get("username") or ""
                            normalized_handle = handle_value.strip().split()[0].lstrip("@")
                            print(
                                f"🧵 Preparing reply #{comment_idx} → "
                                f"{target_comment.get('username')} (@{normalized_handle}): {target_comment.get('comment')}"
                            )
                            reply_button = comment_locator.locator(
                                ":scope button:has(span:has-text('Reply'))"
                            ).first

                            if await reply_button.count() == 0:
                                print(f"⚠️ No reply button for comment index {comment_idx}.")
                                continue

                            reply_box = page.locator("form textarea[aria-label='Add a comment…']")
                            mention = f"@{normalized_handle}" if normalized_handle else ""

                            mention_ready = False
                            for attempt in range(3):
                                await reply_button.click()
                                print(f"Clicking on reply button on comment #{comment_idx} (attempt {attempt + 1})")
                                await reply_box.wait_for(state="visible", timeout=3000)
                                await reply_box.focus()
                                print(f"⏳ Waiting for mention prefill '{mention}' before replying…")
                                total_wait = 0
                                while total_wait < 4000:
                                    current_value = (await reply_box.input_value() or "").strip()
                                    if mention and current_value.lower().startswith(mention.lower()):
                                        mention_ready = True
                                        print(f"✅ Mention detected for comment #{comment_idx}: {current_value}")
                                        break
                                    await page.wait_for_timeout(250)
                                    total_wait += 250
                                if mention_ready or not mention:
                                    break
                                print(
                                    f"⚠️ Mention mismatch for comment #{comment_idx}: "
                                    f"expected prefix '{mention}', saw '{current_value}'. Retrying…"
                                )
                                await page.keyboard.press("Escape")

                            if not mention_ready and mention:
                                print(
                                    f"⚠️ Failed to detect mention prefill for comment #{comment_idx}, skipping reply."
                                )
                                await page.keyboard.press("Escape")
                                continue

                            composed_reply = (f"{mention} {reply_text.strip()}").strip()
                            print(f"📝 Final reply text for comment #{comment_idx}: {composed_reply}")
                            await reply_box.fill(composed_reply)
                            if settings.get("debug_reply_plan"):
                                current_value = await reply_box.input_value()
                                print(
                                    f"🧪 Reply draft for comment #{comment_idx}: {current_value}"
                                )
                                await page.wait_for_timeout(3000)
                                await reply_box.fill("")
                                continue
                            await page.keyboard.press("Enter")
                            replies_done += 1
                            print(
                                f"🗨️ Replied to comment #{comment_idx}: {reply_text}"
                            )
                            await page.wait_for_timeout(1000)
                        except Exception as e:
                            print(
                                f"⚠️ Failed to reply to comment #{comment_idx}: {e}"
                            )
                            continue

                    if settings.get("debug_reply_plan"):
                        print("🛑 Debug mode: stopping before replies or stories.")
                        await close_modal(page, action_delay_ms or 0)
                        return

                    if story_settings["enabled"]:
                        story_comments = [
                            comment
                            for comment in comments_data
                            if comment.get("has_story")
                        ]
                        if story_comments:
                            await view_commenter_stories(
                                context,
                                page,
                                story_comments,
                                story_settings,
                                viewed_story_users,
                                paths["stories_csv"],
                                action_delay_ms,
                            )
                        else:
                            print(
                                "ℹ️ No commenter stories detected, skipping story view."
                            )

                    await close_modal(page, action_delay_ms or 0)

                except Exception as e:
                    print(f"❌ Error processing post #{index}: {e}")
                    traceback.print_exc()
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(1000)

        print(f"🏁 Finished session for @{username}. Closing browser.")
        await context.close()


# ============ ENTRY POINT ============
async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Instagram engagement bot for a configured account."
    )
    parser.add_argument(
        "account",
        nargs="?",
        help="Account name (folder under accounts/). Defaults to 'default' if omitted.",
    )
    args = parser.parse_args()

    ACCOUNTS_ROOT.mkdir(parents=True, exist_ok=True)

    account_name = args.account or "default"

    try:
        settings = load_account_settings(account_name)
    except Exception as e:
        print(f"❌ Failed to load config for '{account_name}': {e}")
        traceback.print_exc()
        return

    try:
        global client
        client = AsyncOpenAI(api_key=settings["openai_api_key"])
        await process_account(settings)
    except Exception as e:
        print(f"❌ Fatal error for @{settings['username']}: {e}")
        traceback.print_exc()
    finally:
        client = None


if __name__ == "__main__":
    asyncio.run(main())
