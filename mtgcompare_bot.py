"""MTGCompare sealed price drops -> #deals "Sealed & Secret Lair Finds" thread.

Reads MTGCompare's public US pages twice a day, finds sealed products whose
cheapest in-stock price dropped by MIN_DROP_PCT or more (2+ stores in stock),
and posts each one once to the existing deals thread.

Env: DEALS_DISCORD_WEBHOOK_URL (same webhook deals_bot.py uses).
State: mtgcompare_seen.json (slug|price keys; a further drop posts again).
Set DRY_RUN=1 to print instead of posting.
"""
import html
import json
import os
import re
import sys
import time
import urllib.request
import urllib.robotparser
from datetime import datetime, timezone

BASE = "https://mtgcompare.app"
PAGES = ["/us/price-moves", "/us/hot-100"]  # drops (▼) are read from both
MIN_DROP_PCT = 15.0      # trigger: price fell at least this much
MIN_STORES = 2           # in stock at 2+ stores
MIN_PRICE = 10.0         # skip single packs and other small change
MAX_POSTS = 10           # per run
RUN_HOURS_UTC = {12, 23}  # ~7-8 AM and ~6-7 PM ET; only first 15 min of the hour
SEEN_FILE = "mtgcompare_seen.json"
THREAD_FILE = "deals_thread.txt"
UA = "NotTimTurner-mtg-spoiler-bot (+https://github.com/NotTimTurner/mtg-spoiler-bot)"

LINK_RE = re.compile(r'<a\b[^>]*href="(?:https://mtgcompare\.app)?(/us/p/[^"#?]+)"[^>]*>(.*?)</a>', re.S | re.I)
ALT_RE = re.compile(r'alt="([^"]+)"', re.I)
STOCK_RE = re.compile(r"In stock at (\d+) of (\d+) stores", re.I)
# Hot 100 shows "▼ 29.4 % · $84.99 → $60.00"; Price moves shows "▼ 16 % · $125.49 7 days ago From $105".
# The % sits in its own tag, so after stripping tags there is a space before it.
DROP_RE = re.compile(r"▼\s*([\d.]+)\s*%\s*·\s*\$([\d,.]+)\s*(?:→\s*\$([\d,.]+)|\d+\s+days?\s+ago\s*From\s*\$([\d,.]+))")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def allowed(path):
    rp = urllib.robotparser.RobotFileParser()
    try:
        rp.parse(get(BASE + "/robots.txt").splitlines())
    except Exception:
        return True  # no robots.txt reachable: treat as allowed
    return rp.can_fetch(UA, BASE + path)


def money(s):
    return float(s.replace(",", ""))


def parse(page_html):
    items = {}
    for slug, inner in LINK_RE.findall(page_html):
        alt = ALT_RE.search(inner)
        text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", inner)))
        drop, stock = DROP_RE.search(text), STOCK_RE.search(text)
        if not (drop and stock):
            continue
        title = html.unescape(alt.group(1)) if alt else slug.rsplit("/", 1)[-1].replace("-", " ").title()
        pct, old, new = float(drop.group(1)), money(drop.group(2)), money(drop.group(3) or drop.group(4))
        stores, total = int(stock.group(1)), int(stock.group(2))
        if pct >= MIN_DROP_PCT and stores >= MIN_STORES and new >= MIN_PRICE:
            items[slug] = dict(slug=slug, title=title, pct=pct, old=old, new=new, stores=stores, total=total)
    return items


def post(webhook, thread_id, item):
    url = f"{webhook}?thread_id={thread_id}&wait=true"
    content = (
        f"📉 **{item['title']}**\n"
        f"${item['new']:,.2f} (was ${item['old']:,.2f}, down {item['pct']:.1f}%) · "
        f"in stock at {item['stores']} of {item['total']} stores\n"
        f"<{BASE}{item['slug']}> · via MTGCompare, check the store's page before buying"
    )
    body = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    urllib.request.urlopen(req, timeout=30).read()


def main():
    now = datetime.now(timezone.utc)
    dry = os.environ.get("DRY_RUN") == "1"
    if not dry and not (now.hour in RUN_HOURS_UTC and now.minute < 15) and os.environ.get("FORCE") != "1":
        print("MTGCompare: not a run window, skipping")
        return
    webhook = os.environ.get("DEALS_DISCORD_WEBHOOK_URL")
    if not webhook and not dry:
        print("MTGCompare: no DEALS_DISCORD_WEBHOOK_URL, skipping")
        return
    thread_id = open(THREAD_FILE).read().strip() if os.path.exists(THREAD_FILE) else ""
    if not thread_id and not dry:
        print("MTGCompare: no deals_thread.txt yet, skipping")
        return

    seen = json.load(open(SEEN_FILE)) if os.path.exists(SEEN_FILE) else []
    found = {}
    for path in PAGES:
        if not allowed(path):
            print(f"MTGCompare: robots.txt disallows {path}, skipping it")
            continue
        try:
            page_items = parse(get(BASE + path))
            print(f"MTGCompare: {path} -> {len(page_items)} drops over {MIN_DROP_PCT}%")
            found.update(page_items)
        except Exception as e:
            print(f"MTGCompare: failed to read {path}: {e}")
        time.sleep(2)

    new_items = [i for i in sorted(found.values(), key=lambda i: -i["pct"])
                 if f"{i['slug']}|{i['new']:.2f}" not in seen][:MAX_POSTS]
    for item in new_items:
        if dry:
            print("WOULD POST:", item)
        else:
            post(webhook, thread_id, item)
            time.sleep(1.5)
        seen.append(f"{item['slug']}|{item['new']:.2f}")

    print(f"MTGCompare: posted {len(new_items)}")
    if not dry:
        json.dump(seen[-2000:], open(SEEN_FILE, "w"), indent=0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"MTGCompare error: {e}")
        sys.exit(1)
