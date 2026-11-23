#!/usr/bin/env python3
"""
Game bot ("Quest Log"): Tim's real-life GTA-style game.
Daily missions (food goals, steps, lifts, meal prep) pay or cost REAL dollars in a balance Tim can
spend on Magic. Tim moves all real money himself; the bot only keeps score. Also: XP + Gunpla levels,
a weekly boss (XP only), the workout guide card, the Sunday meal plan and schedule reminders.

Input: a private Google Calendar ("Tim HQ Bot", secret iCal URL in GAME_ICS_URL) used as a mailbox.
  * STATS (all-day, JSON description) written by the daily food-logging chat:
      {"cal": 1840, "budget": 1848, "protein": 142, "sat_fat": 13, "sugar": 48, "steps": 9200,
       "workout": "A", "pedal_min": 25, "weight": 231.4, "meal_prep": true, "cook_extra": true, "activity": true}
    Missing money fields count as a miss.
  * SPEND / SAVED (all-day, {"amount": N, "note": "..."}): Tim spent balance on Magic / moved money to savings.
  * MEALPLAN (Sunday, Markdown) from the Sunday meal-plan task -> #meal-plan.

Output (Discord webhooks): GAME_WEBHOOK_URL (#workouts: mission board edited in place, day payouts,
weekly payday, reminders), MEALPLAN_WEBHOOK_URL, GUIDES_WEBHOOK_URL.
State: game_state.json (hub_bot.py reads todayLine). Never fails the workflow. DRY_RUN=1 prints only.
"""
import json
import os
import time
import urllib.error
import urllib.parse
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
GOALS = {"protein": 140, "sat_fat": 15, "carbs": 180, "sugar": 60, "fiber": 30, "chol": 200, "water": 100, "steps": 10000}

# ---- workout plan: 3 lifts after work (Mon/Wed/Fri); pedal only to catch up on steps ----
LIFT_DAYS = {0: "A", 2: "B", 4: "C"}  # weekday -> workout
WORKOUTS = {
    "A": ("Push (bench day)", ["Bench press 4×6–8", "DB lateral raise 3×12", "DB flyes 3×12",
                               "DB lying triceps extension 3×12", "Plank 3×30 s"]),
    "B": ("Legs", ["DB goblet squat 4×10", "Barbell Romanian deadlift 3×8", "DB reverse lunge 3×10/leg",
                   "DB calf raise 3×15", "Dead bug 3×10/side"]),
    "C": ("Pull", ["Barbell bent-over row 4×8", "One-arm DB row 3×10/side", "Barbell deadlift 3×5 (moderate)",
                   "DB rear-delt fly 3×12", "DB curl 3×12"]),
}
PEDAL_GOAL = 10  # minutes for the pedal XP bonus
STEP_TARGET = 10000  # Tim's daily step target; short of it at 5 PM -> pedal to catch up
PEDAL_STEPS_PER_MIN = 100  # rough steps Fitbit credits per pedal minute (tune if it reads differently)


