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
  FOOD_ICS_URL        secret iCal URL of the Food Calender (dinner tonight / thaw for tomorrow)
  BIRTHDAYS_ICS_URL   secret iCal URL of a birthdays calendar (optional)
  TODAY_COUNTDOWNS    lines "YYYY-MM-DD|label" (one-off) or "MM-DD|label" (every year)
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
GIVEAWAYS_URL = "https://www.gamerpower.com/api/filter?platform=epic-games-store.steam.gog&type=game"
CH_SPOILERS, CH_DEALS, CH_TROPHIES = "1224728498900570242", "1572361786873614387", "1572430946752528424"
POGO_URL = "https://raw.githubusercontent.com/bigfoott/ScrapedDuck/data/events.min.json"
DRY_RUN = os.environ.get("DRY_RUN") == "1"
CAL_FAILS = []
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
            CAL_FAILS.append(type(ex).__name__)
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


def ics_events(url, day, days=1):
    """(sort key, when, title, date) for events on `day` .. day+days-1 in one iCal feed."""
    import icalendar
    import recurring_ical_events
    start = datetime.combine(day, dtime.min, ET)
    cal = icalendar.Calendar.from_ical(get(url))
    out = []
    for ev in recurring_ical_events.of(cal).between(start, start + timedelta(days=days)):
        title = str(ev.get("SUMMARY", "(no title)")).strip()
        s = ev.get("DTSTART").dt
        if isinstance(s, datetime):
            s = s.astimezone(ET) if s.tzinfo else s.replace(tzinfo=ET)
            out.append((s.isoformat(), s.strftime("%I:%M %p").lstrip("0"), title, s.date()))
        else:
            out.append((s.isoformat(), "", title, s))
    return sorted(out)


def dinner_lines(today):
    url = os.environ.get("FOOD_ICS_URL", "").strip()
    if not url:
        return None
    tonight = ics_events(url, today)
    tomorrow = ics_events(url, today + timedelta(days=1))
    lines = []
    for _, when, title, _ in tonight:
        lines.append(f"Tonight: **{title}**" + (f" (planned {when})" if when else "") + " · aim to be done cooking by 5–6 PM")
    if not tonight:
        lines.append("Nothing on the Food Calender tonight")
    for _, _, title, _ in tomorrow:
        lines.append(f"Tomorrow: **{title}** · if it's frozen, move it to the fridge tonight")
    return lines


def birthday_lines(today):
    url = os.environ.get("BIRTHDAYS_ICS_URL", "").strip()
    if not url:
        return []
    out = []
    for _, _, title, d in ics_events(url, today, days=4):
        n = (d - today).days
        out.append(f"🎂 {title}: " + ("**today**" if n == 0 else f"in {n} day{'s' if n > 1 else ''}"))
    return out


def holidays(year):
    nov1 = date(year, 11, 1)
    thanksgiving = nov1 + timedelta(days=(3 - nov1.weekday()) % 7 + 21)
    return [(date(year, 1, 1), "New Year's Day"), (date(year, 2, 14), "Valentine's Day"),
            (date(year, 7, 4), "Independence Day"), (date(year, 10, 31), "Halloween"),
            (thanksgiving, "Thanksgiving"), (date(year, 12, 25), "Christmas")]


def countdown_lines(today, horizon=45, limit=6):
    items = holidays(today.year) + holidays(today.year + 1)
    for line in os.environ.get("TODAY_COUNTDOWNS", "").splitlines():
        if "|" not in line:
            continue
        when, label = (x.strip() for x in line.split("|", 1))
        try:
            if len(when) == 5:  # MM-DD, every year
                for y in (today.year, today.year + 1):
                    items.append((date.fromisoformat(f"{y}-{when}"), label))
            else:
                items.append((date.fromisoformat(when), label))
        except ValueError:
            continue
    soon = sorted((d, l) for d, l in items if 0 <= (d - today).days <= horizon)
    out = []
    for d, label in soon[:limit]:
        n = (d - today).days
        out.append(f"{label}: " + ("**today**" if n == 0 else f"{n} day{'s' if n > 1 else ''} ({d:%b} {d.day})"))
    return out


def shiny_target_lines(today, events):
    data = json.loads(MISSING_FILE.read_text())
    missing = data.get("missing", []) if isinstance(data, dict) else data
    # base names only (forms like "-female" can't be matched from event text)
    names = {m: m.replace("-", " ") for m in missing}
    hits = []
    for e in events:
        blob = (e.get("name", "") + " " + json.dumps(e.get("extraData", ""))).lower().replace("-", " ")
        for slug, name in names.items():
            if len(name) > 3 and f" {name} " in f" {blob} " and slug not in [h[0] for h in hits]:
                hits.append((slug, e.get("name", "event")))
    return [f"✨ **{s.replace('-', ' ').title()}**: missing shiny, featured in {ev}" for s, ev in hits[:6]]


def todays_pogo(today):
    events = json.loads(get(POGO_URL))
    live = []
    for e in events:
        try:
            s = datetime.fromisoformat(e["start"][:19]).date()
            t = datetime.fromisoformat(e["end"][:19]).date()
        except (KeyError, TypeError, ValueError):
            continue
        if s <= today <= t:
            live.append((s, t, e))
    return live


def count_json(name):
    f = HERE / name
    if not f.exists():
        return 0
    d = json.loads(f.read_text())
    if isinstance(d, dict):
        return sum(len(v) for v in d.values() if isinstance(v, list))
    return len(d)


