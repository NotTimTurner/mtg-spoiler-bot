#!/usr/bin/env python3
"""
MTG Spoiler Bot: posts newly spoiled cards from Scryfall to a Discord webhook.

- Watches every set releasing in the future (plus sets released in the last
  GRACE_DAYS days, to catch late additions). Token/memorabilia sets are skipped.
- Remembers what it already posted in seen.json, so each card is posted once.
- First run (no seen.json yet) just records the existing cards without posting,
  so the channel doesn't get flooded with hundreds of cards.

Env vars:
  DISCORD_WEBHOOK_URL  required (unless DRY_RUN=1)
  DRY_RUN=1            print what would be posted instead of posting
  MAX_POSTS            max cards per run (default 60; the rest go next run)
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

SCRYFALL = "https://api.scryfall.com"
HEADERS = {"User-Agent": "MTGSpoilerDiscordBot/1.0", "Accept": "application/json"}
STATE_FILE = Path(__file__).with_name("seen.json")
GRACE_DAYS = 7
SKIP_SET_TYPES = {"token", "memorabilia", "minigame"}
SKIP_REPRINTS_IN = {"commander"}  # in these set types, only post brand-new cards
# Sets that never "release" because new drops keep being added to them (Secret Lair).
# These are always watched, but only cards dated within the window are checked.
ALWAYS_WATCH = {"sld"}
MAX_POSTS = int(os.environ.get("MAX_POSTS", "60"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
# Early alerts from r/magicTCG posts flaired "Spoiler". A Reddit app login is optional;
# add it if Reddit blocks GitHub's servers.
REDDIT_SUB = "magicTCG"
REDDIT_ID = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")

# Discord embed colors by card color
COLORS = {"W": 0xF8F6D8, "U": 0x0E68AB, "B": 0x150B00, "R": 0xD3202A, "G": 0x00733E}
MULTI, COLORLESS = 0xCFB53B, 0x9E9E9E


def http_json(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
        return json.loads(body) if body else None


def scryfall(url):
    time.sleep(0.1)  # Scryfall asks for 50-100 ms between requests
    try:
        return http_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # search with no results
            return {"data": [], "has_more": False}
        raise


def watched_sets():
    cutoff = (date.today() - timedelta(days=GRACE_DAYS)).isoformat()
    sets = scryfall(f"{SCRYFALL}/sets")["data"]
    return [
        s for s in sets
        if s["code"] in ALWAYS_WATCH or (
            s.get("released_at", "0000") >= cutoff
            and s.get("set_type") not in SKIP_SET_TYPES
            and not s.get("digital")
        )
    ]


def cards_in_set(code):
    query = f"e:{code}"
    if code in ALWAYS_WATCH:
        cutoff = (date.today() - timedelta(days=GRACE_DAYS)).isoformat()
        query += f" date>={cutoff}"
    # include_extras: brand-new previews often aren't tagged as paper cards yet,
    # and Scryfall's default search hides them until they are.
    q = urllib.parse.urlencode({
        "q": query, "unique": "prints", "order": "spoiled", "dir": "asc",
        "include_extras": "true", "include_variations": "true",
    })
    url = f"{SCRYFALL}/cards/search?{q}"
    cards = []
    while url:
        page = scryfall(url)
        cards.extend(page.get("data", []))
        url = page.get("next_page") if page.get("has_more") else None
    return cards


def oracle_id(card):
    return card.get("oracle_id") or (card.get("card_faces") or [{}])[0].get("oracle_id")


def older_printings(oracle_ids, released_at):
    """Return the oracle IDs that were printed in any set released before `released_at`.

    Scryfall's `reprint` flag is often wrong for brand-new previews, so we check
    for earlier printings directly.
    """
    found = set()
    ids = [o for o in oracle_ids if o]
    for i in range(0, len(ids), 20):
        chunk = ids[i:i + 20]
        query = f"date<{released_at} (" + " or ".join(f"oracleid:{o}" for o in chunk) + ")"
        params = urllib.parse.urlencode({"q": query, "unique": "cards", "include_extras": "true"})
        url = f"{SCRYFALL}/cards/search?{params}"
        while url:
            page = scryfall(url)
            found.update(oracle_id(c) for c in page.get("data", []))
            url = page.get("next_page") if page.get("has_more") else None
    return found


def card_text(face):
    lines = [f"**{face.get('type_line', '')}**"]
    if face.get("oracle_text"):
        lines.append(face["oracle_text"])
    if "power" in face:
        lines.append(f"*{face['power']}/{face['toughness']}*")
    if "loyalty" in face:
        lines.append(f"*Loyalty: {face['loyalty']}*")
    return "\n".join(lines)


def to_embed(card, set_name):
    faces = card.get("card_faces") or [card]
    image = (card.get("image_uris") or faces[0].get("image_uris") or {}).get("normal")
    if len(faces) > 1:
        desc = "\n\n".join(f"__{f['name']}__ {f.get('mana_cost', '')}\n{card_text(f)}" for f in faces)
        title = card["name"]
    else:
        desc = card_text(card)
        title = f"{card['name']} {card.get('mana_cost', '')}".strip()
    colors = card.get("colors") or faces[0].get("colors") or []
    color = COLORS.get(colors[0], COLORLESS) if len(colors) == 1 else (MULTI if colors else COLORLESS)
    embed = {
        "title": title[:256],
        "url": card.get("scryfall_uri"),
        "description": desc[:4000],
        "color": color,
        "footer": {"text": f"{set_name} · {card.get('rarity', '').title()}"},
    }
    if image:
        embed["image"] = {"url": image}
    return embed


def reddit_listing():
    """Newest posts from r/magicTCG (uses a Reddit app login if one is configured)."""
    ua = {"User-Agent": "script:mtg-spoiler-discord-bot:1.0"}
    if REDDIT_ID and REDDIT_SECRET:
        auth = base64.b64encode(f"{REDDIT_ID}:{REDDIT_SECRET}".encode()).decode()
        token = http_json(
            "https://www.reddit.com/api/v1/access_token",
            data=b"grant_type=client_credentials",
            headers={**ua, "Authorization": f"Basic {auth}",
                     "Content-Type": "application/x-www-form-urlencoded"},
        )["access_token"]
        url = f"https://oauth.reddit.com/r/{REDDIT_SUB}/new?limit=100&raw_json=1"
        headers = {**ua, "Authorization": f"Bearer {token}"}
    else:
        url = f"https://www.reddit.com/r/{REDDIT_SUB}/new.json?limit=100&raw_json=1"
        headers = ua
    return [c["data"] for c in http_json(url, headers=headers)["data"]["children"]]


def reddit_image(p):
    if p.get("is_gallery"):
        items = (p.get("gallery_data") or {}).get("items") or []
        meta = p.get("media_metadata") or {}
        if items and items[0].get("media_id") in meta:
            return (meta[items[0]["media_id"]].get("s") or {}).get("u")
    link = p.get("url_overridden_by_dest") or p.get("url") or ""
    if re.search(r"\.(jpe?g|png|webp|gif)$", link.split("?")[0], re.I):
        return link
    images = (p.get("preview") or {}).get("images") or []
    return images[0]["source"]["url"] if images else None


def reddit_embed(p):
    link = p.get("url_overridden_by_dest") or ""
    embed = {
        "title": ("🔔 " + p["title"])[:256],
        "url": "https://www.reddit.com" + p["permalink"],
        "color": 0xFF4500,
        "footer": {"text": f"r/{REDDIT_SUB} · early spoiler alert (full card follows once Scryfall adds it)"},
    }
    if link and "reddit.com" not in link and "redd.it" not in link:
        embed["description"] = f"Source: {link}"[:4000]
    image = reddit_image(p)
    if image:
        embed["image"] = {"url": image}
    return embed


def reddit_alerts(seen, new_seen, first_run):
    """Post new Spoiler-flaired r/magicTCG posts. Never lets a Reddit problem stop the Scryfall part."""
    previous = seen.get("_reddit")
    new_seen["_reddit"] = list(previous or [])
    try:
        posts = [p for p in reddit_listing()
                 if "spoiler" in (p.get("link_flair_text") or "").lower() and not p.get("stickied")]
    except Exception as e:  # noqa: BLE001
        print(f"Reddit check failed ({e}); skipping Reddit this run.")
        return
    known = set(new_seen["_reddit"])
    fresh = [p for p in reversed(posts) if p["id"] not in known]  # oldest first
    if first_run or previous is None:
        new_seen["_reddit"].extend(p["id"] for p in fresh)
        print(f"Reddit: first check, recorded {len(fresh)} existing spoiler posts, posted nothing.")
    else:
        print(f"Reddit: {len(fresh)} new spoiler posts.")
        for i in range(0, len(fresh), 10):
            batch = fresh[i:i + 10]
            if DRY_RUN:
                for p in batch:
                    print(" - [reddit]", p["title"])
            else:
                post_embeds([reddit_embed(p) for p in batch])
                time.sleep(2)
            new_seen["_reddit"].extend(p["id"] for p in batch)
    new_seen["_reddit"] = new_seen["_reddit"][-500:]  # keep the file small


def post_embeds(embeds):
    payload = json.dumps({"username": "MTG Spoilers", "embeds": embeds}).encode()
    headers = {"Content-Type": "application/json", "User-Agent": HEADERS["User-Agent"]}
    for attempt in range(5):
        try:
            http_json(WEBHOOK, data=payload, headers=headers)
            return
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limited
                retry = json.loads(e.read() or b"{}").get("retry_after", 5)
                time.sleep(float(retry) + 0.5)
                continue
            raise
    raise RuntimeError("Discord kept rate-limiting; giving up this run")


def main():
    if not WEBHOOK and not DRY_RUN:
        sys.exit("DISCORD_WEBHOOK_URL is not set")

    first_run = not STATE_FILE.exists()
    seen = {} if first_run else json.loads(STATE_FILE.read_text())

    new_seen = {}
    reddit_alerts(seen, new_seen, first_run)  # fast alerts first

    sets = watched_sets()
    print(f"Watching {len(sets)} sets: {', '.join(s['code'].upper() for s in sets)}")

    new = []  # (set_code, card, set_name)
    for s in sets:
        code = s["code"]
        already = set(seen.get(code, []))
        new_seen[code] = list(already)
        fresh = [c for c in cards_in_set(code) if c["id"] not in already]
        if s.get("set_type") in SKIP_REPRINTS_IN and fresh:
            old = older_printings({oracle_id(c) for c in fresh}, s["released_at"])
            reprints = [c for c in fresh if c.get("reprint") or oracle_id(c) in old]
            new_seen[code].extend(c["id"] for c in reprints)  # skip them for good
            fresh = [c for c in fresh if c not in reprints]
            if reprints:
                print(f"{code.upper()}: skipped {len(reprints)} reprints")
        new.extend((code, c, s["name"]) for c in fresh)

    if first_run:
        for code, card, _ in new:
            new_seen[code].append(card["id"])
        print(f"First run: recorded {len(new)} existing cards, posted nothing.")
    else:
        to_post = new[:MAX_POSTS]
        print(f"{len(new)} new cards; posting {len(to_post)} this run.")
        for i in range(0, len(to_post), 10):  # Discord allows 10 embeds per message
            batch = to_post[i:i + 10]
            embeds = [to_embed(c, n) for _, c, n in batch]
            if DRY_RUN:
                for e in embeds:
                    print(" -", e["title"], "|", e["footer"]["text"])
            else:
                post_embeds(embeds)
                time.sleep(2)
            for code, card, _ in batch:  # mark seen only after a successful post
                new_seen[code].append(card["id"])

    # Sets that left the watch window are dropped, which keeps the file small.
    STATE_FILE.write_text(json.dumps(new_seen, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