def pedal_minutes(steps):
    """Minutes of pedaling to reach STEP_TARGET from `steps` (0 = not needed), rounded up to 5, 10..60."""
    if steps is None or steps >= STEP_TARGET:
        return 0
    m = -(-(STEP_TARGET - steps) // PEDAL_STEPS_PER_MIN)
    return min(60, max(10, -(-m // 5) * 5))
DONE_KEYS = ("workout", "cook_extra", "activity", "meal_prep", "pedal_min")  # what the Day page can mark
GUIDES_CHANNEL = "1573751266402635786"  # #workout-guides (Tim + Daisy)


def split_move(move):
    """'Plank 3×30 s' -> ('Plank', '3×30 s')."""
    words = move.split(" ")
    for i, w in enumerate(words):
        if w[:1].isdigit():
            return " ".join(words[:i]), " ".join(words[i:])
    return move, ""


def video(exercise):
    """YouTube search link for a form tutorial (search links never go stale)."""
    name = split_move(exercise)[0].replace("DB ", "dumbbell ")
    return "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(f"how to {name} proper form")


# ---- XP table ----
LEVELS = [(0, "SD"), (300, "HG"), (900, "RG"), (2000, "MG"), (4000, "PG"), (7000, "PG Unleashed")]
BOSSES = {  # key: (name, emoji, stat checker, hits needed per week)
    "sat_fat": ("Sat Fat Dragon", "🐉", "sat fat under 15 g", 5),
    "sugar": ("Sugar Slime", "🟢", "sugar under 60 g", 5), "carbs": ("Carb Kraken", "🦑", "carbs under 180 g", 5), "fiber": ("Fiber Hydra", "🌿", "fiber 30 g+", 5),
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
        "carbs": None if g("carbs") is None else g("carbs") < GOALS["carbs"], "sugar": None if g("sugar") is None else g("sugar") < GOALS["sugar"],
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
             ("carbs", 10, "carbs < 180 g"), ("fiber", 10, "fiber 30 g+"),
             ("water", 10, "water 100 oz+"), ("steps", 15, "10k steps"), ("pedal", 10, "pedal session")]
    for k, pts, label in table:
        if h[k]:
            xp += pts; lines.append(f"+{pts} {label}")
    if s.get("cal") is not None and s.get("budget") is not None and not h["cal"] and s["cal"] - s["budget"] <= 100:
        xp += 5; lines.append("+5 within 100 of budget")
    if (s.get("protein") or 0) >= 120 and not h["protein"]:
        xp += 10; lines.append("+10 protein 120 g+")
    if (s.get("steps") or 0) >= 12500:
        xp += 10; lines.append("+10 12.5k steps bonus")
    if s.get("workout"):
        pts = 40 if day.weekday() in LIFT_DAYS else 20
        xp += pts; lines.append(f"+{pts} workout" + ("" if day.weekday() in LIFT_DAYS else " (bonus day)"))
    if all(h[k] for k in ("cal", "protein", "sat_fat", "carbs")):
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
    for k in [b for b in BOSSES if b != "sugar"]:  # sugar retired Nov 22 (carbs + fiber now)
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
    stats, plans, moves, flags = {}, {}, [], {}
    for ev in recurring_ical_events.of(cal).between(start, datetime.now(ET) + timedelta(days=2)):
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
        elif title.startswith("DONE"):
            try:
                j = json.loads(body[body.index("{"): body.rindex("}") + 1])
                flags.setdefault(d.isoformat(), {}).update({k: v for k, v in j.items() if k in DONE_KEYS})
            except (ValueError, TypeError, AttributeError) as ex:
                print(f"Game: DONE on {d} isn't valid ({ex})")
        elif title.startswith("SPEND") or title.startswith("SAVED"):
            try:
                j = json.loads(body[body.index("{"): body.rindex("}") + 1])
                moves.append({"id": f"{title[:5]}|{d}|{str(ev.get('UID', ''))}", "kind": title[:5].lower(),
                              "date": d.isoformat(), "amount": float(j["amount"]), "note": str(j.get("note", ""))})
            except (ValueError, KeyError, TypeError) as ex:
                print(f"Game: {title[:5]} on {d} isn't valid ({ex})")
    return stats, plans, moves, flags


# ---------- Fitbit via Google Health API (read-only; secrets live in GitHub Actions) ----------
LIFT_TYPES = ("WEIGHT", "STRENGTH", "CIRCUIT", "CROSSFIT", "CALISTHENICS", "BOOTCAMP", "TRAINING")
WALK_TYPES = ("WALK", "HIK", "RUN", "BIK", "CYCL", "SPORT", "GOLF", "SWIM", "DANC", "TENNIS")


def health_token():
    cid, sec, ref = (os.environ.get(k, "").strip() for k in ("HEALTH_CLIENT_ID", "HEALTH_CLIENT_SECRET", "HEALTH_REFRESH_TOKEN"))
    if not (cid and sec and ref):
        print("Game: Fitbit not configured (missing: " + ", ".join(n for n, v in (("HEALTH_CLIENT_ID", cid), ("HEALTH_CLIENT_SECRET", sec), ("HEALTH_REFRESH_TOKEN", ref)) if not v) + ")")
        return None
    data = urllib.parse.urlencode({"client_id": cid, "client_secret": sec, "refresh_token": ref,
                                   "grant_type": "refresh_token"}).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data, headers={"User-Agent": UA})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())["access_token"]


def health_api(tok, path, body=None):
    req = urllib.request.Request("https://health.googleapis.com/v4/users/me/" + path,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET",
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json",
                                          "User-Agent": UA})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def civil(d):
    return {"date": {"year": d.year, "month": d.month, "day": d.day}}


def rollup(tok, kind, start, end):
    out = {}
    for p in health_api(tok, f"dataTypes/{kind}/dataPoints:dailyRollUp",
                        {"range": {"start": civil(start), "end": civil(end)}, "windowSizeDays": 1}).get("rollupDataPoints", []):
        dd = p["civilStartTime"]["date"]
        out[date(dd["year"], dd["month"], dd["day"]).isoformat()] = p
    return out


def read_fitbit(since, today):
    """{date: {"steps", "weight", "active_min", "exercises": [(type, minutes)]}}; {} when not configured."""
    tok = health_token()
    if not tok:
        return {}
    fit = {}
    end = today + timedelta(days=1)
    start = max(since, end - timedelta(days=89))
    for k, p in rollup(tok, "steps", start, end).items():
        fit.setdefault(k, {})["steps"] = int(p.get("steps", {}).get("countSum", 0))
    for k, p in rollup(tok, "weight", start, end).items():
        g = p.get("weight", {}).get("weightGramsAvg")
        if g:
            fit.setdefault(k, {})["weight"] = round(g / 453.59237, 1)
    for k, p in rollup(tok, "active-minutes", max(start, end - timedelta(days=13)), end).items():
        lv = {x.get("activityLevel"): int(x.get("activeMinutesSum", 0))
              for x in p.get("activeMinutes", {}).get("activeMinutesRollupByActivityLevel", [])}
        fit.setdefault(k, {})["active_min"] = lv.get("MODERATE", 0) + lv.get("VIGOROUS", 0)
    flt = urllib.parse.quote(f'exercise.interval.civil_start_time >= "{start.isoformat()}T00:00:00"')
    page, token = 0, ""
    while page < 10:
        res = health_api(tok, f"dataTypes/exercise/dataPoints?filter={flt}" + (f"&pageToken={token}" if token else ""))
        for p in res.get("dataPoints", []):
            iv = p.get("exercise", {}).get("interval", {})
            try:
                st = datetime.fromisoformat(iv["startTime"].replace("Z", "+00:00")).astimezone(ET)
                en = datetime.fromisoformat(iv["endTime"].replace("Z", "+00:00")).astimezone(ET)
            except (KeyError, ValueError):
                continue
            mins = round((en - st).total_seconds() / 60)
            fit.setdefault(st.date().isoformat(), {}).setdefault("exercises", []).append(
                (p["exercise"].get("exerciseType", "OTHER"), mins))
        token = res.get("nextPageToken") or ""
        page += 1
        if not token:
            break
    try:  # TEST ONLY (Nov 21): does Lose It food reach the Health API? Read and print, nothing else
        nut = rollup(tok, "nutrition-log", today - timedelta(days=1), end)
        for k in sorted(nut):
            print(f"Nutrition test {k}: " + json.dumps({a: b for a, b in nut[k].items() if not a.startswith("civil")}, sort_keys=True)[:1500])
        if not nut:
            print("Nutrition test: no data")
    except urllib.error.HTTPError as ex:
        print(f"Nutrition test failed: {ex.code} " + " ".join(ex.read().decode("utf-8", "replace").split())[:300])
    except Exception as ex:
        print(f"Nutrition test failed: {type(ex).__name__}: {str(ex)[:200]}")
    try:  # TEST ONLY (Nov 22): can the bot read Fitbit sleep? Read and print, nothing else
        sf = urllib.parse.quote(f'sleep.interval.civil_end_time >= "{(today - timedelta(days=3)).isoformat()}"')
        sl = health_api(tok, f"dataTypes/sleep/dataPoints?filter={sf}").get("dataPoints", [])
        for p in sl:
            z = p.get("sleep", {})
            print("Sleep test: " + json.dumps({"interval": z.get("interval"), "type": z.get("type"), "metadata": z.get("metadata"), "summary": {a: b for a, b in z.get("summary", {}).items() if a != "stagesSummary"}, "keys": sorted(z)}, sort_keys=True)[:900])
        if not sl:
            print("Sleep test: no data")
    except urllib.error.HTTPError as ex:
        print(f"Sleep test failed: {ex.code} " + " ".join(ex.read().decode("utf-8", "replace").split())[:300])
    except Exception as ex:
        print(f"Sleep test failed: {type(ex).__name__}: {str(ex)[:200]}")
    print(f"Game: Fitbit {start}..{today}: {sum('steps' in v for v in fit.values())} step days, "
          f"{sum('weight' in v for v in fit.values())} weigh-ins, {sum(len(v.get('exercises', [])) for v in fit.values())} workouts")
    return fit


def fit_lift(f):
    return any(any(t in typ for t in LIFT_TYPES) and m >= 15 for typ, m in (f or {}).get("exercises", []))


def fit_walk(f):
    return any(any(t in typ for t in WALK_TYPES) and m >= 30 for typ, m in (f or {}).get("exercises", []))


# ---------- posts ----------
def day_post(day, s, xp, h, lines):
    def mark(k, label, val, unit=""):
        if h.get(k) is None:
            return None
        return f"{'✅' if h[k] else '❌'} {label} {val}{unit}"
    parts = [mark("cal", "calories", f"{s.get('cal')}/{s.get('budget')}"),
             mark("protein", "protein", s.get("protein"), " g"), mark("sat_fat", "sat fat", s.get("sat_fat"), " g"),
             mark("carbs", "carbs", s.get("carbs"), " g"), mark("fiber", "fiber", s.get("fiber"), " g"),
             mark("water", "water", s.get("water"), " oz"),
             mark("steps", "steps", f"{s.get('steps'):,}" if s.get("steps") is not None else None),
             ("✅ workout " + WORKOUTS.get(s["workout"], (s["workout"],))[0]) if s.get("workout") else
             ("❌ lift day missed" if day.weekday() in LIFT_DAYS else None),
             mark("pedal", "pedal", s.get("pedal_min"), " min")]
    return {"title": f"📊 {day:%a %b} {day.day}: +{xp} XP",
            "description": "\n".join(p for p in parts if p) + "\n\n" + " · ".join(lines),
            "color": 0x43A047 if xp >= 120 else (0xFBC02D if xp >= 70 else 0xE53935)}


def guide_payload():
    days = {"A": "Monday", "B": "Wednesday", "C": "Friday"}
    embeds = []
    for key, (title, moves) in WORKOUTS.items():
        lines = []
        for m in moves:
            name, sets = split_move(m)
            lines.append(f"**{name}** {sets} · [▶ video]({video(m)})")
        embeds.append({"title": f"{days[key]} · {title}", "description": "\n".join(lines), "color": 0xF57C00})
    embeds.append({"title": "Every day · steps, pedal + how-to", "color": 0x43A047, "description": "\n".join([
        f"**Pedal exerciser** only on days you're under {STEP_TARGET:,} steps at 5 PM; the 5 PM reminder says how long",
        "**Warm up** 5 min easy pedal + 1 light set of the first lift",
        "**Rest** 60–90 s between sets",
        "**Weight** pick one where the last 2 reps feel hard but your form stays clean; go up a little when every set feels easy",
        "**Each of you** uses your own weights; do the sets together, taking turns",
        "Videos are YouTube searches for a form tutorial, so pick one from a trusted trainer. Stop if anything hurts; this isn't medical advice.",
    ])})
    return {"username": "Workout Guides", "embeds": embeds, "allowed_mentions": {"parse": []}}


def split_markdown(text, limit=3900):
    chunks, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 > limit:
            chunks.append(cur); cur = ""
        cur += para + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks



# ---- real-money missions (1 credit = $1; Tim moves real money himself) ----
GAME_START = "2026-11-23"   # XP counts from here
MONEY_START = "2026-11-23"  # dollars count from here (Tim, Nov 22: real money from the first day)
PENALTY_START = "2026-11-30"  # misses cost $0 before this (grace week Nov 23-29; wins still pay)
DAILY = [  # key, label, pay, miss
    ("log", "log food", 1, 1),
    ("cal", "calories ≤ budget", 1, 1),
    ("protein", "protein 140 g+", 1, 1),
    ("sat_fat", "sat fat < 15 g", 1, 1),
    ("steps", "10k steps", 1, 1),
]
LIFT_PAY = (3, 3)
COOK_EXTRA_PAY = (1, 1)      # Wednesday: cook extra so Daisy has Thursday lunch
SAT_ACTIVITY_PAY = 3         # Saturday long walk / activity (or 12k+ steps); no penalty
SAT_STEPS = 12000
MEAL_PREP_PAY = (3, 3)       # Sunday: Mon/Tue lunches (Daisy + Tim)
WEIGHT_PAY = (4, 2)          # weekly average weight down / up vs last week
PERFECT_WEEK = 5
LEVEL_REWARD = {"HG": 10, "RG": 15, "MG": 25, "PG": 40, "PG Unleashed": 75}
BUST_MULT = 2                # negative at week close -> move 2x the debt to savings by Wednesday
BUST_CAP = 100               # most a single bust can cost
FORFEIT_AFTER = 3            # this many busted weeks in a row cancels the next level-up reward

# reminders: (weekdays, "HH:MM" ET, text). Tim works from home 9-5 M-F, lunch hour at noon.
REMINDERS = [
    ((0, 1, 2, 3, 4), "12:00", "🍽️ Lunch hour: log breakfast + lunch in Lose It, aim for 40 g protein, take a 10-min walk."),
    ((0,), "17:00", "🏋️ Work's done. **Lift A: Push** when Daisy's home (~5:30). +$3 / −$3"),
    ((1,), "17:00", "🚶 Work's done. Daisy's home ~7:30: take a walk for steps."),
    ((2,), "17:00", "🏋️ Work's done. **Lift B: Legs** tonight (+$3 / −$3), and **cook extra** so Daisy has Thursday lunch (+$1 / −$1)."),
    ((3,), "17:00", "🚶 Work's done. Daisy's home ~6:30: take a walk for steps."),
    ((4,), "17:00", "🏋️ Work's done. **Lift C: Pull** (Daisy's home from ~4). +$3 / −$3"),
    ((5,), "09:00", "🌳 Fun day: long walk or an activity with Daisy (+$3), then lunch or dinner out. Log it in Lose It."),
    ((6,), "10:00", "🥡 Meal prep day: Mon/Tue lunches for you and Daisy. +$3 / −$3"),
    ((5, 6), "17:00", ""),  # weekend: just the 5 PM step check
    ((0, 1, 2, 3, 4, 5, 6), "21:00", "📝 Log the rest of today in Lose It before bed (logging is +$1, and no log costs every food mission)."),
]


def pedal_line(steps):
    if steps is None:
        return f"🚲 Couldn't read your Fitbit steps. If you're under {STEP_TARGET:,}, pedal to catch up (about {PEDAL_STEPS_PER_MIN} steps a minute)."
    m = pedal_minutes(steps)
    if not m:
        return f"✅ {steps:,} steps: {STEP_TARGET // 1000}k done, no pedal needed today."
    return (f"🚲 {steps:,} steps so far ({STEP_TARGET - steps:,} short of {STEP_TARGET // 1000}k): "
            f"pedal about **{m} min** tonight, or walk it off.")


def is_busted_on(state, d):
    for b in state.get("busts", []):
        if b["from"] <= d and (not b.get("paid") or d < b["paid"]):
            return True
    return False


def day_missions(day, s, doubled=False, f=None):
    """[(label, ok, amount)] for one finished day. s=None means nothing logged (every food mission missed).
    f = that day's Fitbit data (steps and workouts are checked against it when present)."""
    h = hits(s) if s else {}
    mult = BUST_MULT if doubled else 1
    out = []
    steps = f["steps"] if f and "steps" in f else (s or {}).get("steps")
    fl = (f or {}).get("flags", {})

    def m(label, ok, pay, miss):
        out.append((label, ok, pay if ok else (-miss * mult if day.isoformat() >= PENALTY_START else 0)))
    for key, label, pay, miss in DAILY:
        if key == "steps":
            m(label + (" ⌚" if f and "steps" in f else ""), (steps or 0) >= GOALS["steps"], pay, miss)
        else:
            m(label, s is not None if key == "log" else h.get(key) is True, pay, miss)
    wd = day.weekday()
    if wd in LIFT_DAYS:
        ok = bool((s or {}).get("workout") or fl.get("workout")) or fit_lift(f)
        m(f"lift {LIFT_DAYS[wd]} ({WORKOUTS[LIFT_DAYS[wd]][0].split(' (')[0]})" + (" ⌚" if fit_lift(f) else ""), ok, *LIFT_PAY)
    if wd == 2:
        m("cook extra for Thursday", bool((s or {}).get("cook_extra") or fl.get("cook_extra")), *COOK_EXTRA_PAY)
    if wd == 5:
        ok = bool((s or {}).get("activity") or fl.get("activity")) or (steps or 0) >= SAT_STEPS or fit_walk(f)
        out.append(("Saturday walk/activity" + (" ⌚" if fit_walk(f) else ""), ok, SAT_ACTIVITY_PAY if ok else 0))
    if wd == 6:
        m("meal prep", bool((s or {}).get("meal_prep") or fl.get("meal_prep")), *MEAL_PREP_PAY)
    return out


def balance(state):
    return round(sum(e["amount"] for e in state.get("ledger", {}).values()), 2)


def owed(state):
    return round(sum(b["owed"] - b.get("saved", 0) for b in state.get("busts", []) if not b.get("paid")), 2)


def busted_now(state):
    return any(not b.get("paid") for b in state.get("busts", []))


def usd(x):
    return f"${x:g}" if x >= 0 else f"−${-x:g}"


def fmt(x):
    return ("+" if x > 0 else ("−" if x < 0 else "")) + f"${abs(x):g}"


def settle(state, stats, today, fit=None):
    fit = fit or {}
    """Recompute every finished day's dollars (idempotent), close finished weeks, pay level-ups.
    Returns a list of (kind, payload) messages for anything new or changed."""
    led = state.setdefault("ledger", {})
    msgs = []
    start = date.fromisoformat(MONEY_START)
    # practice dollars for the test week (not counted)
    prac = {}
    d = date.fromisoformat(GAME_START)
    while d < min(start, today):
        k = d.isoformat()
        if k in stats or d <= today - timedelta(days=2):
            prac[k] = sum(a for _, _, a in day_missions(d, stats.get(k), False, fit.get(k)))
        d += timedelta(days=1)
    state["practice"] = prac
    # real days
    d = start
    while d <= today - timedelta(days=1):
        k = d.isoformat()
        if k in stats or d <= today - timedelta(days=2):
            items = day_missions(d, stats.get(k), is_busted_on(state, k), fit.get(k))
            amt = sum(a for _, _, a in items)
            old = led.get("day:" + k)
            if not old or old["amount"] != amt:
                led["day:" + k] = {"amount": amt, "date": k, "note": "missions"}
                msgs.append(("day", (d, items, amt, old["amount"] if old else None)))
        d += timedelta(days=1)
    # week close: Monday payday once Sunday is logged (Tuesday at the latest)
    w = start
    while w + timedelta(days=6) <= today - timedelta(days=1):
        sun = w + timedelta(days=6)
        wk = w.isoformat()
        if "week:" + wk not in led and (sun.isoformat() in stats or sun <= today - timedelta(days=2)):
            lines, amt = [], 0
            days = [(w + timedelta(days=i)).isoformat() for i in range(7)]
            perfect = all(k in stats and all(ok for _, ok, _ in day_missions(date.fromisoformat(k), stats[k], False, fit.get(k)))
                          for k in days)
            if perfect:
                amt += PERFECT_WEEK; lines.append(f"🏆 Perfect week {fmt(PERFECT_WEEK)}")
            wt = lambda ds: [fit[k]["weight"] if "weight" in fit.get(k, {}) else stats[k]["weight"] for k in ds
                            if "weight" in fit.get(k, {}) or (k in stats and isinstance(stats[k].get("weight"), (int, float)))]
            cur, prev = wt(days), wt([(w - timedelta(days=7 - i)).isoformat() for i in range(7)])
            if cur and prev:
                a, b = sum(cur) / len(cur), sum(prev) / len(prev)
                if a < b:
                    amt += WEIGHT_PAY[0]; lines.append(f"⚖️ Weekly average down {b - a:.1f} lb {fmt(WEIGHT_PAY[0])}")
                elif a > b:
                    pen = (WEIGHT_PAY[1] if sun.isoformat() >= PENALTY_START else 0) * (BUST_MULT if busted_now(state) else 1)
                    amt -= pen; lines.append(f"⚖️ Weekly average up {a - b:.1f} lb {fmt(-pen)}")
            led["week:" + wk] = {"amount": amt, "date": sun.isoformat(), "note": "weekly bonuses"}
            week_total = sum(e["amount"] for k2, e in led.items() if k2.startswith("day:") and e["date"] in days) + amt
            bal = balance(state)
            if bal < 0:
                debt = -bal
                led["bust:" + wk] = {"amount": debt, "date": sun.isoformat(), "note": "busted: balance reset to $0"}
                state.setdefault("busts", []).append({"week": wk, "owed": min(BUST_MULT * debt, BUST_CAP), "saved": 0,
                                                      "from": today.isoformat(),
                                                      "due": (w + timedelta(days=9)).isoformat()})
                state["bustStreak"] = state.get("bustStreak", 0) + 1
                lines.append(f"🚨 **BUSTED** at {fmt(-debt)}. Move **${min(BUST_MULT * debt, BUST_CAP):g}** to savings by "
                             f"Wed {(w + timedelta(days=9)):%b} {(w + timedelta(days=9)).day}. Balance reset to $0; "
                             f"until it's paid: no Magic spending and penalties are doubled.")
            else:
                state["bustStreak"] = 0
            msgs.append(("week", (w, week_total, lines)))
        w += timedelta(days=7)
    # level-up rewards (only once money is live)
    if today >= start:
        names = [n for _, n in LEVELS]
        cur = level_of(state["xp"])[0][1]
        for lv in names[1: names.index(cur) + 1]:
            if "level:" + lv in led:
                continue
            if state.get("bustStreak", 0) >= FORFEIT_AFTER:
                led["level:" + lv] = {"amount": 0, "date": today.isoformat(), "note": f"{lv} reward forfeited"}
                msgs.append(("note", f"🎉 Level up: **{lv}**, but the ${LEVEL_REWARD[lv]} reward is forfeited "
                                     f"({FORFEIT_AFTER} busted weeks in a row)."))
                state["bustStreak"] = 0
            else:
                led["level:" + lv] = {"amount": LEVEL_REWARD[lv], "date": today.isoformat(), "note": f"reached {lv}"}
                msgs.append(("note", f"🎉 **Level up: {lv}!** {fmt(LEVEL_REWARD[lv])}"))
    return msgs


def apply_moves(state, moves):
    led = state.setdefault("ledger", {})
    state.setdefault("movesSeen", [])
    msgs = []
    for mv in sorted(moves, key=lambda m: m["date"]):
        key = ("spend:" if mv["kind"] == "spend" else "saved:") + mv["id"]
        if key in state.setdefault("movesSeen", []):
            continue
        state["movesSeen"].append(key)
        if mv["kind"] == "spend":
            warn = ""
            if busted_now(state):
                warn = " ⚠️ spent while BUSTED"
            elif mv["amount"] > balance(state):
                warn = f" ⚠️ that's more than the {fmt(balance(state))} balance"
            led[key] = {"amount": -mv["amount"], "date": mv["date"], "note": mv["note"] or "Magic"}
            msgs.append(f"🛒 Spent ${mv['amount']:g}" + (f" on {mv['note']}" if mv["note"] else "") + warn)
        else:
            left = mv["amount"]
            for b in state.get("busts", []):
                if b.get("paid") or left <= 0:
                    continue
                take = min(left, b["owed"] - b.get("saved", 0))
                b["saved"] = round(b.get("saved", 0) + take, 2)
                left -= take
                if b["saved"] >= b["owed"]:
                    b["paid"] = mv["date"]
            msgs.append(f"🏦 Moved ${mv['amount']:g} to savings" +
                        (" · **bust cleared, you're free** ✅" if not busted_now(state) else f" · still owe ${owed(state):g}"))
    live = {"spend:" + m["id"] for m in moves}
    for k in [k for k in led if k.startswith("spend:") and k not in live]:  # spend removed on the Day page
        msgs.append(f"Removed a ${-led[k]['amount']:g} spend from {led[k]['date']} (undone on the Day page)")
        led.pop(k)
        if k in state["movesSeen"]:
            state["movesSeen"].remove(k)
    state["movesSeen"] = state["movesSeen"][-300:]
    return msgs


def board(state, today):
    xp = state["xp"]
    (lo, name), nxt = level_of(xp)
    frac = 1.0 if not nxt else (xp - lo) / (nxt[0] - lo)
    bal = balance(state)
    test = today.isoformat() < MONEY_START
    monday = week_start(today)
    wk_days = [(monday + timedelta(days=i)).isoformat() for i in range(7)]
    if test:
        wk = sum(v for k, v in state.get("practice", {}).items() if k in wk_days)
        top = (f"🧪 **Test week**: money starts Mon Nov 30.\nPractice earnings this week: **{fmt(wk)}**")
    else:
        wk = sum(e["amount"] for k, e in state["ledger"].items() if k.startswith("day:") and e["date"] in wk_days)
        top = f"💵 Balance **{usd(bal)}** · this week {fmt(wk)}" + ("\n🛡️ Grace week: misses cost $0 until Mon Nov 30" if today.isoformat() < PENALTY_START else "")
    if busted_now(state):
        b = next(b for b in state["busts"] if not b.get("paid"))
        top = (f"🚨 **BUSTED** 🚨 Move **${owed(state):g}** to savings by {date.fromisoformat(b['due']):%a %b} "
               f"{date.fromisoformat(b['due']).day}\nNo Magic spending · penalties doubled\n" + top)
    mult = (BUST_MULT if busted_now(state) else 1) if today.isoformat() >= PENALTY_START else 0
    todays = []
    for _, label, pay, miss in DAILY:
        todays.append(f"⬜ {label} `+${pay}/−${miss * mult}`")
    wd = today.weekday()
    if wd in LIFT_DAYS:
        todays.append(f"⬜ **{WORKOUTS[LIFT_DAYS[wd]][0]}** `+${LIFT_PAY[0]}/−${LIFT_PAY[1] * mult}`")
    if wd == 2:
        todays.append(f"⬜ cook extra for Daisy's Thursday lunch `+${COOK_EXTRA_PAY[0]}/−${COOK_EXTRA_PAY[1] * mult}`")
    if wd == 5:
        todays.append(f"⬜ long walk or activity `+${SAT_ACTIVITY_PAY}`")
    if wd == 6:
        todays.append(f"⬜ meal prep (Mon/Tue lunches) `+${MEAL_PREP_PAY[0]}/−${MEAL_PREP_PAY[1] * mult}`")
    fsteps = state.get("fitbit", {}).get("today_steps")
    if fsteps is not None:
        todays.append(f"⌚ steps so far: **{fsteps:,}** / {GOALS['steps']:,}")
    y = (today - timedelta(days=1)).isoformat()
    ye = state["ledger"].get("day:" + y)
    yv = ye["amount"] if ye else state.get("practice", {}).get(y)
    yline = f"{fmt(yv)}" + (" (practice)" if not ye and yv is not None else "") if yv is not None else "waiting for the log"
    bkey = state["boss"]["key"]
    bname, bemoji, bdesc, _ = BOSSES[bkey]
    got, need = boss_progress(state, monday, bkey)
    fields = [
        {"name": "Wallet", "value": top, "inline": False},
        {"name": f"Today's missions ({today:%a})", "value": "\n".join(todays), "inline": False},
        {"name": "Yesterday", "value": yline, "inline": True},
        {"name": "Level", "value": f"**{name}** · {xp:,} XP\n`{bar(frac, 12)}` "
                                    + (f"{nxt[0] - xp:,} to {nxt[1]} ({fmt(LEVEL_REWARD[nxt[1]])})" if nxt else "max!"),
         "inline": True},
        {"name": f"{bemoji} Boss: {bname} (XP only)", "value": f"{bdesc}: {got}/{need}", "inline": False},
    ]
    return {"title": "📱 Mission board", "color": 0xE53935 if busted_now(state) else (0x9E9E9E if test else 0x43A047),
            "fields": fields,
            "footer": {"text": "Missions settle from the daily food-log wrap-up · videos in #workout-guides · "
                               "not medical advice"},
            "timestamp": datetime.now(ZoneInfo("UTC")).isoformat()}


def day_money_post(d, items, amt, old):
    lines = [f"{'✅' if ok else '❌'} {label} `{fmt(a)}`" for label, ok, a in items]
    title = f"💵 {d:%a %b} {d.day}: {fmt(amt)}" + (f" (corrected from {fmt(old)})" if old is not None else "")
    return {"title": title, "description": "\n".join(lines),
            "color": 0x43A047 if amt > 0 else (0xFBC02D if amt == 0 else 0xE53935)}


# ---- hobby quests (XP only, no dollars): PlayStation trophies + Gunpla build stages ----
TROPHY_XP = {"bronze": 2, "silver": 5, "gold": 10, "platinum": 100}
GUNPLA_STAGE_XP = {"built": 40, "panel": 10, "decals": 10, "topcoat": 15, "section": 5}
GUNPLA_STAGE_NAME = {"built": "built", "panel": "panel lines", "decals": "decals", "topcoat": "top coat", "section": "decal section"}
GUNPLA_GRADE_MULT = {"SD": 0.75, "EG": 0.75, "HG": 1, "RG": 1.5, "MG": 2, "PG": 4}


def read_builds(url):
    """GUNPLA events in the mailbox calendar -> {key: (kit, grade, stage)}. A stage only counts with a photo."""
    import icalendar
    import recurring_ical_events
    cal = icalendar.Calendar.from_ical(get(url))
    start = datetime.combine(date.fromisoformat(GAME_START), datetime.min.time(), ET)
    out = {}
    for ev in recurring_ical_events.of(cal).between(start, datetime.now(ET) + timedelta(days=2)):
        if not str(ev.get("SUMMARY", "")).strip().upper().startswith("GUNPLA"):
            continue
        body = str(ev.get("DESCRIPTION", "") or "")
        try:
            j = json.loads(body[body.index("{"): body.rindex("}") + 1])
            kit, grade, stage = str(j["kit"]).strip()[:60], str(j.get("grade", "HG")).upper(), str(j["stage"]).lower()
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if kit and stage in GUNPLA_STAGE_XP and j.get("photo"):
            part = str(j.get("part", "")).strip()[:30]
            if stage == "section" and (not part or sum(1 for k in out if k.startswith(kit.lower() + "|section|")) >= 10):
                continue  # a decal section needs a name; at most 10 sections per kit
            out[f"{kit.lower()}|{stage}" + (f"|{part.lower()}" if stage == "section" else "")] = (kit + (f" / {part}" if stage == "section" else ""), grade, stage)
    return out


def hobby_quests(state, url):
    """Keep hobby XP matching the trophies and Gunpla stages on record (adds new ones, removes deleted stages). Returns lines to post."""
    hob = state.setdefault("hobby", {})
    hob.setdefault("trophies", {}); hob.setdefault("builds", {})
    notes = []
    tfile = HERE / "trophies_state.json"
    if tfile.exists():
        for t in json.loads(tfile.read_text()).get("week", []):
            at, typ = str(t.get("at", "")), str(t.get("type", "")).lower()
            key = f"{t.get('game', '')}|{typ}|{at}"
            if at[:10] >= GAME_START and typ in TROPHY_XP and key not in hob["trophies"]:
                hob["trophies"][key] = TROPHY_XP[typ]
                state["xp"] += TROPHY_XP[typ]
                notes.append(f"\U0001F3C6 +{TROPHY_XP[typ]} XP {typ} trophy ({t.get('game', '')})")
        hob["trophies"] = dict(list(hob["trophies"].items())[-400:])
    builds = read_builds(url)
    for key, (kit, grade, stage) in builds.items():
        if key not in hob["builds"]:
            xp = round(GUNPLA_STAGE_XP[stage] * GUNPLA_GRADE_MULT.get(grade, 1))
            hob["builds"][key] = xp
            state["xp"] += xp
            notes.append(f"\U0001F916 +{xp} XP Gunpla: {kit} ({grade}) {GUNPLA_STAGE_NAME[stage]}")
    for key in [k for k in hob["builds"] if k not in builds]:
        state["xp"] -= hob["builds"].pop(key)
        notes.append(f"Removed a Gunpla stage ({key.replace('|', ', ')})")
    return notes[:7] + ([f"...and {len(notes) - 7} more"] if len(notes) > 7 else [])


def main():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    state.setdefault("days", {}); state.setdefault("xp", 0); state.setdefault("posted", [])
    state.setdefault("plans", [])
    if not isinstance(state.get("ledger"), dict):  # v1 (card fund / savings jar) -> v2 wallet
        for k in ("ledger", "fund", "owed", "missRun", "settled", "moves", "levelsPaid", "start", "bossStreak", "rewards"):
            state.pop(k, None)
        state["ledger"] = {}
    now = datetime.fromisoformat(os.environ["GAME_TEST_NOW"]).replace(tzinfo=ET) if os.environ.get("GAME_TEST_NOW") else datetime.now(ET)
    today = now.date()
    for d in [d for d in state["days"] if d < GAME_START]:  # drop test days from before the start
        state["xp"] -= state["days"].pop(d)["xp"]
    url = os.environ.get("GAME_ICS_URL", "").strip()
    game_hook = os.environ.get("GAME_WEBHOOK_URL", "").strip()
    plan_hook = os.environ.get("MEALPLAN_WEBHOOK_URL", "").strip()
    guide_hook = os.environ.get("GUIDES_WEBHOOK_URL", "").strip()
    post = lambda payload: send(game_hook, dict(payload, username="Quest Log", allowed_mentions={"parse": []}))
    if guide_hook or DRY_RUN:  # workout guide card, re-posted only when the plan changes
        gp = guide_payload()
        sig = json.dumps(gp, sort_keys=True)
        if state.get("guideSig") != sig:
            try:
                state["guideId"] = send(guide_hook, gp, edit_id=state.get("guideId"))
            except urllib.error.HTTPError as ex:
                if ex.code != 404:
                    raise
                state["guideId"] = send(guide_hook, gp)
            state["guideSig"] = sig
    if not url:
        print("Game: GAME_ICS_URL not set; skipping")
        if not DRY_RUN:
            STATE_FILE.write_text(json.dumps(state, indent=1) + "\n")
        return
    if os.environ.get("GAME_TEST_STATS"):  # local testing only
        stats, plans, moves = json.loads(os.environ["GAME_TEST_STATS"]), {}, json.loads(os.environ.get("GAME_TEST_MOVES", "[]"))
        flags = json.loads(os.environ.get("GAME_TEST_FLAGS", "{}"))
    else:
        stats, plans, moves, flags = read_mailbox(url, date.fromisoformat(GAME_START) - timedelta(days=7))
    weights = {k: v for k, v in stats.items()}  # keep pre-start weights for the first weekly comparison
    stats = {k: v for k, v in stats.items() if k >= GAME_START or "weight" in v}
    fit = {}
    if not os.environ.get("GAME_TEST_STATS"):
        try:
            fit = read_fitbit(date.fromisoformat(GAME_START) - timedelta(days=14), today)
            ex = sorted({t for v in fit.values() for t, _ in v.get("exercises", [])})
            print(f"Game: Fitbit ok, {len(fit)} days, workout types seen: {', '.join(ex) or 'none'}")
        except Exception as e:  # Fitbit trouble never breaks the game; self-reported stats still count
            print(f"Game: Fitbit read failed ({type(e).__name__}: {str(e)[:120]})")
            fit = {}
    else:
        fit = json.loads(os.environ.get("GAME_TEST_FIT", "{}"))
    if fit:
        state["fitbit"] = {"ok": True, "checked": now.isoformat(timespec="minutes"),
                           "today_steps": fit.get(today.isoformat(), {}).get("steps")}
        for k, v in fit.items():
            if "weight" in v:
                state.setdefault("weightLog", {})[k] = v["weight"]
        state["weightLog"] = dict(sorted(state.get("weightLog", {}).items())[-120:])
    else:
        state.setdefault("fitbit", {})["ok"] = False
    for k, s in stats.items():  # Fitbit is the source of truth for steps (and fills in a missing lift)
        f = fit.get(k, {})
        if "steps" in f:
            s["steps"] = f["steps"]
        if not s.get("workout") and fit_lift(f) and date.fromisoformat(k).weekday() in LIFT_DAYS:
            s["workout"] = LIFT_DAYS[date.fromisoformat(k).weekday()]
    for k, fl in flags.items():  # taps from the Day page (DONE events)
        if k < GAME_START:
            continue
        if k in stats:
            stats[k].update(fl)
        else:
            fit.setdefault(k, {})["flags"] = fl

    # weekly boss (XP only)
    monday = week_start(today)
    if state.get("boss", {}).get("week") != monday.isoformat():
        old = state.get("boss")
        if old:
            got, need = boss_progress(state, date.fromisoformat(old["week"]), old["key"])
            state["lastBoss"] = old["key"]
            state.setdefault("bossLog", []).append({"week": old["week"], "key": old["key"], "won": got >= need})
            if got >= need:
                state["xp"] += 100
        state["boss"] = {"week": monday.isoformat(), "key": pick_boss(state, monday)}

    # XP for new or changed days
    changed = []
    for d, s in sorted(stats.items()):
        if d < GAME_START or d >= today.isoformat():  # today can be a running total; score it tomorrow
            continue
        sig = json.dumps(s, sort_keys=True)
        old = state["days"].get(d)
        if old and old.get("sig") == sig:
            continue
        xp, h, lines = score(date.fromisoformat(d), s)
        h["logged"] = True
        state["xp"] += xp - (old["xp"] if old else 0)
        state["days"][d] = {"xp": xp, "hits": h, "sig": sig}
        changed.append((d, s, xp, h, lines, bool(old)))

    msgs = settle(state, weights, today, fit)
    notes = apply_moves(state, moves)
    try:  # hobby quests (XP only); never allowed to break the game
        if not os.environ.get("GAME_TEST_STATS"):
            notes += hobby_quests(state, url)
    except Exception as ex:
        print(f"Game: hobby quests failed ({type(ex).__name__}: {str(ex)[:120]})")
    print(f"Game: {len(stats)} STATS days, {len(changed)} new/changed, {state['xp']} XP, "
          f"balance {usd(balance(state))}, owed ${owed(state):g}, {len(msgs)} money events")

    if game_hook or DRY_RUN:
        for d, s, xp, h, lines, was in changed:  # XP post per newly logged day (last 7 days only)
            if (d in state["posted"] and was) or (today - date.fromisoformat(d)).days > 7:
                continue
            post({"embeds": [day_post(date.fromisoformat(d), s, xp, h, lines)]})
            state["posted"].append(d)
        state["posted"] = state["posted"][-60:]
        for kind, p in msgs:
            if kind == "day":
                post({"embeds": [day_money_post(*p)]})
            elif kind == "week":
                w, total, lines = p
                post({"embeds": [{"title": f"💰 Payday: week of {w:%b} {w.day}",
                                  "description": f"Missions this week: **{fmt(total)}**\n" + "\n".join(lines)
                                                 + f"\n\n💵 Balance **{usd(balance(state))}**",
                                  "color": 0xE53935 if busted_now(state) else 0xFFD54F}]})
            else:
                notes.insert(0, p)
        if notes:
            post({"embeds": [{"title": "💵 Wallet", "description": "\n".join(notes)
                              + f"\n\nBalance **{usd(balance(state))}**" + (f" · owe savings **${owed(state):g}**"
                                                                          if busted_now(state) else ""),
                              "color": 0xFFD54F}]})
        # reminders (first run inside a 2-hour window after the time)
        sent = state.setdefault("reminded", [])
        for i, (days, hhmm, text) in enumerate(REMINDERS):
            hh, mm = map(int, hhmm.split(":"))
            at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            key = f"{today}|{i}"
            if today.weekday() in days and at <= now < at + timedelta(hours=2) and key not in sent:
                msg = text
                if hhmm == "17:00":  # 5 PM step check decides tonight's pedal time
                    msg = (text + "\n" if text else "") + pedal_line(fit.get(today.isoformat(), {}).get("steps") if fit else None)
                post({"content": "📟 " + msg})
                sent.append(key)
        state["reminded"] = sent[-60:]
        # mission board: edit in place; refresh when anything changed or once an hour
        hour_key = now.strftime("%Y-%m-%d %H")
        if changed or msgs or notes or state.get("boardHour") != hour_key:
            payload = {"username": "Quest Log", "embeds": [board(state, today)], "allowed_mentions": {"parse": []}}
            try:
                state["boardId"] = send(game_hook, payload, edit_id=state.get("boardId"))
            except urllib.error.HTTPError as ex:
                if ex.code != 404:
                    raise
                state["boardId"] = send(game_hook, payload)
            state["boardHour"] = hour_key

    if plan_hook or DRY_RUN:
        for d, body in sorted(plans.items()):
            if d in state["plans"] or not body:
                continue
            chunks = split_markdown(body)
            for i in range(0, len(chunks), 10):
                embeds = [{"description": c, "color": 0x43A047} for c in chunks[i:i + 10]]
                if i == 0:
                    nd = date.fromisoformat(d) + timedelta(days=(6 - date.fromisoformat(d).weekday()) % 7)  # food week runs Sunday-Saturday: the first Sunday on or after the plan's date
                    embeds[0]["title"] = f"🍽️ Meal plan, week of {nd:%b} {nd.day}"
                send(plan_hook, {"username": "Meal Plan", "embeds": embeds, "allowed_mentions": {"parse": []}})
            state["plans"] = (state["plans"] + [d])[-20:]

    # summary for hub_bot's #today
    y = (today - timedelta(days=1)).isoformat()
    wk = LIFT_DAYS.get(today.weekday())
    (_, name), _ = level_of(state["xp"])
    test = today.isoformat() < MONEY_START
    state["todayLine"] = {
        "date": today.isoformat(),
        "level": f"{name} · {state['xp']:,} XP",
        "yesterday": state["days"][y]["xp"] if y in state["days"] else None,
        "workout": (f"{WORKOUTS[wk][0]}: " + " · ".join(WORKOUTS[wk][1])) if wk else "Rest day (walk; pedal if under 10k)",
        "money": ("🚨 BUSTED: move $%g to savings · " % owed(state) if busted_now(state) else "")
                 + ("test week (money starts Nov 30)" if test else f"{usd(balance(state))} balance"),
        "boss": f"{BOSSES[state['boss']['key']][1]} {BOSSES[state['boss']['key']][0]} "
                f"{'%d/%d' % boss_progress(state, monday, state['boss']['key'])}",
    }
    # compact summary for the "Tim's Day" page (copied into the page by a scheduled task)
    (lo, lname), nx = level_of(state["xp"])
    wk_days = [(monday + timedelta(days=i)).isoformat() for i in range(7)]
    yl = state["ledger"].get("day:" + y)
    due = next((b["due"] for b in state.get("busts", []) if not b.get("paid")), "")
    state["board"] = {
        "date": today.isoformat(), "asOf": now.strftime("%a %I:%M %p").replace(" 0", " "),
        "testWeek": test, "practiceWeek": sum(v for k, v in state.get("practice", {}).items() if k in wk_days),
        "balance": balance(state),
        "week": sum(e["amount"] for k, e in state["ledger"].items() if k.startswith("day:") and e["date"] in wk_days),
        "yesterday": yl["amount"] if yl else state.get("practice", {}).get(y),
        "busted": busted_now(state), "owed": owed(state),
        "due": (date.fromisoformat(due).strftime("%a %b ") + str(date.fromisoformat(due).day)) if due else "",
        "level": lname, "xp": state["xp"], "next": nx[1] if nx else None,
        "toNext": (nx[0] - state["xp"]) if nx else None, "levelSpan": (nx[0] - lo) if nx else None,
        "nextReward": LEVEL_REWARD.get(nx[1]) if nx else None,
        "boss": state["todayLine"]["boss"], "steps": state.get("fitbit", {}).get("today_steps"),
        "stepsAt": state.get("fitbit", {}).get("checked", "")[11:16],
        "stepTarget": STEP_TARGET, "fitWalk": fit_walk(fit.get(today.isoformat())), "fitLift": fit_lift(fit.get(today.isoformat())), "pedalMin": pedal_minutes(state.get("fitbit", {}).get("today_steps")),
        "history": [{"d": dd, "amt": (state["ledger"].get("day:" + dd) or {}).get("amount", state.get("practice", {}).get(dd))}
                    for dd in [(today - timedelta(days=i)).isoformat() for i in range(28, 0, -1)]],
        "weights": [{"d": k, "lb": v} for k, v in state.get("weightLog", {}).items() if k >= (today - timedelta(days=60)).isoformat()],
    }
    if not DRY_RUN:
        STATE_FILE.write_text(json.dumps(state, indent=1) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:  # never break the spoiler workflow
        print(f"Game bot error: {type(ex).__name__}: {str(ex)[:200]}")