def recap_lines(state):
    counts = {"spoilers": count_json("seen.json"),
              "drops": count_json("mtgcompare_seen.json") + count_json("posted_deals.json"),
              "arena": count_json("arena_seen.json"),
              "news": count_json("rss_seen.json")}
    prev = state.get("counts") or {}
    state["counts"] = counts
    if not prev:
        return ["(counts start tomorrow)"]
    d = {k: max(counts[k] - prev.get(k, 0), 0) for k in counts}
    out = [f"{d['spoilers']} new spoiler{'s' if d['spoilers'] != 1 else ''} in <#{CH_SPOILERS}>",
           f"{d['drops']} deal{'s' if d['drops'] != 1 else ''} / price drop{'s' if d['drops'] != 1 else ''} and "
           f"{d['arena']} Arena deal post{'s' if d['arena'] != 1 else ''} in <#{CH_DEALS}>"]
    return out


def gaming_lines(today):
    out = []
    t = HERE / "trophies_state.json"
    if t.exists():
        st = json.loads(t.read_text())
        since = datetime.now(ZoneInfo("UTC")) - timedelta(days=1)
        recent = [w for w in st.get("week", []) if datetime.fromisoformat(w["at"].replace("Z", "+00:00")) >= since]
        lt = st.get("lastTrophy") or {}
        if lt:
            out.append(f"🏆 Last trophy: **{lt.get('name', '?')}** ({lt.get('game', '?')}, {lt.get('type', '?')})")
        if recent:
            out.append(f"{len(recent)} trophy{'s' if len(recent) != 1 else ''} in the last 24 h · card in <#{CH_TROPHIES}>")
    try:
        games = json.loads(get(GIVEAWAYS_URL))
        ending = []
        for g in games if isinstance(games, list) else []:
            end = g.get("end_date") or ""
            if end[:10] == today.isoformat():
                ending.append(g["title"].replace(" Giveaway", "").replace(" Key", ""))
        if ending:
            out.append("🎁 Free game ending **today**: " + ", ".join(ending[:4]))
    except Exception as ex:
        print(f"Today: free games failed: {type(ex).__name__}")
    return out


def quest_lines(today):
    f = HERE / "game_state.json"
    g = json.loads(f.read_text()).get("todayLine") if f.exists() else None
    out = []
    if g and g.get("date") == today.isoformat():
        y = g.get("yesterday")
        out.append(f"🎮 {g['level']}" + (f" · yesterday **+{y} XP**" if y is not None else " · yesterday not logged"))
        out.append(f"🏋️ {g['workout']}")
        out.append(f"{g['boss']} this week")
    out.append("🚶 Steps 8k (10k = bonus XP) · pedal 20+ min")
    return out

def health_lines(problems):
    out = list(problems)
    t = HERE / "trophies_state.json"
    if t.exists() and json.loads(t.read_text()).get("authWarned"):
        out.append("PSN sign-in expired: refresh the `PSN_NPSSO` secret (steps in mtg-spoiler-bot.md)")
    return [f"⚠️ {x}" for x in out]


def post_today(state, now):
    hook = os.environ.get("TODAY_WEBHOOK_URL", "")
    if not hook and not DRY_RUN:
        print("Today: TODAY_WEBHOOK_URL not set; skipping")
        return
    today = now.date()
    force = os.environ.get("FORCE_TODAY") == "1"
    if not force and (state.get("todayPosted") == today.isoformat() or now.time() < POST_AFTER):
        return
    fields, problems = [], []

    def add(label, fn):
        try:
            val = fn()
        except Exception as ex:
            print(f"Today: {label} failed: {type(ex).__name__}: {str(ex)[:120]}")
            problems.append(f"{label} couldn't load")
            return
        if val is None:
            return
        if isinstance(val, list):
            if not val:
                return
            val = "\n".join(val)
        fields.append({"name": label, "value": val[:1024], "inline": False})

    add("Weather", weather_line)
    add("Calendar", lambda: calendar_lines(today) or ["Nothing scheduled"])
    add("Dinner", lambda: dinner_lines(today))
    add("Coming up", lambda: birthday_lines(today) + countdown_lines(today))
    pogo = []
    try:
        pogo = todays_pogo(today)
    except Exception as ex:
        print(f"Today: Pokémon GO failed: {type(ex).__name__}")
        problems.append("Pokémon GO events couldn't load")

    def pogo_text():
        out = []
        for s_, t_, e in pogo[:8]:
            tag = ("today" if s_ == t_ else "starts today") if s_ == today else (
                "last day" if t_ == today else f"until {t_:%b} {t_.day}")
            out.append(f"{e.get('name', 'Event')} ({tag})")
        return out or ["No events today"]
    add("Pokémon GO", pogo_text)
    add("Shiny targets", lambda: shiny_target_lines(today, [e for _, _, e in pogo]))
    add("Overnight", lambda: recap_lines(state))
    add("Gaming", lambda: gaming_lines(today))
    add("Quest", lambda: quest_lines(today))
    if CAL_FAILS:
        problems.append(f"{len(CAL_FAILS)} calendar(s) failed to load (was a secret address reset?)")
    add("Needs you", lambda: health_lines(problems))
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
    if "counts" not in state:  # baseline so the first Overnight recap has numbers
        try:
            recap_lines(state)
        except Exception:
            pass
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
