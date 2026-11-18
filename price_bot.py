#!/usr/bin/env python3
"""
Price alert bot: posts MTG cards whose price jumped 50%+ to #price-alerts.

Once a day (first run after 11 AM ET) it downloads Scryfall's "default cards" bulk
file (English paper printings with TCGplayer USD prices), saves a compact snapshot,
and compares it with the snapshot from LOOKBACK_DAYS ago (or the oldest one kept).
A card is a spike when the new price is at least MIN_PRICE, it went up by at least
MIN_JUMP_PCT percent and at least MIN_JUMP_USD dollars. Nonfoil and foil are checked
separately. Snapshots live in price_history/ (kept between runs with the GitHub
Actions cache, not committed). Never fails the workflow.

Env vars:
  PRICE_ALERTS_WEBHOOK_URL  webhook in #price-alerts
  FORCE=1                   ignore the once-a-day / 11 AM gate
  DRY_RUN=1                 print instead of posting
"""
import gzip
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HIST = Path(__file__).with_name("price_history")
ET = ZoneInfo("America/New_York")
LOOKBACK_DAYS = 3
KEEP_DAYS = 8
MIN_PRICE = 3.00       # ignore bulk
MIN_OLD = 0.50         # ignore cards that were basically free
MIN_JUMP_PCT = 50
MIN_JUMP_USD = 2.00
MAX_POSTS = 15
WEBHOOK = os.environ.get("PRICE_ALERTS_WEBHOOK_URL", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
FORCE = os.environ.get("FORCE") == "1"
UA = "MTGSpoilerDiscordBot/1.0 (github.com/NotTimTurner/mtg-spoiler-bot)"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    return urllib.request.urlopen(req, timeout=300)


def iter_cards(meta):
    """Yield card objects from Scryfall's bulk file (new gzipped JSON Lines, or the old JSON array)."""
    url = meta.get("jsonl_download_uri")
    if url:
        print(f"Downloading {url} ({meta.get('compressed_size', 0) // 1_000_000} MB compressed)")
        with get(url) as r, gzip.open(r, "rt", encoding="utf-8") as lines:
            for line in lines:
                line = line.strip()
                if line:
                    yield json.loads(line)
        return
    url = meta["download_uri"]
    print(f"Downloading {url} ({meta.get('size', 0) // 1_000_000} MB)")
    with get(url) as r:
        yield from json.load(r)


def snapshot():
    meta = json.load(get("https://api.scryfall.com/bulk-data/default-cards"))
    snap = {}
    for c in iter_cards(meta):
        if c.get("digital") or c.get("lang") != "en" or "paper" not in c.get("games", []):
            continue
        p = c.get("prices") or {}
        for kind, key in (("n", "usd"), ("f", "usd_foil")):
            v = p.get(key)
            if v and float(v) >= 0.25:
                snap[f"{c['id']}:{kind}"] = [float(v), c["name"], c["set"].upper(),
                                             c.get("collector_number", ""), c.get("scryfall_uri", "")]
    return snap


def post(payload):
    req = urllib.request.Request(
        WEBHOOK, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "User-Agent": "DiscordBot (https://github.com/NotTimTurner/mtg-spoiler-bot, 1.0)"})
    urllib.request.urlopen(req, timeout=30).read()


def main():
    if not WEBHOOK and not DRY_RUN:
        print("PRICE_ALERTS_WEBHOOK_URL not set; skipping price alerts")
        return
    HIST.mkdir(exist_ok=True)
    (HIST / "UPDATED").unlink(missing_ok=True)
    now = datetime.now(ET)
    today = now.strftime("%Y-%m-%d")
    files = sorted(HIST.glob("prices-*.json.gz"))
    if not FORCE and (now.hour < 11 or any(f.name == f"prices-{today}.json.gz" for f in files)):
        print("Price alerts: already checked today (or before 11 AM ET); skipping")
        return

    snap = snapshot()
    print(f"Price snapshot: {len(snap)} prices")
    with gzip.open(HIST / f"prices-{today}.json.gz", "wt") as f:
        json.dump({k: v[0] for k, v in snap.items()}, f)
    (HIST / "UPDATED").write_text(today)

    target = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    older = [f for f in files if f.name < f"prices-{today}.json.gz"]
    for f in older:  # prune
        if f.name < (now - timedelta(days=KEEP_DAYS)).strftime("prices-%Y-%m-%d"):
            f.unlink()
    older = [f for f in older if f.exists()]
    if not older:
        print("Price alerts: first snapshot saved; alerts start tomorrow")
        return
    base_file = next((f for f in reversed(older) if f.name <= f"prices-{target}.json.gz"), older[0])
    with gzip.open(base_file, "rt") as f:
        base = json.load(f)
    base_day = base_file.name[7:17]

    spikes = []
    for key, (new, name, set_code, num, uri) in snap.items():
        old = base.get(key)
        if not old or old < MIN_OLD or new < MIN_PRICE:
            continue
        pct = (new - old) / old * 100
        if pct >= MIN_JUMP_PCT and new - old >= MIN_JUMP_USD:
            spikes.append((pct, new, old, name, set_code, num, uri, key.endswith(":f")))
    spikes.sort(reverse=True)
    # one line per card name (keep its biggest jump)
    seen, top = set(), []
    for s in spikes:
        if (s[3], s[7]) not in seen:
            seen.add((s[3], s[7]))
            top.append(s)
    print(f"Price alerts: {len(top)} cards up {MIN_JUMP_PCT}%+ since {base_day}")
    if not top:
        return
    lines = [f"**[{n}]({u})** ({s} #{c}{', foil' if foil else ''}): ${o:.2f} → **${p:.2f}** (+{pct:.0f}%)"
             for pct, p, o, n, s, c, u, foil in top[:MAX_POSTS]]
    more = f"\n…and {len(top) - MAX_POSTS} more" if len(top) > MAX_POSTS else ""
    embed = {
        "title": f"Price spikes since {base_day}",
        "description": ("\n".join(lines) + more)[:4000],
        "color": 0x2ECC71,
        "footer": {"text": f"TCGplayer market via Scryfall · min ${MIN_PRICE:.0f}, +{MIN_JUMP_PCT}% and +${MIN_JUMP_USD:.0f}"},
    }
    if DRY_RUN:
        print(json.dumps(embed, indent=1))
    else:
        post({"username": "Price Alerts", "embeds": [embed], "allowed_mentions": {"parse": []}})


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:  # never break the spoiler workflow
        print(f"Price bot error: {ex}")
