#!/usr/bin/env python3
"""
Deals bot: posts new sealed-MTG deals to the #deals forum on Discord.

Tim's daily sealed-deal check (a Claude scheduled task) drops files named
deals-YYYY-MM-DD*.json into a public Google Drive folder. Each file is a JSON
list of deals: [{"id": "...", "title": "...", "body": "...", "tag": "sealed"}].
This script runs with the spoiler bot every 15 minutes, reads the newest files,
and posts each deal it hasn't posted before as its own forum post.

Remembers what it posted in posted_deals.json. Never fails the workflow: any
error is printed and the script exits 0, so spoilers keep working.

Env vars:
  DEALS_DISCORD_WEBHOOK_URL  webhook in the #deals forum
  DRY_RUN=1                  print instead of posting
"""
import json
import os
import re
import time
import urllib.request
from pathlib import Path

FOLDER_ID = "1azCWzaJgKqTp4Rpv1qm0dGC3Z30_OozB"  # "Discord deal queue" in Tim's Drive
STATE_FILE = Path(__file__).with_name("posted_deals.json")
FILES_TO_CHECK = 10   # newest N files in the folder
MAX_POSTS = 10        # per run
TAGS = {  # forum tag IDs in #deals
    "sealed": "1572361864002666497",
    "singles": "1572361892813475982",
    "secret_lair": "1572361916251377806",
    "arena": "1572361945103990885",
}
UA = "DiscordBot (https://github.com/NotTimTurner/mtg-spoiler-bot, 1.0)"
WEBHOOK = os.environ.get("DEALS_DISCORD_WEBHOOK_URL", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def queue_files():
    html = get(f"https://drive.google.com/embeddedfolderview?id={FOLDER_ID}")
    files = re.findall(
        r'id="entry-([A-Za-z0-9_-]+)".*?flip-entry-title">([^<]+)<', html, re.S)
    files = [(name.strip(), fid) for fid, name in files
             if name.strip().startswith("deals-") and name.strip().endswith(".json")]
    return sorted(files)[-FILES_TO_CHECK:]


def chunks(text):
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > 1900:
            out.append(line[:1900]); line = line[1900:]
        if len(cur) + len(line) + 1 > 1900:
            out.append(cur); cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur)
    return out or ["(no details)"]


def post(payload, query=""):
    req = urllib.request.Request(
        WEBHOOK + "?wait=true" + query, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def post_deal(d):
    title = (d.get("title") or "Deal").strip()[:100]
    body = (d.get("body") or title).replace("\\n", "\n").strip()
    tag = TAGS.get((d.get("tag") or "sealed").lower().replace(" ", "_"), TAGS["sealed"])
    if DRY_RUN:
        print(f"[dry run] {title} ({tag})\n{body}\n")
        return
    base = {"username": "Sealed Deals", "allowed_mentions": {"parse": []}}
    parts = chunks(body)
    msg = post({**base, "content": parts[0], "thread_name": title, "applied_tags": [tag]})
    for part in parts[1:]:
        time.sleep(1)
        post({**base, "content": part}, f"&thread_id={msg['channel_id']}")


def main():
    if not WEBHOOK and not DRY_RUN:
        print("DEALS_DISCORD_WEBHOOK_URL not set; skipping deals")
        return
    posted = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else []
    seen = set(posted)
    files = queue_files()
    print(f"Deal queue: {len(files)} file(s)")
    count = 0
    for name, fid in files:
        try:
            deals = json.loads(get(f"https://drive.google.com/uc?export=download&id={fid}"))
        except Exception as e:
            print(f"Couldn't read {name}: {e}")
            continue
        if isinstance(deals, dict):
            deals = [deals]
        for d in deals:
            key = str(d.get("id") or d.get("title") or "").strip()
            if not key or key in seen:
                continue
            if count >= MAX_POSTS:
                print("Hit MAX_POSTS; the rest wait for the next run")
                break
            post_deal(d)
            seen.add(key); posted.append(key); count += 1
            if not DRY_RUN:
                STATE_FILE.write_text(json.dumps(posted[-500:], indent=1) + "\n")
            print("Posted deal:", d.get("title"))
            time.sleep(2)
    if not DRY_RUN:
        STATE_FILE.write_text(json.dumps(posted[-500:], indent=1) + "\n")
    print(f"{count} new deal(s) posted")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the spoiler workflow
        print(f"Deals bot error: {e}")
