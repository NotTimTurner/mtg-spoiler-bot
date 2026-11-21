#!/usr/bin/env python3
"""
Hub bot: Tim's personal Discord posts that need no Claude.

1. #today (Tim HQ): one morning post at ~6:30 AM ET with Norristown weather,
   today's events from Tim's personal Google calendars (private iCal URLs),
   Pokémon GO events running today, and the 10k-step goal.
2. #shiny-dex: one pinned progress card, edited in place whenever
   missing_shinies.json changes (delete a slug from it when you catch one).

State: hub_state.json. Never fails the workflow.

Env vars:
  TODAY_WEBHOOK_URL   webhook in #today
  TODAY_ICS_URLS      secret iCal URLs, separated by spaces or new lines
  SHINY_WEBHOOK_URL   webhook in #shiny-dex
  DRY_RUN=1           print instead of posting
  FORCE_TODAY=1       post #today now even if already posted / too early
"""
import json
import os
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HERE = Path(__file__).parent
STATE_FILE = HERE / "hub_state.json"
MISSING_FILE = HERE / "missing_shinies.json"
DEX_TOTAL = 1359          # HOME living dex entries tracked in CTA Dex (Nov 14, 2026)
POST_AFTER = dtime(6, 20)  # first run at/after this ET time posts #today (runs at :07/:27/:47)
LAT, LON, PLACE = 40.1215, -75.3399, "Norristown"
POGO_URL = "https://raw.githubusercontent.com/bigfoott/ScrapedDuck/data/events.min.json"
DRY_RUN = os.environ.get("DRY_RUN") == "1"
UA = "DiscordBot (https://github.com/NotTimTurner/mtg-spoiler-bot, 1.0)"

WMO = {0: "☀️ Clear", 1: "🌤️ Mostly clear", 2: "⛅ Partly cloudy", 3: "☁️ Cloudy", 45: "🌫️ Fog", 48: "🌫️ Fog",
       51: "🌦️ Light drizzle", 53: "🌦️ Drizzle", 55: "🌧️ Heavy drizzle", 56: "🌧️ Freezing drizzle",
       57: "🌧️ Freezing drizzle", 61: "🌧️ Light rain", 63: "🌧️ Rain", 65: "🌧️ Heavy rain",
       66: "🌧️ Freezing rain", 67: "🌧️ Freezing rain", 71: "🌨️ Light snow", 73: "🌨️ Snow",
       75: "❄️ Heavy snow", 77: "🌨️ Snow grains", 80: "🌦️ Showers", 81: "🌧️ Showers",
       82: "⛈️ Heavy showers", 85: "🌨️ Snow showers", 86: "❄️ Heavy snow showers",
       95: "⛈️ Thunderstorms", 96: "⛈️ Thunderstorms + hail", 99: "⛈️ Thunderstorms + hail"}


def get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout).read()


def webhook_send(url, payload, edit_id=None):
    """Post (or edit) a webhook message; returns the message id."""
    if DRY_RUN:
        print("WOULD", "EDIT" if edit_id else "POST", json.dumps(payload, ensure_ascii=False)[:1500])
        return edit_id or "dry-run"
    target = f"{url}/messages/{edit_id}" if edit_id else f"{url}?wait=true"
    req = urllib.request.Request(target, data=json.dumps(payload).encode(), method="PATCH" if edit_id else "POST",
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())["id"]


# ---------- #today ----------

def weather_line():
    q = (f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}"
         "&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
         "&temperature_unit=fahrenheit&timezone=America%2FNew_York&forecast_days=1")
    d = json.loads(get(q))["daily"]
    sky = WMO.get(d["weathercode"][0], "🌡️")
    rain = d["precipitation_probability_max"][0]
    return (f"{sky}, high {round(d['temperature_2m_max'][0])}°F / low {round(d['temperature_2m_min'][0])}°F"
            + (f", {rain}% chance of rain" if rain else "") + f" ({PLACE})")


def calendar_lines(today):
    urls = os.environ.get("TODAY_ICS_URLS", "").split()
    if not urls:
        return None
    import icalendar
    import recurring_ical_events
    start = datetime.combine(today, dtime.min, ET)
    end = start + timedelta(days=1)
    rows = []
    for url in urls:
        try:
            cal = icalendar.Calendar.from_ical(get(url))
        except Exception as ex:
            print(f"Today: a calendar failed to load ({type(ex).__name__})")  # never print the secret URL
            continue
        name = str(cal.get("X-WR-CALNAME", "")).strip()
        for ev in recurring_ical_events.of(cal).between(start, end):
            title = str(ev.get("SUMMARY", "(no title)")).strip()
            s = ev.get("DTSTART").dt
            if isinstance(s, datetime):
                s = s.astimezone(ET) if s.tzinfo else s.replace(tzinfo=ET)
                key, when = s.strftime("%H:%M"), s.strftime("%I:%M %p").lstrip("0")
            else:
                key, when = "00:00", "All day"
            rows.append((key, f"**{when}** · {title}" + (f"  _({name})_" if name and len(urls) > 1 else "")))
    rows.sort()
    seen, out = set(), []
    for _, line in rows:  # shared calendars can list the same event twice
        if line not in seen:
            seen.add(line); out.append(line)
    return out


