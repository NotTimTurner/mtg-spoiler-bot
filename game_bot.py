#!/usr/bin/env python3
"""
Game bot: turns Tim's daily food/workout totals into XP, levels, streaks and a weekly boss,
and posts the Sunday meal plan.

Input: a private Google Calendar ("Tim HQ Bot", secret iCal URL in GAME_ICS_URL) used as a mailbox.
  * All-day event titled STATS on a day, description = JSON, written by the daily food-logging chat:
      {"cal": 1840, "budget": 1848, "protein": 142, "sat_fat": 13, "sugar": 48, "fiber": 31,
       "chol": 180, "water": 100, "steps": 9200, "workout": "A", "pedal_min": 25}
    Any field can be left out; missing fields just aren't scored.
  * All-day event titled MEALPLAN on a Sunday, description = the week's plan (Markdown), written by
    the Sunday meal-plan scheduled task. Posted once to #meal-plan.

Output (Discord webhooks):
  GAME_WEBHOOK_URL      #workouts: a scoreboard card edited in place + one result post per logged day
                        + a weekly recap Sunday evening
  MEALPLAN_WEBHOOK_URL  #meal-plan
State: game_state.json (also read by hub_bot.py for the #today "Quest" line). Never fails the workflow.
DRY_RUN=1 prints instead of posting.
"""
import json
import os
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HERE = Path(__file__).parent
STATE_FILE = HERE / "game_state.json"
DRY_RUN = os.environ.get("DRY_RUN") == "1"
UA = "DiscordBot (https://github.com/NotTimTurner/mtg-spoiler-bot, 1.0)"

# ---- goals (Lose It goals, Oct 30 2026) ----
GOALS = {"protein": 140, "sat_fat": 15, "sugar": 60, "fiber": 30, "chol": 200, "water": 100, "steps": 8000}

# ---- workout plan: 3 lifts after work (Mon/Wed/Fri) + daily pedal ----
LIFT_DAYS = {0: "A", 2: "B", 4: "C"}  # weekday -> workout
WORKOUTS = {
    "A": ("Push (bench day)", ["Bench press 4×6–8", "Barbell overhead press 3×8", "DB flyes 3×12",
                               "DB overhead triceps extension 3×12", "Plank 3×30 s"]),
    "B": ("Legs", ["DB goblet squat 4×10", "Barbell Romanian deadlift 3×8", "DB reverse lunge 3×10/leg",
                   "DB calf raise 3×15", "Dead bug 3×10/side"]),
    "C": ("Pull", ["Barbell bent-over row 4×8", "One-arm DB row 3×10/side", "Barbell deadlift 3×5 (moderate)",
                   "DB rear-delt fly 3×12", "DB curl 3×12"]),
}
PEDAL_GOAL = 20  # minutes, every day

# ---- XP table ----
LEVELS = [(0, "SD"), (300, "HG"), (900, "RG"), (2000, "MG"), (4000, "PG"), (7000, "PG Unleashed")]
BOSSES = {  # key: (name, emoji, stat checker, hits needed per week)
    "sat_fat": ("Sat Fat Dragon", "🐉", "sat fat under 15 g", 5),
    "sugar": ("Sugar Slime", "🟢", "sugar under 60 g", 5),
    "protein": ("Protein Golem", "🗿", "protein 140 g+", 5),
    "lift": ("Iron Titan", "🏋️", "all 3 lift days done", 3),
    "cal": ("Snack Goblin", "👺", "at or under your calorie budget", 5),
}


