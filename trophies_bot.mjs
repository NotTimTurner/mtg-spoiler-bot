// Trophies bot: posts Tim's newly earned PlayStation trophies to #trophies.
//
// Uses the psn-api package with an NPSSO token (repo secret PSN_NPSSO).
// The NPSSO token expires after about 2 months: when it does, the bot posts
// one warning to the channel and stops until the secret is refreshed
// (sign in at playstation.com, then open https://ca.account.sony.com/api/v1/ssocookie).
//
// Runs once an hour (the first run in each hour) to keep PSN logins low.
// State (last check time, titles) is kept in trophies_state.json.
// Never fails the workflow.
//
// Env vars: PSN_NPSSO, TROPHIES_WEBHOOK_URL, FORCE=1 (ignore the hourly limit)
import fs from "node:fs";
import {
  exchangeNpssoForAccessCode, exchangeAccessCodeForAuthTokens,
  getUserTitles, getTitleTrophies, getUserTrophiesEarnedForTitle,
} from "psn-api";

const STATE_FILE = new URL("./trophies_state.json", import.meta.url);
const WEBHOOK = process.env.TROPHIES_WEBHOOK_URL || "";
// Weekly recap goes to #gaming (the Free Games webhook lives there)
const RECAP_WEBHOOK = process.env.RECAP_WEBHOOK_URL || "";
const NPSSO = process.env.PSN_NPSSO || "";
const MAX_PER_TITLE = 8;
const COLORS = { platinum: 0x8fb8de, gold: 0xd4af37, silver: 0xb4b4b4, bronze: 0xcd7f32 };
const UA = "DiscordBot (https://github.com/NotTimTurner/mtg-spoiler-bot, 1.0)";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function post(payload, webhook = WEBHOOK) {
  const res = await fetch(webhook, {
    method: "POST",
    headers: { "Content-Type": "application/json", "User-Agent": UA },
    body: JSON.stringify({ username: "Trophies", allowed_mentions: { parse: [] }, ...payload }),
  });
  if (!res.ok) throw new Error(`Discord ${res.status}: ${await res.text()}`);
}

function saveState(state) {
  fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 1) + "\n");
}

