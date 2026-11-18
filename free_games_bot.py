#!/usr/bin/env python3
"""
Free games bot: posts free-to-keep PC games (Epic, Steam, GOG) to #gaming.

Data: GamerPower API (gamerpower.com/api). Posts each giveaway once.
Skips giveaways published more than MAX_AGE_DAYS ago, so the first run
doesn't dig up old ones. Remembers what it posted in free_games_seen.json.
Never fails the workflow.

Env vars:
  FREE_GAMES_WEBHOOK_URL  webhook in #gaming
  DRY_RUN=1               print instead of posting
"""
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

API = "https://www.gamerpower.com/api/filter?platform=epic-games-store.steam.gog&type=game"
STATE_FILE = Path(__file__).with_name("free_games_seen.json")
MAX_AGE_DAYS = 14
MAX_POSTS = 6
WEBHOOK = os.environ.get("FREE_GAMES_WEBHOOK_URL", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
UA = "DiscordBot (https://github.com/NotTimTurner/mtg-spoiler-bot, 1.0)"
COLORS = {"Epic Games Store": 0x2F2F2F, "Steam": 0x1B2838, "GOG": 0x86328A}


def store(platforms):
    for name in COLORS:
        if name in platforms:
            return name
    return platforms


def main():
    if not WEBHOOK and not DRY_RUN:
        print("FREE_GAMES_WEBHOOK_URL not set; skipping free games")
        return
    seen = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else []
    seen_set = set(seen)
    req = urllib.request.Request(API, headers={"User-Agent": UA, "Accept": "application/json"})
    data = json.load(urllib.request.urlopen(req, timeout=30))
    if not isinstance(data, list):
        print(f"GamerPower returned no list: {data}")
        return
    cutoff = datetime.now() - timedelta(days=MAX_AGE_DAYS)
    new = []
    for g in data:
        gid = str(g.get("id"))
        if gid in seen_set or g.get("status") != "Active":
            continue
        try:
            published = datetime.strptime(g["published_date"], "%Y-%m-%d %H:%M:%S")
        except (KeyError, ValueError):
            published = datetime.now()
        if published < cutoff:
            seen.append(gid); seen_set.add(gid)  # too old to announce; remember quietly
            continue
        new.append(g)
    print(f"Free games: {len(data)} active, {len(new)} new")
    count = 0
    for g in new[:MAX_POSTS]:
        shop = store(g.get("platforms", ""))
        title = g["title"].replace(" Giveaway", "").replace(" Key", "")
        lines = [f"**Free on {shop}**" + (f" (normally {g['worth']})" if g.get("worth") not in (None, "", "N/A") else "")]
        if g.get("end_date") and g["end_date"] != "N/A":
            end = datetime.strptime(g["end_date"], "%Y-%m-%d %H:%M:%S")
            lines.append(f"Ends {end:%a %b} {end.day}")
        if g.get("description"):
            lines.append(g["description"][:300])
        embed = {
            "title": title[:256], "url": g.get("open_giveaway_url") or g.get("gamerpower_url"),
            "description": "\n".join(lines),
            "color": COLORS.get(shop, 0x5865F2),
            "footer": {"text": "Free to keep · via GamerPower"},
        }
        if g.get("image"):
            embed["image"] = {"url": g["image"]}
        if DRY_RUN:
            print(json.dumps(embed, indent=1))
        else:
            req = urllib.request.Request(
                WEBHOOK, data=json.dumps({"username": "Free Games", "embeds": [embed],
                                          "allowed_mentions": {"parse": []}}).encode(),
                headers={"Content-Type": "application/json", "User-Agent": UA})
            urllib.request.urlopen(req, timeout=30).read()
            time.sleep(2)
        seen.append(str(g["id"])); seen_set.add(str(g["id"])); count += 1
        if not DRY_RUN:
            STATE_FILE.write_text(json.dumps(seen[-500:], indent=1) + "\n")
    if not DRY_RUN:
        STATE_FILE.write_text(json.dumps(seen[-500:], indent=1) + "\n")
    print(f"{count} free game(s) posted")


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:  # never break the spoiler workflow
        print(f"Free games bot error: {ex}")
