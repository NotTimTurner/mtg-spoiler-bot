#!/usr/bin/env python3
"""
RSS news bot: posts new articles from a few RSS/Atom feeds to Discord news channels.

Replaces the 3-feed MonitoRSS free tier. Add a feed by adding a line to FEEDS.
Posts each article once and remembers it in rss_seen.json. The first time a
feed is seen, only its newest article is posted (the rest are marked seen) so
the channel isn't flooded. Never fails the workflow.

Env vars:
  GAMING_NEWS_WEBHOOK_URL   webhook in #gaming-news
  GUNPLA_NEWS_WEBHOOK_URL   webhook in #gunpla-news
  DRY_RUN=1                 print instead of posting
"""
import html
import json
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

# name, feed url, webhook env var, embed color
FEEDS = [
    ("Gematsu", "https://www.gematsu.com/feed", "GAMING_NEWS_WEBHOOK_URL", 0xE53935),
    ("Gundam Kits Collection", "https://www.gundamkitscollection.com/feeds/posts/default?alt=rss",
     "GUNPLA_NEWS_WEBHOOK_URL", 0xF57C00),
]
STATE_FILE = Path(__file__).with_name("rss_seen.json")
MAX_PER_FEED = 3          # per run, so a busy feed can't flood the channel
MAX_AGE = timedelta(days=3)
KEEP = 300                # ids remembered per feed
DRY_RUN = os.environ.get("DRY_RUN") == "1"
UA = "Mozilla/5.0 (compatible; NotTimTurner-news-bot; +https://github.com/NotTimTurner/mtg-spoiler-bot)"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}


def text(el, path):
    found = el.find(path, NS)
    return (found.text or "").strip() if found is not None and found.text else ""


def clean(s, limit=280):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"\s+", " ", html.unescape(s)).strip()
    return s if len(s) <= limit else s[: limit - 1].rsplit(" ", 1)[0] + "…"


def parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def find_image(item, body):
    for path in ("media:content", "media:thumbnail", "enclosure"):
        for el in item.findall(path, NS):
            url = el.get("url", "")
            kind = el.get("type", "") or el.get("medium", "") or "image"
            if url and ("image" in kind or re.search(r"\.(jpe?g|png|webp|gif)(\?|$)", url, re.I)):
                return url
    m = re.search(r'<img[^>]+src="([^"]+)"', body or "")
    return html.unescape(m.group(1)) if m else None


def parse_feed(raw):
    root = ET.fromstring(raw)
    items = []
    for it in root.iter("item"):  # RSS 2.0
        body = text(it, "content:encoded") or text(it, "description")
        items.append({
            "id": text(it, "guid") or text(it, "link"),
            "title": text(it, "title"),
            "link": text(it, "link"),
            "date": parse_date(text(it, "pubDate")),
            "summary": clean(text(it, "description") or body),
            "image": find_image(it, body),
        })
    for it in root.iter("{%s}entry" % NS["atom"]):  # Atom
        link = it.find("atom:link[@rel='alternate']", NS)
        if link is None:
            link = it.find("atom:link", NS)
        body = text(it, "atom:content") or text(it, "atom:summary")
        items.append({
            "id": text(it, "atom:id") or (link.get("href") if link is not None else ""),
            "title": text(it, "atom:title"),
            "link": link.get("href", "") if link is not None else "",
            "date": parse_date(text(it, "atom:published") or text(it, "atom:updated")),
            "summary": clean(text(it, "atom:summary") or body),
            "image": find_image(it, body),
        })
    return [i for i in items if i["id"] and i["title"]]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, */*"})
    return urllib.request.urlopen(req, timeout=30).read()


def post(webhook, name, color, item):
    embed = {
        "title": html.unescape(item["title"])[:256],
        "url": item["link"] or None,
        "description": item["summary"],
        "color": color,
        "footer": {"text": name},
    }
    if item["date"]:
        embed["timestamp"] = item["date"].astimezone(timezone.utc).isoformat()
    if item["image"]:
        embed["image"] = {"url": item["image"]}
    if DRY_RUN:
        print("WOULD POST:", name, "|", embed["title"], "|", item["link"])
        return
    payload = {"username": name[:80], "embeds": [embed], "allowed_mentions": {"parse": []}}
    req = urllib.request.Request(webhook, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    urllib.request.urlopen(req, timeout=30).read()
    time.sleep(2)


def main():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    now = datetime.now(timezone.utc)
    total = 0
    for name, url, env, color in FEEDS:
        webhook = os.environ.get(env, "")
        if not webhook and not DRY_RUN:
            print(f"RSS: {name}: {env} not set; skipping")
            continue
        try:
            items = parse_feed(fetch(url))
        except Exception as ex:
            print(f"RSS: {name}: fetch/parse error: {ex}")
            continue
        first_run = name not in state
        seen = state.get(name, [])
        seen_set = set(seen)
        new = [i for i in items if i["id"] not in seen_set]
        fresh = [i for i in new if not i["date"] or now - i["date"] <= MAX_AGE]
        fresh.sort(key=lambda i: i["date"] or now)  # oldest first, so channel reads in order
        to_post = fresh[-1:] if first_run else fresh[:MAX_PER_FEED]
        print(f"RSS: {name}: {len(items)} items, {len(new)} new, posting {len(to_post)}"
              + (" (first run)" if first_run else ""))
        posted_ids = set()
        for item in to_post:
            try:
                post(webhook, name, color, item)
                posted_ids.add(item["id"]); total += 1
            except Exception as ex:
                print(f"RSS: {name}: post error: {ex}")
                break
        # remember posted items, stale items, and on first run everything
        for i in new:
            if i["id"] in posted_ids or first_run or i not in fresh:
                seen.append(i["id"])
        state[name] = seen[-KEEP:]
    if not DRY_RUN:
        STATE_FILE.write_text(json.dumps(state, indent=1) + "\n")
    print(f"RSS: {total} article(s) posted")


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:  # never break the spoiler workflow
        print(f"RSS bot error: {ex}")