async function main() {
  if (!WEBHOOK || !NPSSO) {
    console.log("TROPHIES_WEBHOOK_URL or PSN_NPSSO not set; skipping trophies");
    return;
  }
  const state = fs.existsSync(STATE_FILE) ? JSON.parse(fs.readFileSync(STATE_FILE, "utf8")) : null;
  const minute = new Date().getUTCMinutes();
  if (state && minute >= 15 && process.env.FORCE !== "1") {
    console.log("Trophies: checked hourly; skipping this run");
    return;
  }
  const runStart = new Date().toISOString();

  let auth;
  try {
    auth = await exchangeAccessCodeForAuthTokens(await exchangeNpssoForAccessCode(NPSSO));
  } catch (e) {
    console.log(`Trophies: PSN sign-in failed: ${e.message}`);
    if (state && !state.authWarned) {
      await post({ content: "Trophy feed paused: the PlayStation sign-in token (PSN_NPSSO) has expired. Refresh the repo secret to turn it back on." });
      state.authWarned = true;
      saveState(state);
    }
    return;
  }

  const week = (state && state.week) || [];
  const { trophyTitles } = await getUserTitles(auth, "me", { limit: 50 });
  if (!state) {
    const plats = trophyTitles.filter((t) => t.earnedTrophies.platinum > 0).length;
    saveState({ lastCheck: runStart, authWarned: false });
    await post({ content: `Trophy feed connected. Watching ${trophyTitles.length} recent games (${plats} with platinums). New trophies will show up here.` });
    console.log("Trophies: first run, state saved");
    return;
  }

  const since = new Date(state.lastCheck);
  const changed = trophyTitles.filter((t) => new Date(t.lastUpdatedDateTime) > since);
  console.log(`Trophies: ${changed.length} game(s) with new trophies since ${state.lastCheck}`);
  let count = 0;
  for (const t of changed.reverse()) {
    const opts = { npServiceName: t.npServiceName };
    const earned = await getUserTrophiesEarnedForTitle(auth, "me", t.npCommunicationId, "all", opts);
    const fresh = earned.trophies
      .filter((x) => x.earned && x.earnedDateTime && new Date(x.earnedDateTime) > since)
      .sort((a, b) => new Date(a.earnedDateTime) - new Date(b.earnedDateTime));
    if (!fresh.length) continue;
    const info = await getTitleTrophies(auth, t.npCommunicationId, "all", opts);
    const byId = new Map(info.trophies.map((x) => [x.trophyId, x]));
    const total = Object.values(t.earnedTrophies).reduce((a, b) => a + b, 0);
    const defined = Object.values(t.definedTrophies).reduce((a, b) => a + b, 0);
    const show = fresh.length > MAX_PER_TITLE
      ? [...fresh.filter((x) => x.trophyType === "platinum"), ...fresh.filter((x) => x.trophyType !== "platinum").slice(-(MAX_PER_TITLE - 1))]
      : fresh;
    const embeds = show.map((x) => {
      const d = byId.get(x.trophyId) || {};
      const plat = x.trophyType === "platinum";
      return {
        author: { name: t.trophyTitleName, icon_url: t.trophyTitleIconUrl },
        title: (plat ? "Platinum! " : "") + (d.trophyName || "Trophy"),
        description: d.trophyDetail || "",
        color: COLORS[x.trophyType] || 0x5865f2,
        thumbnail: d.trophyIconUrl ? { url: d.trophyIconUrl } : undefined,
        footer: { text: `${x.trophyType[0].toUpperCase()}${x.trophyType.slice(1)}` +
          (x.trophyEarnedRate ? ` · ${x.trophyEarnedRate}% of players` : "") +
          ` · ${total}/${defined} (${t.progress}%)` },
        timestamp: x.earnedDateTime,
      };
    });
    if (fresh.length > show.length) {
      embeds.unshift({ author: { name: t.trophyTitleName, icon_url: t.trophyTitleIconUrl },
        description: `${fresh.length} new trophies; showing the latest ${show.length}.`, color: 0x5865f2 });
    }
    for (let i = 0; i < embeds.length; i += 10) {
      await post({ embeds: embeds.slice(i, i + 10) });
      await sleep(1500);
    }
    count += fresh.length;
    for (const x of fresh) week.push({ game: t.trophyTitleName, type: x.trophyType, at: x.earnedDateTime });
  }
  const recapDate = await weeklyRecap(week, state.lastRecap);
  const cutoff = Date.now() - 8 * 864e5;
  saveState({ lastCheck: runStart, authWarned: false, lastRecap: recapDate || state.lastRecap,
    week: week.filter((w) => new Date(w.at).getTime() > cutoff) });
  console.log(`${count} trophy(s) posted`);
}

// Sunday evening (7 PM ET hour): post a "what we're playing" recap in #gaming
async function weeklyRecap(week, lastRecap) {
  if (!RECAP_WEBHOOK) return null;
  const now = new Date();
  const et = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", hour: "numeric", hour12: false, year: "numeric", month: "2-digit", day: "2-digit" })
    .formatToParts(now).reduce((o, p) => ({ ...o, [p.type]: p.value }), {});
  const today = `${et.year}-${et.month}-${et.day}`;
  if (et.weekday !== "Sun" || Number(et.hour) !== 19 || lastRecap === today) return null;
  const since = now.getTime() - 7 * 864e5;
  const recent = week.filter((w) => new Date(w.at).getTime() > since);
  const byGame = {};
  for (const w of recent) (byGame[w.game] ||= []).push(w.type);
  const lines = Object.entries(byGame).sort((a, b) => b[1].length - a[1].length).map(([g, types]) => {
    const plat = types.includes("platinum") ? " (Platinum!)" : "";
    return `• **${g}**: ${types.length} troph${types.length === 1 ? "y" : "ies"}${plat}`;
  });
  const content = [
    "**Weekly recap: what are you playing?**",
    recent.length ? `Tim earned ${recent.length} trophies this week:` : "Quiet trophy week for Tim.",
    ...lines,
    "",
    "Drop what you've been playing (or stuck on) this week below.",
  ].join("\n");
  await post({ username: "Weekly Recap", content: content.slice(0, 1990) }, RECAP_WEBHOOK);
  return today;
}

main().catch((e) => console.log(`Trophies bot error: ${e.message}`));
