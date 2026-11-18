#!/usr/bin/env python3
"""
Pokémon GO events bot: posts upcoming Pokémon GO events to Discord.

Data: ScrapedDuck (community scrape of leekduck.com), events.min.json on GitHub.
Posts each event once, when it starts within the next LOOKAHEAD_DAYS days:
Community Days, Spotlight Hours, Raid Hours/Days, raid bosses, Max Battles,
GO Tour and regular events. Skips GO Battle League, seasons and GO Pass.

Remembers what it posted in pogo_seen.json. Never fails the workflow.

Env vars:
  POKEMON_GO_WEBHOOK_URL  webhook in the Pokémon channel
  DRY_RUN=1               print instead of posting
"""
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DATA_URL = "https://raw.githubusercontent.com/bigfoott/ScrapedDuck/data/events.min.json"
STATE_FILE = Path(__file__).with_name("pogo_seen.json")
MISSING_FILE = Path(__file__).with_name("missing_shinies.json")
# Hyphenated species names (so "jangmo-o" isn't read as a form of "jangmo")
HYPHENATED = {"jangmo-o", "hakamo-o", "kommo-o", "type-null", "tapu-bulu", "tapu-fini",
              "tapu-lele", "tapu-koko", "porygon-z", "ho-oh", "mr-mime", "mr-rime", "mime-jr",
              "nidoran-m", "nidoran-f", "iron-boulder", "iron-crown", "iron-leaves",
              "walking-wake", "raging-bolt", "gouging-fire", "great-tusk", "roaring-moon"}
LOOKAHEAD_DAYS = 7
MAX_POSTS = 8
SKIP_TYPES = {"go-battle-league", "season", "go-pass"}
ET = ZoneInfo("America/New_York")
COLORS = {
    "community-day": 0x3BA55D, "pokemon-spotlight-hour": 0xF1C40F,
    "raid-hour": 0xE74C3C, "raid-day": 0xE74C3C, "raid-battles": 0xC0392B,
    "max-battles": 0x9B59B6, "max-mondays": 0x9B59B6, "pokemon-go-tour": 0x1ABC9C,
}
WEBHOOK = os.environ.get("POKEMON_GO_WEBHOOK_URL", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
UA = "DiscordBot (https://github.com/NotTimTurner/mtg-spoiler-bot, 1.0)"


def parse(ts):
    """ScrapedDuck times without 'Z' are local time wherever you are."""
    if not ts:
        return None, False
    if ts.endswith("Z"):
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ET), False
    return datetime.fromisoformat(ts).replace(tzinfo=ET), True


def fmt_time(dt):
    return dt.strftime("%I:%M %p").lstrip("0")


def when(e):
    start, local = parse(e.get("start"))
    end, _ = parse(e.get("end"))
    suffix = " local time" if local else " ET"
    if not start:
        return "Date TBA"
    if end and start.date() == end.date():
        return f"{start:%a %b} {start.day} · {fmt_time(start)}–{fmt_time(end)}{suffix}"
    s = f"{start:%a %b} {start.day}, {fmt_time(start)}"
    if end:
        s += f" → {end:%a %b} {end.day}, {fmt_time(end)}"
    return s + suffix


def details(e):
    x = e.get("extraData") or {}
    lines = []
    cd = x.get("communityday") or {}
    if cd.get("spawns"):
        lines.append("**Featured:** " + ", ".join(s["name"] for s in cd["spawns"]))
    if cd.get("bonuses"):
        lines.append("**Bonuses:** " + "; ".join(b["text"].rstrip("*") for b in cd["bonuses"][:6]))
    sp = x.get("spotlight") or {}
    if sp:
        mons = sp.get("list") or [sp]
        names = ", ".join(m["name"] + (" ✨" if m.get("canBeShiny") else "") for m in mons)
        lines.append(f"**Pokémon:** {names}")
        if sp.get("bonus"):
            lines.append(f"**Bonus:** {sp['bonus']}")
    rb = x.get("raidbattles") or {}
    if rb.get("bosses"):
        names = ", ".join(b["name"] + (" ✨" if b.get("canBeShiny") else "") for b in rb["bosses"])
        lines.append(f"**Bosses:** {names}")
    if rb.get("shinies") and not rb.get("bosses"):
        lines.append("**Can be shiny:** " + ", ".join(m["name"] for m in rb["shinies"]))
    if any("✨" in l for l in lines):
        lines.append("✨ = can be shiny")
    return lines


def load_missing():
    try:
        return json.loads(MISSING_FILE.read_text())["missing"]
    except Exception:
        return []


