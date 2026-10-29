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
import json
import os
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
MAX_POSTS = int(os.environ.get("MAX_POSTS", "60"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")

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
        if s.get("released_at", "0000") >= cutoff
        and s.get("set_type") not in SKIP_SET_TYPES
        and not s.get("digital")
    ]


def cards_in_set(code):
    q = urllib.parse.urlencode({"q": f"e:{code}", "unique": "prints", "order": "spoiled", "dir": "asc"})
    url = f"{SCRYFALL}/cards/search?{q}"
    cards = []
    while url:
        page = scryfall(url)
        cards.extend(page.get("data", []))
        url = page.get("next_page") if page.get("has_more") else None
    return cards


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

    sets = watched_sets()
    print(f"Watching {len(sets)} sets: {', '.join(s['code'].upper() for s in sets)}")

    new = []  # (set_code, card, set_name)
    new_seen = {}
    for s in sets:
        code = s["code"]
        already = set(seen.get(code, []))
        new_seen[code] = list(already)
        for card in cards_in_set(code):
            if card["id"] not in already:
                new.append((code, card, s["name"]))

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
