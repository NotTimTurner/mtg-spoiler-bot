#!/usr/bin/env python3
"""
Arena daily deals bot: posts @mtgadailydeals.com's daily Bluesky post to the
#deals forum (Arena tag) with the deals image shown in the post.

Replaces the MonitoRSS feed, whose Bluesky RSS has no image. Reads the public
Bluesky API (no login), posts each "Daily Deals" post once, and remembers what
it posted in arena_seen.json. Never fails the workflow.

Env vars:
  DEALS_DISCORD_WEBHOOK_URL  webhook in the #deals forum (same one deals_bot.py uses)
  DRY_RUN=1                  print instead of posting
"""
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ACTOR = "mtgadailydeals.com"
FEED_URL = ("https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?"
            + urllib.parse.urlencode({"actor": ACTOR, "limit": 10, "filter": "posts_no_replies"}))
STATE_FILE = Path(__file__).with_name("arena_seen.json")
ARENA_TAG = "1572361945103990885"   # "Arena" tag in the #deals forum
MAX_AGE = timedelta(days=2)
MAX_POSTS = 3
UA = "DiscordBot (https://github.com/NotTimTurner/mtg-spoiler-bot, 1.0)"
WEBHOOK = os.environ.get("DEALS_DISCORD_WEBHOOK_URL", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def download(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read(), r.headers.get_content_type()


def post(payload, image=None):
    """Post to the webhook. image = (bytes, content_type) is uploaded as an attachment,
    which Discord shows reliably (it won't render cdn.bsky.app links in embeds)."""
    if not image:
        data, ctype = json.dumps(payload).encode(), "application/json"
    else:
        body, itype = image
        ext = {"image/png": "png", "image/webp": "webp"}.get(itype, "jpg")
        name = f"arena-deals.{ext}"
        payload["embeds"][0]["image"] = {"url": f"attachment://{name}"}
        payload["attachments"] = [{"id": 0, "filename": name}]
        b = "----arenadeals" + str(int(time.time() * 1000))
        parts = [
            f'--{b}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
            f"Content-Type: application/json\r\n\r\n".encode() + json.dumps(payload).encode() + b"\r\n",
            f'--{b}\r\nContent-Disposition: form-data; name="files[0]"; filename="{name}"\r\n'
            f"Content-Type: {itype}\r\n\r\n".encode() + body + b"\r\n",
            f"--{b}--\r\n".encode(),
        ]
        data, ctype = b"".join(parts), f"multipart/form-data; boundary={b}"
    req = urllib.request.Request(WEBHOOK + "?wait=true", data=data,
                                 headers={"Content-Type": ctype, "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def main():
    if not WEBHOOK and not DRY_RUN:
        print("DEALS_DISCORD_WEBHOOK_URL not set; skipping Arena deals")
        return
    seen = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else []
    feed = get_json(FEED_URL).get("feed", [])
    now = datetime.now(timezone.utc)
    new = []
    for item in feed:
        if "reason" in item:            # skip reposts / pinned-post markers
            continue
        p = item["post"]
        rec = p.get("record", {})
        text = rec.get("text", "")
        if p["uri"] in seen or "daily deals" not in text.lower():
            continue
        created = datetime.fromisoformat(rec.get("createdAt", "").replace("Z", "+00:00"))
        if now - created > MAX_AGE:
            seen.append(p["uri"])       # old: remember silently
            continue
        new.append((created, p, text))
    new.sort(key=lambda x: x[0])
    print(f"Arena deals: {len(feed)} posts checked, {len(new)} new")

    for created, p, text in new[:MAX_POSTS]:
        rkey = p["uri"].rsplit("/", 1)[-1]
        web = f"https://bsky.app/profile/{ACTOR}/post/{rkey}"
        images = (p.get("embed") or {}).get("images") or []
        lines = text.split("\n")
        title_line = lines[0].strip() or "MTG Arena Daily Deals"
        body = "\n".join(lines[1:]).strip()
        embed = {
            "title": title_line[:256],
            "url": web,
            "description": body[:4000],
            "color": 0xE67E22,
            "footer": {"text": f"@{ACTOR} on Bluesky"},
            "timestamp": created.isoformat(),
        }
        image = None
        if images:
            try:
                image = download(images[0]["fullsize"])
            except Exception as e:
                print(f"Couldn't download the deals image: {e}")
        local = created.astimezone(timezone(timedelta(hours=-5)))
        thread = f"Arena Daily Deals - {local.strftime('%a %b')} {local.day}"
        if DRY_RUN:
            print(json.dumps({"thread_name": thread, "embed": embed, "image_bytes": len(image[0]) if image else 0}, indent=1))
        else:
            post({"username": "Arena Daily Deals", "allowed_mentions": {"parse": []},
                  "thread_name": thread, "applied_tags": [ARENA_TAG], "embeds": [embed]}, image)
            print("Posted:", thread)
        seen.append(p["uri"])
        time.sleep(2)

    if not DRY_RUN:
        STATE_FILE.write_text(json.dumps(seen[-200:], indent=1) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the spoiler workflow
        print(f"Arena deals bot error: {e}")
