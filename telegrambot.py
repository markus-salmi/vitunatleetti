#!/usr/bin/env python3
"""
atleetti_bot.py — watch atleetti.fi and push every new post to Telegram.

Setup:
  1. Talk to @BotFather on Telegram -> /newbot -> copy the token.
  2. Send any message to your new bot (or add it to a group/channel as admin).
  3. export TELEGRAM_BOT_TOKEN="123456:ABC..."
     python atleetti_bot.py --find-chat-id      # prints your chat id
     export TELEGRAM_CHAT_ID="12345678"
  4. python atleetti_bot.py                     # runs forever, polls every 5 min

Options:
  --once              check a single time and exit (good for cron / systemd timer)
  --notify-existing   on the very first run, send the posts already on the site
                      (default: first run just records them silently)
  --test              send a test message and exit
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

def load_dotenv(path: str = ".env") -> None:
    """Read KEY=VALUE lines from a .env file next to this script.

    Real environment variables always win, so an export still overrides the file.
    """
    env_file = Path(__file__).resolve().parent / path
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_dotenv()

SITE = "https://atleetti.fi"
WP_API_URL = f"{SITE}/wp-json/wp/v2/posts"
RSS_URL = f"{SITE}/feed/"

USER_AGENT = "atleetti-telegram-notifier/1.0"
HTTP_TIMEOUT = 20
MAX_REMEMBERED = 500

STATE_FILE = Path(os.getenv("STATE_FILE", "seen_posts.json"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "300"))  # seconds
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# Comma-separated WordPress category IDs; empty means "everything".
CATEGORIES = os.getenv("CATEGORIES", "").replace(" ", "")

log = logging.getLogger("atleetti")

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


# --------------------------------------------------------------------------
# Fetching posts — three strategies, best first
# --------------------------------------------------------------------------

def _strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def fetch_wp_api() -> list[dict]:
    """WordPress REST API. Most reliable: real post IDs and timestamps."""
    params = {"per_page": 15, "orderby": "date", "_fields": "id,link,title,date_gmt"}
    if CATEGORIES:
        params["categories"] = CATEGORIES
    r = session.get(WP_API_URL, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    posts = []
    for item in r.json():
        posts.append({
            "id": f"wp:{item['id']}",
            "url": item["link"],
            "title": _strip_tags(item.get("title", {}).get("rendered", "")) or item["link"],
            "date": item.get("date_gmt", ""),
        })
    return posts


def fetch_rss() -> list[dict]:
    """Standard WordPress RSS feed."""
    r = session.get(RSS_URL, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    posts = []
    for item in root.iterfind(".//item"):
        link = (item.findtext("link") or "").strip()
        if not link:
            continue
        guid = (item.findtext("guid") or link).strip()
        posts.append({
            "id": f"rss:{guid}",
            "url": link,
            "title": _strip_tags(item.findtext("title") or "") or link,
            "date": (item.findtext("pubDate") or "").strip(),
        })
    return posts


# Homepage links that are articles, not sections
_ARTICLE_RE = re.compile(
    r'<a[^>]+href="(https://atleetti\.fi/([a-z0-9][a-z0-9\-]{15,})/)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_NOT_ARTICLES = ("/category/", "/tag/", "/author/", "/wp-", "/page/")


def fetch_html() -> list[dict]:
    """Last-resort scrape of the front page."""
    r = session.get(SITE, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    posts, seen = [], set()
    for url, slug, inner in _ARTICLE_RE.findall(r.text):
        if url in seen or any(p in url for p in _NOT_ARTICLES):
            continue
        seen.add(url)
        title = _strip_tags(inner)
        if not title:
            m = re.search(r'title="([^"]+)"', inner)
            title = html.unescape(m.group(1)) if m else slug.replace("-", " ")
        posts.append({"id": f"url:{url}", "url": url, "title": title, "date": ""})
    return posts


SOURCES = [("wp-api", fetch_wp_api), ("rss", fetch_rss), ("html", fetch_html)]


def get_posts() -> list[dict]:
    """Try each source in order; return the first one that yields posts."""
    # The RSS and HTML sources can't filter by category, so if a filter is set
    # we only use the API rather than silently sending everything.
    sources = SOURCES[:1] if CATEGORIES else SOURCES
    last_error = None
    for name, fn in sources:
        try:
            posts = fn()
            if posts:
                log.debug("fetched %d posts via %s", len(posts), name)
                return posts
            log.warning("%s returned no posts, trying next source", name)
        except Exception as exc:  # noqa: BLE001 - never let a source kill the loop
            log.warning("%s failed: %s", name, exc)
            last_error = exc
    if last_error:
        raise last_error
    return []


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_seen() -> list[str]:
    if not STATE_FILE.exists():
        return []
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data.get("seen", []) if isinstance(data, dict) else list(data)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("could not read %s (%s) — starting fresh", STATE_FILE, exc)
        return []


def save_seen(seen: list[str]) -> None:
    trimmed = seen[-MAX_REMEMBERED:]
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps({"seen": trimmed}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)  # atomic: never leave a half-written state file


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def telegram(method: str, **payload):
    if not BOT_TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN is not set")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    for attempt in range(4):
        try:
            r = session.post(url, json=payload, timeout=HTTP_TIMEOUT)
            if r.status_code == 429:
                wait = r.json().get("parameters", {}).get("retry_after", 5)
                log.warning("rate limited, sleeping %ss", wait)
                time.sleep(wait + 1)
                continue
            r.raise_for_status()
            return r.json()["result"]
        except requests.RequestException as exc:
            log.warning("telegram %s failed (attempt %d): %s", method, attempt + 1, exc)
            time.sleep(2 ** attempt)
    log.error("giving up on telegram %s", method)
    return None


def send_post(post: dict) -> bool:
    text = (
        f"<b>{html.escape(post['title'])}</b>\n"
        f'<a href="{html.escape(post["url"], quote=True)}">Lue juttu →</a>'
    )
    result = telegram(
        "sendMessage",
        chat_id=CHAT_ID,
        text=text,
        parse_mode="HTML",
        link_preview_options={"is_disabled": False},
    )
    return result is not None


def find_chat_id() -> None:
    updates = telegram("getUpdates") or []
    if not updates:
        print("No updates. Send a message to your bot first, then run this again.")
        print("(For a group: add the bot, then post something. For a channel: make")
        print(" it an admin and post something.)")
        return
    seen = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            seen[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name", "")
    for cid, name in seen.items():
        print(f"chat_id={cid}   {name}")


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def list_categories() -> None:
    """Print every category on the site with its ID and post count."""
    cats, page = [], 1
    while True:
        r = session.get(
            f"{SITE}/wp-json/wp/v2/categories",
            params={"per_page": 100, "page": page,
                    "_fields": "id,name,slug,count,parent"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        batch = r.json()
        cats.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    by_id = {c["id"]: c for c in cats}
    print(f"{'ID':>6}  {'posts':>6}  name")
    print("-" * 46)
    for c in sorted(cats, key=lambda c: -c["count"]):
        parent = by_id.get(c.get("parent"))
        label = f"{parent['name']} > {c['name']}" if parent else c["name"]
        print(f"{c['id']:>6}  {c['count']:>6}  {label}")
    print("\nPut the IDs you want in .env, e.g.:  CATEGORIES=12,34")


def check(seen: list[str], notify: bool) -> list[str]:
    posts = get_posts()
    known = set(seen)
    # oldest first so notifications arrive in chronological order
    new = [p for p in reversed(posts) if p["id"] not in known]

    if not new:
        log.info("no new posts (%d checked)", len(posts))
        return seen

    if not notify:
        log.info("first run: recording %d existing posts without notifying", len(new))
        return seen + [p["id"] for p in new]

    log.info("%d new post(s)", len(new))
    for post in new:
        if send_post(post):
            log.info("sent: %s", post["title"])
            seen.append(post["id"])
            time.sleep(1)  # stay well under Telegram's rate limits
        else:
            log.error("failed to send %s — will retry next round", post["url"])
    return seen


def main() -> None:
    ap = argparse.ArgumentParser(description="Telegram notifier for atleetti.fi")
    ap.add_argument("--once", action="store_true", help="check once and exit")
    ap.add_argument("--notify-existing", action="store_true",
                    help="on first run, send posts already on the site")
    ap.add_argument("--find-chat-id", action="store_true", help="print your chat id and exit")
    ap.add_argument("--list-categories", action="store_true",
                    help="print every category on atleetti.fi with its ID, then exit")
    ap.add_argument("--categories", default=None,
                    help="only watch these category IDs, e.g. --categories 12,34")
    ap.add_argument("--test", action="store_true", help="send a test message and exit")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL, help="seconds between checks")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    global CATEGORIES
    if args.categories is not None:
        CATEGORIES = args.categories.replace(" ", "")

    if args.list_categories:
        list_categories()
        return

    if args.find_chat_id:
        find_chat_id()
        return

    if CATEGORIES:
        log.info("watching only categories: %s", CATEGORIES)

    if not CHAT_ID:
        sys.exit("TELEGRAM_CHAT_ID is not set (run with --find-chat-id to look it up)")

    if args.test:
        ok = send_post({"title": "Testiviesti — atleetti-botti toimii ✅", "url": SITE})
        print("sent" if ok else "failed")
        return

    first_run = not STATE_FILE.exists()
    seen = load_seen()

    while True:
        try:
            seen = check(seen, notify=not first_run or args.notify_existing)
            save_seen(seen)
            first_run = False
        except Exception as exc:  # noqa: BLE001 - keep the daemon alive
            log.error("check failed: %s", exc)

        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