def slugify(name):
    n = name.lower()
    n = re.sub(r"\(.*?\)", "", n)
    for word in ("shadow ", "mega ", "dynamax ", "gigantamax ", "primal ", "alolan ", "galarian ",
                 "hisuian ", "paldean "):
        n = n.replace(word, "")
    n = n.replace("♀", "-f").replace("♂", "-m").replace(".", "").replace("'", "").replace("é", "e")
    return re.sub(r"[^a-z0-9]+", "-", n).strip("-")


def species(slug):
    for h in HYPHENATED:
        if slug == h or slug.startswith(h + "-"):
            return h
    return slug.split("-")[0]


def missing_shinies(e, missing):
    """Missing-shiny slugs that this event could help with."""
    if not missing:
        return []
    x = e.get("extraData") or {}
    names = [s["name"] for s in (x.get("communityday") or {}).get("spawns", [])]
    sp = x.get("spotlight") or {}
    names += [m["name"] for m in (sp.get("list") or ([sp] if sp else []))]
    rb = x.get("raidbattles") or {}
    names += [b["name"] for b in rb.get("bosses", [])] + [b["name"] for b in rb.get("shinies", [])]
    wanted = {species(slugify(n)) for n in names if n}
    raw = e.get("name", "").lower().replace("♂", " m ").replace("♀", " f ")
    title = " " + re.sub(r"[^a-z0-9]+", " ", raw) + " "
    hits = []
    for m in missing:
        sp_name = species(m)
        if sp_name in wanted or f" {sp_name.replace('-', ' ')} " in title:
            hits.append(m)
    return hits


def pretty(slug):
    sp_name = species(slug)
    base = {"type-null": "Type: Null", "nidoran-m": "Nidoran♂", "nidoran-f": "Nidoran♀"}.get(
        sp_name, sp_name.title())
    form = slug[len(sp_name):].strip("-").replace("-", " ")
    return f"{base} ({form})" if form else base


def post(payload):
    req = urllib.request.Request(
        WEBHOOK, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA})
    urllib.request.urlopen(req, timeout=30).read()


def main():
    if not WEBHOOK and not DRY_RUN:
        print("POKEMON_GO_WEBHOOK_URL not set; skipping Pokémon GO events")
        return
    seen = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else []
    seen_set = set(seen)
    missing = load_missing()
    req = urllib.request.Request(f"{DATA_URL}?_={int(time.time())}", headers={"User-Agent": UA})
    events = json.load(urllib.request.urlopen(req, timeout=30))
    now = datetime.now(ET)
    horizon = now + timedelta(days=LOOKAHEAD_DAYS)
    due = []
    for e in events:
        if e.get("eventType") in SKIP_TYPES or e.get("eventID") in seen_set:
            continue
        start, _ = parse(e.get("start"))
        end, _ = parse(e.get("end"))
        # skip events that are over, too far out, or already running for days
        if not start or start > horizon or (end and end < now) or start < now - timedelta(days=2):
            continue
        due.append((start, e))
    due.sort(key=lambda t: t[0])
    print(f"Pokémon GO: {len(events)} events listed, {len(due)} new within {LOOKAHEAD_DAYS} days")
    count = 0
    for start, e in due[:MAX_POSTS]:
        lines = [f"**{e.get('heading') or 'Event'}** · {when(e)}"] + details(e)
        hits = missing_shinies(e, missing)
        if hits:
            shown = ", ".join(pretty(h) for h in hits[:8]) + (f" +{len(hits) - 8} more" if len(hits) > 8 else "")
            lines.insert(1, f"🎯 **Shiny missing from Tim's dex:** {shown}")
        embed = {
            "title": e["name"][:256], "url": e.get("link"),
            "description": "\n".join(lines)[:4000],
            "color": COLORS.get(e.get("eventType"), 0x5865F2),
            "footer": {"text": "Data: LeekDuck via ScrapedDuck"},
        }
        if e.get("image"):
            embed["image"] = {"url": e["image"]}
        if DRY_RUN:
            print(json.dumps(embed, ensure_ascii=False, indent=1))
        else:
            post({"username": "Pokémon GO Events", "embeds": [embed],
                  "allowed_mentions": {"parse": []}})
            time.sleep(2)
        seen.append(e["eventID"]); seen_set.add(e["eventID"]); count += 1
        if not DRY_RUN:
            STATE_FILE.write_text(json.dumps(seen[-500:], indent=1) + "\n")
    if not DRY_RUN:
        STATE_FILE.write_text(json.dumps(seen[-500:], indent=1) + "\n")
    print(f"{count} Pokémon GO event(s) posted")


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:  # never break the spoiler workflow
        print(f"Pokémon GO bot error: {ex}")