def hits(s):
    """Per-goal pass/fail for one day's stats (None = not reported)."""
    g = lambda k: s.get(k)
    return {
        "cal": None if g("cal") is None or g("budget") is None else g("cal") <= g("budget"),
        "protein": None if g("protein") is None else g("protein") >= GOALS["protein"],
        "sat_fat": None if g("sat_fat") is None else g("sat_fat") < GOALS["sat_fat"],
        "sugar": None if g("sugar") is None else g("sugar") < GOALS["sugar"],
        "fiber": None if g("fiber") is None else g("fiber") >= GOALS["fiber"],
        "chol": None if g("chol") is None else g("chol") < GOALS["chol"],
        "water": None if g("water") is None else g("water") >= GOALS["water"],
        "steps": None if g("steps") is None else g("steps") >= GOALS["steps"],
        "lift": bool(s.get("workout")),
        "pedal": None if g("pedal_min") is None else g("pedal_min") >= PEDAL_GOAL,
    }


def score(day, s):
    h = hits(s)
    xp, lines = 10, ["+10 logged"]
    table = [("cal", 20, "calories ≤ budget"), ("protein", 25, "protein 140 g+"), ("sat_fat", 20, "sat fat < 15 g"),
             ("sugar", 10, "sugar < 60 g"), ("fiber", 10, "fiber 30 g+"), ("chol", 10, "cholesterol < 200 mg"),
             ("water", 10, "water 100 oz+"), ("steps", 15, "8k steps"), ("pedal", 10, f"pedal {PEDAL_GOAL}+ min")]
    for k, pts, label in table:
        if h[k]:
            xp += pts; lines.append(f"+{pts} {label}")
    if s.get("cal") is not None and s.get("budget") is not None and not h["cal"] and s["cal"] - s["budget"] <= 100:
        xp += 5; lines.append("+5 within 100 of budget")
    if (s.get("protein") or 0) >= 120 and not h["protein"]:
        xp += 10; lines.append("+10 protein 120 g+")
    if (s.get("steps") or 0) >= 10000:
        xp += 10; lines.append("+10 10k steps bonus")
    if s.get("workout"):
        pts = 40 if day.weekday() in LIFT_DAYS else 20
        xp += pts; lines.append(f"+{pts} workout" + ("" if day.weekday() in LIFT_DAYS else " (bonus day)"))
    if all(h[k] for k in ("cal", "protein", "sat_fat", "sugar")):
        xp += 25; lines.append("+25 PERFECT DAY")
    return xp, h, lines


def level_of(xp):
    cur = LEVELS[0]
    for lv in LEVELS:
        if xp >= lv[0]:
            cur = lv
    i = LEVELS.index(cur)
    nxt = LEVELS[i + 1] if i + 1 < len(LEVELS) else None
    return cur, nxt


def bar(frac, n=16):
    k = max(0, min(n, round(frac * n)))
    return "█" * k + "░" * (n - k)


def week_start(d):
    return d - timedelta(days=d.weekday())  # Monday


def pick_boss(state, monday):
    """This week's boss = the goal with the worst hit rate last week (rotates if tied)."""
    prev = [d for d in state["days"] if week_start(date.fromisoformat(d)) == monday - timedelta(days=7)]
    if not prev:
        return "sat_fat"
    rates = {}
    for k in BOSSES:
        vals = [state["days"][d]["hits"].get(k) for d in prev]
        vals = [v for v in vals if v is not None]
        rates[k] = (sum(vals) / len(vals)) if vals else 1.0
    return min(rates, key=lambda k: (rates[k], k == state.get("lastBoss")))


def boss_progress(state, monday, key):
    days = [d for d in state["days"] if week_start(date.fromisoformat(d)) == monday]
    got = sum(1 for d in days if state["days"][d]["hits"].get(key))
    return got, BOSSES[key][3]


def streak(state, key, today, lift_only=False):
    n, d = 0, today - timedelta(days=1)
    while True:
        if lift_only and d.weekday() not in LIFT_DAYS:
            d -= timedelta(days=1)
            if (today - d).days > 400:
                break
            continue
        rec = state["days"].get(d.isoformat())
        if not rec or not rec["hits"].get(key):
            break
        n += 1
        d -= timedelta(days=1)
    return n