def pogo_lines(today):
    events = json.loads(get(POGO_URL))
    out = []
    for e in events:
        try:
            s = datetime.fromisoformat(e["start"][:19]).date()
            t = datetime.fromisoformat(e["end"][:19]).date()
        except (KeyError, TypeError, ValueError):
            continue
        if s <= today <= t:
            tag = ("today" if s == t else "starts today") if s == today else (
                "last day" if t == today else f"until {t:%b} {t.day}")
            out.append(f"{e.get('name', 'Event')} ({tag})")
    return out[:8]


def post_today(state, now):
    hook = os.environ.get("TODAY_WEBHOOK_URL", "")
    if not hook and not DRY_RUN:
        print("Today: TODAY_WEBHOOK_URL not set; skipping")
        return
    today = now.date()
    force = os.environ.get("FORCE_TODAY") == "1"
    if not force and (state.get("todayPosted") == today.isoformat() or now.time() < POST_AFTER):
        return
    fields = []
    for label, fn in (("Weather", weather_line), ("Calendar", lambda: calendar_lines(today)),
                      ("Pokémon GO", lambda: pogo_lines(today))):
        try:
            val = fn()
        except Exception as ex:
            print(f"Today: {label} failed: {type(ex).__name__}: {str(ex)[:120]}")
            val = "_(couldn't load)_"
        if val is None:
            continue
        if isinstance(val, list):
            val = "\n".join(val) if val else ("Nothing scheduled" if label == "Calendar" else "No events today")
        fields.append({"name": label, "value": val[:1024], "inline": False})
    fields.append({"name": "Steps", "value": "Goal: 10,000 (the pedal exerciser counts)", "inline": False})
    embed = {"title": f"{today:%A, %B} {today.day}", "color": 0xF57C00, "fields": fields,
             "footer": {"text": "Tim HQ · daily"}}
    webhook_send(hook, {"username": "Today", "embeds": [embed], "allowed_mentions": {"parse": []}})
    state["todayPosted"] = today.isoformat()
    print(f"Today: posted ({len(fields)} sections)")


# ---------- #shiny-dex ----------

def post_shiny(state, now):
    hook = os.environ.get("SHINY_WEBHOOK_URL", "")
    if not hook and not DRY_RUN:
        print("Shiny: SHINY_WEBHOOK_URL not set; skipping")
        return
    missing = json.loads(MISSING_FILE.read_text())
    if isinstance(missing, dict):  # {"_note": ..., "missing": [...]}
        missing = missing.get("missing", [])
    if state.get("shinyMissing") == len(missing) and state.get("shinyMsgId") and \
            state.get("shinyDay") == now.date().isoformat():
        return  # nothing changed today
    have = DEX_TOTAL - len(missing)
    pct = have / DEX_TOTAL * 100
    bar = "█" * round(pct / 5) + "░" * (20 - round(pct / 5))
    # a different handful of targets each day
    start = (now.date().toordinal() * 7) % max(len(missing), 1)
    picks = (missing[start:] + missing[:start])[:8]
    nice = lambda s: s.replace("-", " ").title()
    embed = {
        "title": "✨ Shiny living dex",
        "description": f"`{bar}` **{pct:.1f}%**\n**{have:,} / {DEX_TOTAL:,}** shinies · **{len(missing)}** to go",
        "color": 0xFFD54F,
        "fields": [{"name": "Some still missing", "value": "\n".join(f"• {nice(p)}" for p in picks) or "None!"},
                   {"name": "Caught one?", "value": "Delete its name from `missing_shinies.json` in the bot repo; "
                                                    "this card updates on the next run."}],
        "footer": {"text": "From CTA Dex snapshot (Nov 14, 2026) · includes forms and some shiny-locked Pokémon"},
        "timestamp": now.astimezone(ZoneInfo("UTC")).isoformat(),
    }
    payload = {"username": "Shiny Dex", "embeds": [embed], "allowed_mentions": {"parse": []}}
    msg_id = state.get("shinyMsgId")
    try:
        msg_id = webhook_send(hook, payload, edit_id=msg_id)
    except urllib.error.HTTPError as ex:
        if ex.code != 404:
            raise
        msg_id = webhook_send(hook, payload)  # card was deleted: post a new one
    state.update(shinyMsgId=msg_id, shinyMissing=len(missing), shinyDay=now.date().isoformat())
    print(f"Shiny: card updated ({have}/{DEX_TOTAL})")


def main():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    now = datetime.now(ET)
    for part in (post_today, post_shiny):
        try:
            part(state, now)
        except Exception as ex:
            print(f"Hub bot: {part.__name__} error: {type(ex).__name__}: {str(ex)[:200]}")
        time.sleep(1)
    if not DRY_RUN:
        STATE_FILE.write_text(json.dumps(state, indent=1) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:  # never break the spoiler workflow
        print(f"Hub bot error: {ex}")