# ---------- I/O ----------

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=30).read()


def send(url, payload, edit_id=None):
    if DRY_RUN:
        print("WOULD", "EDIT" if edit_id else "POST", json.dumps(payload, ensure_ascii=False)[:1800])
        return edit_id or "dry-run"
    target = f"{url}/messages/{edit_id}" if edit_id else f"{url}?wait=true"
    req = urllib.request.Request(target, data=json.dumps(payload).encode(), method="PATCH" if edit_id else "POST",
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    out = json.loads(urllib.request.urlopen(req, timeout=30).read())
    time.sleep(1.5)
    return out["id"]


def read_mailbox(url, since):
    import icalendar
    import recurring_ical_events
    cal = icalendar.Calendar.from_ical(get(url))
    start = datetime.combine(since, datetime.min.time(), ET)
    stats, plans = {}, {}
    for ev in recurring_ical_events.of(cal).between(start, start + timedelta(days=60)):
        title = str(ev.get("SUMMARY", "")).strip().upper()
        d = ev.get("DTSTART").dt
        d = d.astimezone(ET).date() if isinstance(d, datetime) else d
        body = str(ev.get("DESCRIPTION", "") or "").strip()
        if title.startswith("STATS"):
            try:
                txt = body[body.index("{"): body.rindex("}") + 1]
                stats[d.isoformat()] = json.loads(txt)
            except ValueError as ex:
                print(f"Game: STATS on {d} isn't valid JSON ({ex})")
        elif title.startswith("MEALPLAN"):
            plans[d.isoformat()] = body
    return stats, plans


# ---------- posts ----------

def day_post(day, s, xp, h, lines):
    def mark(k, label, val, unit=""):
        if h.get(k) is None:
            return None
        return f"{'✅' if h[k] else '❌'} {label} {val}{unit}"
    parts = [mark("cal", "calories", f"{s.get('cal')}/{s.get('budget')}"),
             mark("protein", "protein", s.get("protein"), " g"), mark("sat_fat", "sat fat", s.get("sat_fat"), " g"),
             mark("sugar", "sugar", s.get("sugar"), " g"), mark("fiber", "fiber", s.get("fiber"), " g"),
             mark("chol", "cholesterol", s.get("chol"), " mg"), mark("water", "water", s.get("water"), " oz"),
             mark("steps", "steps", f"{s.get('steps'):,}" if s.get("steps") is not None else None),
             ("✅ workout " + WORKOUTS.get(s["workout"], (s["workout"],))[0]) if s.get("workout") else
             ("❌ lift day missed" if day.weekday() in LIFT_DAYS else None),
             mark("pedal", "pedal", s.get("pedal_min"), " min")]
    return {"title": f"📊 {day:%a %b} {day.day}: +{xp} XP",
            "description": "\n".join(p for p in parts if p) + "\n\n" + " · ".join(lines),
            "color": 0x43A047 if xp >= 120 else (0xFBC02D if xp >= 70 else 0xE53935)}


def scoreboard(state, today):
    xp = state["xp"]
    (lo, name), nxt = level_of(xp)
    frac = 1.0 if not nxt else (xp - lo) / (nxt[0] - lo)
    monday = week_start(today)
    bkey = state["boss"]["key"]
    bname, bemoji, bdesc, need = BOSSES[bkey]
    got, need = boss_progress(state, monday, bkey)
    week = []
    for i in range(7):
        d = monday + timedelta(days=i)
        rec = state["days"].get(d.isoformat())
        icon = "⬜" if d > today else ("✅" if rec and rec["hits"].get("lift") else ("❌" if d < today else "⏳"))
        if d.weekday() in LIFT_DAYS:
            week.append(f"{icon} {d:%a} {WORKOUTS[LIFT_DAYS[d.weekday()]][0]}")
    wk = LIFT_DAYS.get(today.weekday())
    todays = (f"**{WORKOUTS[wk][0]}**: " + " · ".join(WORKOUTS[wk][1])) if wk else "Rest day (no lift)"
    fields = [
        {"name": "Level", "value": f"**{name}** · {xp:,} XP\n`{bar(frac)}` "
                                    + (f"{nxt[0] - xp:,} XP to {nxt[1]}" if nxt else "max grade!"), "inline": False},
        {"name": f"{bemoji} Weekly boss: {bname}", "value": f"{bdesc}: **{got}/{need}** days\n`{bar(got / need, 10)}`"
                                                             + ("\n💥 **Defeated!**" if got >= need else ""), "inline": False},
        {"name": "Streaks", "value": f"🥩 protein {streak(state, 'protein', today)} · 🧈 sat fat {streak(state, 'sat_fat', today)}"
                                     f" · 📝 logging {streak(state, 'logged', today)} · 🏋️ lifts {streak(state, 'lift', today, True)}",
         "inline": False},
        {"name": "This week's lifts", "value": "\n".join(week), "inline": False},
        {"name": "Today", "value": todays + f"\n+ pedal {PEDAL_GOAL}+ min", "inline": False},
        {"name": "Rewards", "value": f"🎁 {state.get('rewards', 0)} earned (1 per boss defeated: build a kit from the Gunpla "
                                     f"backlog, or pick your own)", "inline": False},
    ]
    return {"title": "🎮 Tim's quest log", "color": 0xF57C00, "fields": fields,
            "footer": {"text": "Scores come from the daily food-log wrap-up · not medical advice"},
            "timestamp": datetime.now(ZoneInfo("UTC")).isoformat()}


def split_markdown(text, limit=3900):
    chunks, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 > limit:
            chunks.append(cur); cur = ""
        cur += para + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks


def main():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    state.setdefault("days", {}); state.setdefault("xp", 0); state.setdefault("posted", [])
    state.setdefault("plans", []); state.setdefault("rewards", 0)
    now = datetime.now(ET)
    today = now.date()
    url = os.environ.get("GAME_ICS_URL", "").strip()
    game_hook = os.environ.get("GAME_WEBHOOK_URL", "").strip()
    plan_hook = os.environ.get("MEALPLAN_WEBHOOK_URL", "").strip()
    if not url:
        print("Game: GAME_ICS_URL not set; skipping")
        return
    stats, plans = read_mailbox(url, today - timedelta(days=21))

    # new weekly boss on Monday (or first run)
    monday = week_start(today)
    if state.get("boss", {}).get("week") != monday.isoformat():
        old = state.get("boss")
        if old:
            got, need = boss_progress(state, date.fromisoformat(old["week"]), old["key"])
            if got >= need:
                state["rewards"] += 1
            state["lastBoss"] = old["key"]
            state.setdefault("bossLog", []).append({"week": old["week"], "key": old["key"], "won": got >= need})
        state["boss"] = {"week": monday.isoformat(), "key": pick_boss(state, monday)}
        print(f"Game: boss for week of {monday}: {state['boss']['key']}")

    # score new or changed days
    changed = []
    for d, s in sorted(stats.items()):
        sig = json.dumps(s, sort_keys=True)
        old = state["days"].get(d)
        if old and old.get("sig") == sig:
            continue
        xp, h, lines = score(date.fromisoformat(d), s)
        h["logged"] = True
        state["xp"] += xp - (old["xp"] if old else 0)
        state["days"][d] = {"xp": xp, "hits": h, "sig": sig}
        changed.append((d, s, xp, h, lines, bool(old)))
    print(f"Game: {len(stats)} STATS days found, {len(changed)} new/changed, total {state['xp']} XP")

    if game_hook or DRY_RUN:
        for d, s, xp, h, lines, was in changed:
            if d in state["posted"] and was:
                continue  # corrected day: XP updated quietly, no second post
            if (today - date.fromisoformat(d)).days > 7:
                continue  # don't backfill old days into the channel
            send(game_hook, {"username": "Quest Log", "embeds": [day_post(date.fromisoformat(d), s, xp, h, lines)],
                             "allowed_mentions": {"parse": []}})
            state["posted"].append(d)
        state["posted"] = state["posted"][-60:]
        # scoreboard card: edit in place, refresh when something changed or once a day
        if changed or state.get("boardDay") != today.isoformat():
            payload = {"username": "Quest Log", "embeds": [scoreboard(state, today)], "allowed_mentions": {"parse": []}}
            try:
                state["boardId"] = send(game_hook, payload, edit_id=state.get("boardId"))
            except urllib.error.HTTPError as ex:
                if ex.code != 404:
                    raise
                state["boardId"] = send(game_hook, payload)
            state["boardDay"] = today.isoformat()
        # weekly recap: Sunday from 8 PM ET
        if today.weekday() == 6 and now.hour >= 20 and state.get("recapWeek") != monday.isoformat():
            days = [d for d in state["days"] if week_start(date.fromisoformat(d)) == monday]
            wxp = sum(state["days"][d]["xp"] for d in days)
            bkey = state["boss"]["key"]
            got, need = boss_progress(state, monday, bkey)
            best = max(days, key=lambda d: state["days"][d]["xp"]) if days else None
            desc = (f"**{wxp} XP** this week across {len(days)} logged day{'s' if len(days) != 1 else ''}.\n"
                    f"{BOSSES[bkey][1]} {BOSSES[bkey][0]}: {got}/{need} → "
                    + ("**defeated, reward earned!** 🎁" if got >= need else "it got away this week.")
                    + (f"\nBest day: {date.fromisoformat(best):%A} (+{state['days'][best]['xp']} XP)" if best else ""))
            send(game_hook, {"username": "Quest Log", "embeds": [{"title": "🗓️ Weekly recap", "description": desc,
                                                                    "color": 0x8E24AA}], "allowed_mentions": {"parse": []}})
            state["recapWeek"] = monday.isoformat()

    if plan_hook or DRY_RUN:
        for d, body in sorted(plans.items()):
            if d in state["plans"] or not body:
                continue
            chunks = split_markdown(body)
            for i in range(0, len(chunks), 10):
                embeds = [{"description": c, "color": 0x43A047} for c in chunks[i:i + 10]]
                if i == 0:
                    embeds[0]["title"] = f"🍽️ Meal plan, week of {date.fromisoformat(d) + timedelta(days=1):%b} " \
                                         f"{(date.fromisoformat(d) + timedelta(days=1)).day}"
                send(plan_hook, {"username": "Meal Plan", "embeds": embeds, "allowed_mentions": {"parse": []}})
            state["plans"] = (state["plans"] + [d])[-20:]
            print(f"Game: meal plan for {d} posted ({len(chunks)} part(s))")

    # summary for hub_bot's #today
    y = (today - timedelta(days=1)).isoformat()
    wk = LIFT_DAYS.get(today.weekday())
    (_, name), _ = level_of(state["xp"])
    state["todayLine"] = {
        "date": today.isoformat(),
        "level": f"{name} · {state['xp']:,} XP",
        "yesterday": state["days"][y]["xp"] if y in state["days"] else None,
        "workout": (f"{WORKOUTS[wk][0]}: " + " · ".join(WORKOUTS[wk][1])) if wk else "Rest day",
        "boss": f"{BOSSES[state['boss']['key']][1]} {BOSSES[state['boss']['key']][0]} "
                f"{'%d/%d' % boss_progress(state, monday, state['boss']['key'])}",
    }
    if not DRY_RUN:
        STATE_FILE.write_text(json.dumps(state, indent=1) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:  # never break the spoiler workflow
        print(f"Game bot error: {type(ex).__name__}: {str(ex)[:200]}")
