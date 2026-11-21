#!/usr/bin/env python3
"""Free NBA weekly games grid & back-to-back finder. Python 3.8+, standard library only.

  python3 run.py grid [--roster "Victor Wembanyama, Luka Doncic"] [--slots 10] [--tz "Seoul"]
  python3 run.py grid --teams all  every team's games this week (Mon-Sun): count, game days, back-to-backs
  python3 run.py settings --tz "Los Angeles"   remember a time zone (city or IANA name)
  python3 run.py setkey            read your free BALLDONTLIE key from stdin and save it locally
  python3 run.py status            show where the key is read from and how fresh the cache is

The key comes from the BALLDONTLIE_API_KEY environment variable, or from the local config file written by
`setkey` (~/.config/dhsoft-fantasy-grids/config.json). It is only sent to api.balldontlie.io and never printed.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
API = "https://api.balldontlie.io"
CONFIG = Path(os.environ.get("FANTASY_GRIDS_CONFIG") or Path.home() / ".config" / "dhsoft-fantasy-grids" / "config.json")
DATA = Path(os.environ.get("FREE_NBA_GRID_DATA") or HERE / "data")
SCHEDULE_MAX_AGE_H = 6
PLAYER_MAX_AGE_H = 24 * 7
MIN_INTERVAL_S = 12.5  # free plan: 5 requests per minute
LIGHT_DAY_MAX = 7  # days with this many games or fewer leave more streamers available
DEFAULT_SLOTS = 10

FOOTER = (
    "---\n"
    "Schedule data: BALLDONTLIE. Unofficial — not affiliated with, endorsed by, or sponsored by the NBA or any team. "
    "For season-long fantasy planning only; not betting or daily fantasy (DFS) contest advice.\n\n"
    "Want this checked automatically every 4 hours, with injuries, minutes trends, start/sit and streamers for your "
    "weak categories? Try Fantasy NBA Roster Watch on Capafy (3-day free trial): "
    "https://capafy.ai/agent/fantasy-nba-roster-watch-start-sit-streamers/4740205927"
)
KEY_HELP = (
    "Get a free BALLDONTLIE API key (about 1 minute): sign up at https://app.balldontlie.io and copy the key from "
    "your account page. Then either set the environment variable BALLDONTLIE_API_KEY, or run this yourself in a "
    "terminal and paste the key when asked: python3 run.py setkey"
)

# City -> IANA zone, plus a built-in fallback (standard UTC offset in hours, daylight-saving rule) for systems
# without a time-zone database (common on Windows Python). ponytail: only these rules; add more if users need them.
CITIES = {
    "new york": "America/New_York", "boston": "America/New_York", "toronto": "America/Toronto", "miami": "America/New_York",
    "atlanta": "America/New_York", "chicago": "America/Chicago", "dallas": "America/Chicago", "houston": "America/Chicago",
    "denver": "America/Denver", "phoenix": "America/Phoenix", "los angeles": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles", "seattle": "America/Los_Angeles", "vancouver": "America/Vancouver",
    "mexico city": "America/Mexico_City", "london": "Europe/London", "dublin": "Europe/Dublin", "paris": "Europe/Paris",
    "berlin": "Europe/Berlin", "madrid": "Europe/Madrid", "rome": "Europe/Rome", "athens": "Europe/Athens",
    "istanbul": "Europe/Istanbul", "dubai": "Asia/Dubai", "mumbai": "Asia/Kolkata", "delhi": "Asia/Kolkata",
    "bangkok": "Asia/Bangkok", "ho chi minh city": "Asia/Ho_Chi_Minh", "jakarta": "Asia/Jakarta", "manila": "Asia/Manila",
    "singapore": "Asia/Singapore", "hong kong": "Asia/Hong_Kong", "taipei": "Asia/Taipei", "beijing": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai", "seoul": "Asia/Seoul", "tokyo": "Asia/Tokyo", "sydney": "Australia/Sydney",
    "melbourne": "Australia/Melbourne", "auckland": "Pacific/Auckland", "sao paulo": "America/Sao_Paulo",
    "buenos aires": "America/Argentina/Buenos_Aires",
}
FALLBACK = {
    "America/New_York": (-5, "us"), "America/Toronto": (-5, "us"), "America/Chicago": (-6, "us"),
    "America/Denver": (-7, "us"), "America/Phoenix": (-7, None), "America/Los_Angeles": (-8, "us"),
    "America/Vancouver": (-8, "us"), "America/Mexico_City": (-6, None), "Europe/London": (0, "eu"),
    "Europe/Dublin": (0, "eu"), "Europe/Paris": (1, "eu"), "Europe/Berlin": (1, "eu"), "Europe/Madrid": (1, "eu"),
    "Europe/Rome": (1, "eu"), "Europe/Athens": (2, "eu"), "Europe/Istanbul": (3, None), "Asia/Dubai": (4, None),
    "Asia/Kolkata": (5.5, None), "Asia/Bangkok": (7, None), "Asia/Ho_Chi_Minh": (7, None), "Asia/Jakarta": (7, None),
    "Asia/Manila": (8, None), "Asia/Singapore": (8, None), "Asia/Hong_Kong": (8, None), "Asia/Taipei": (8, None),
    "Asia/Shanghai": (8, None), "Asia/Seoul": (9, None), "Asia/Tokyo": (9, None), "Australia/Sydney": (10, "au"),
    "Australia/Melbourne": (10, "au"), "Pacific/Auckland": (12, "nz"), "America/Sao_Paulo": (-3, None),
    "America/Argentina/Buenos_Aires": (-3, None), "UTC": (0, None),
}

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


class Fail(Exception):
    """kind: api_key_missing, auth_failed, rate_limited, network, bad_response, bad_timezone, bad_slots, bad_key."""

    def __init__(self, kind: str, message: str, fix: str):
        super().__init__(message)
        self.kind, self.fix = kind, fix


# ---------- time zones ----------

def resolve_tz(text: str) -> str:
    """City name or IANA name -> IANA name."""
    t = (text or "").strip()
    if not t:
        return "America/New_York"
    key = " ".join(re.sub(r"[^a-z ]", " ", t.lower().split(",")[0]).split())  # "Seoul, Korea" -> "seoul"
    if key in CITIES:
        return CITIES[key]
    for zone in FALLBACK:
        if zone.lower() == t.lower():
            return zone
    if _zoneinfo(t) is not None:
        return t
    raise Fail("bad_timezone", "Unknown city or time zone '%s'." % clean(t),
               "Use a big city near you (e.g. Seoul, London, Los Angeles) or an IANA name such as Asia/Seoul.")


def _zoneinfo(name: str):
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+; needs a time-zone database
        return ZoneInfo(name)
    except Exception:
        return None


def _nth_sunday(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_sunday(year: int, month: int) -> date:
    d = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _dst(rule, utc: datetime, std: float) -> bool:
    y = utc.year
    naive = utc.replace(tzinfo=None)
    if rule == "us":  # 2nd Sunday of March 2:00 local to 1st Sunday of November 2:00 local
        start = datetime.combine(_nth_sunday(y, 3, 2), datetime.min.time()) + timedelta(hours=2 - std)
        end = datetime.combine(_nth_sunday(y, 11, 1), datetime.min.time()) + timedelta(hours=1 - std)
        return start <= naive < end
    if rule == "eu":  # last Sunday of March to last Sunday of October, 01:00 UTC
        start = datetime.combine(_last_sunday(y, 3), datetime.min.time()) + timedelta(hours=1)
        end = datetime.combine(_last_sunday(y, 10), datetime.min.time()) + timedelta(hours=1)
        return start <= naive < end
    if rule in ("au", "nz"):  # southern hemisphere: from early spring (Oct / late Sep) to early April
        start = datetime.combine(_nth_sunday(y, 10, 1) if rule == "au" else _last_sunday(y, 9),
                                 datetime.min.time()) + timedelta(hours=2 - std)
        end = datetime.combine(_nth_sunday(y, 4, 1), datetime.min.time()) + timedelta(hours=2 - std)
        return naive >= start or naive < end
    return False


def to_local(utc: datetime, zone: str) -> datetime:
    """Aware UTC datetime -> naive local wall time in `zone`."""
    zi = _zoneinfo(zone)
    if zi is not None:
        return utc.astimezone(zi).replace(tzinfo=None)
    std, rule = FALLBACK.get(zone, (0, None))
    return (utc + timedelta(hours=std + (1 if _dst(rule, utc, std) else 0))).replace(tzinfo=None)


def tz_label(zone: str) -> str:
    return zone.split("/")[-1].replace("_", " ")


# ---------- key, settings and cache ----------

def _read_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_config(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
    try:
        os.chmod(CONFIG, 0o600)
    except OSError:
        pass


def load_key() -> tuple[str, str]:
    key = (os.environ.get("BALLDONTLIE_API_KEY") or "").strip()
    if key:
        return key, "environment variable BALLDONTLIE_API_KEY"
    key = (_read_config().get("balldontlie_api_key") or "").strip()
    return key, (f"config file {CONFIG}" if key else "")


def save_key(key: str) -> None:
    key = key.strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{16,100}", key):
        raise Fail("bad_key", "That does not look like a BALLDONTLIE API key.", KEY_HELP)
    cfg = _read_config()
    cfg["balldontlie_api_key"] = key
    _write_config(cfg)


def read_cache(name: str, max_age_h: float):
    try:
        d = json.loads((DATA / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return None if time.time() - d.get("saved", 0) > max_age_h * 3600 else d.get("data")


def write_cache(name: str, data) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / name).write_text(json.dumps({"saved": time.time(), "data": data}), encoding="utf-8")


# ---------- HTTP (free plan: 5 requests per minute) ----------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib would copy the Authorization header (the key) to the new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _urlopen(req, timeout):
    return _OPENER.open(req, timeout=timeout)


class Client:
    def __init__(self, key: str, sleep=None, min_interval: float = MIN_INTERVAL_S):
        self._key, self.min_interval = key, min_interval
        self._sleep = sleep or (lambda s: time.sleep(s))
        self._last = None
        self.requests = 0

    def get(self, path: str, params: list) -> dict:
        url = API + path + "?" + urllib.parse.urlencode(params)
        for attempt in range(3):
            if self._last is not None:
                gap = self.min_interval - (time.monotonic() - self._last)
                if gap > 0:
                    self._sleep(gap)
            self._last = time.monotonic()
            self.requests += 1
            req = urllib.request.Request(url, headers={"Authorization": self._key, "Accept": "application/json",
                                                       "User-Agent": "free-nba-games-grid/1.0"})
            try:
                with _urlopen(req, 15) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise Fail("auth_failed", "BALLDONTLIE rejected the API key (HTTP %d)." % exc.code,
                               "Check the key, or get a new free key. " + KEY_HELP)
                if exc.code == 429:
                    if attempt < 2:
                        print("Free plan limit reached (5 requests per minute); waiting 60 seconds...", file=sys.stderr)
                        self._sleep(61)
                        continue
                    raise Fail("rate_limited", "BALLDONTLIE's free plan limit (5 requests per minute) was "
                               "reached again.", "Wait a minute and run the command again.")
                if exc.code >= 500 and attempt < 2:
                    self._sleep(5)
                    continue
                raise Fail("network", "BALLDONTLIE returned HTTP %d." % exc.code, "Try again in a few minutes.")
            except ValueError:
                raise Fail("bad_response", "BALLDONTLIE sent a response that is not JSON.", "Try again in a few minutes.")
            except (urllib.error.URLError, OSError) as exc:
                if attempt < 2:
                    self._sleep(5)
                    continue
                raise Fail("network", "Could not reach api.balldontlie.io (%s)." % type(exc).__name__,
                           "Check your internet connection and try again.")
        raise Fail("network", "Could not reach api.balldontlie.io.", "Try again in a few minutes.")

    def paginate(self, path: str, params: list) -> list:
        items, cursor = [], None
        for _ in range(20):
            page = self.get(path, params + [("per_page", "100")] + ([("cursor", str(cursor))] if cursor else []))
            data = page.get("data")
            if not isinstance(data, list):
                raise Fail("bad_response", "Unexpected response from BALLDONTLIE (no data list).", "Try again later.")
            items += data
            cursor = (page.get("meta") or {}).get("next_cursor")
            if not cursor or not data:
                break
        return items


# ---------- schedule ----------

def clean(value) -> str:
    """Provider or user text printed into tables: drop characters that could break Markdown."""
    return "".join(" " if ch in "<>`|\r\n" else ch for ch in str(value or "")).strip()


def league_today(now: datetime) -> date:
    """NBA dates follow US Eastern time."""
    return to_local(now, "America/New_York").date()


def two_weeks(today: date) -> tuple[date, date, date]:
    """(this Monday, next Monday, next Sunday)."""
    mon = today - timedelta(days=today.weekday())
    return mon, mon + timedelta(days=7), mon + timedelta(days=13)


def norm_game(g: dict) -> dict:
    d = str(g.get("date") or "")[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        raise Fail("bad_response", "Unexpected date in BALLDONTLIE's schedule.", "Try again later.")
    start = g.get("datetime")
    return {"date": d, "start": start if isinstance(start, str) and start[:4] >= "2000" else None,
            "home": clean(g["home_team"]["abbreviation"]), "away": clean(g["visitor_team"]["abbreviation"]),
            "post": bool(g.get("postseason"))}


def load_schedule(client_factory, today: date) -> dict:
    mon, _, end = two_weeks(today)
    cached = read_cache("schedule.json", SCHEDULE_MAX_AGE_H)
    if cached and cached.get("from") == mon.isoformat():
        return cached
    print("Downloading two weeks of games (2-3 requests on the free plan)...", file=sys.stderr)
    raw = client_factory().paginate("/v1/games", [("start_date", mon.isoformat()), ("end_date", end.isoformat())])
    sched = {"from": mon.isoformat(), "to": end.isoformat(),
             "fetched": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
             "games": [norm_game(g) for g in raw]}
    write_cache("schedule.json", sched)
    return sched


def team_days(games: list) -> dict:
    """{team: sorted list of league dates with a game}."""
    out = {}
    for g in games:
        for t in (g["home"], g["away"]):
            out.setdefault(t, set()).add(g["date"])
    return {t: sorted(ds) for t, ds in out.items()}


def back_to_backs(days: list) -> list:
    """Second days of back-to-backs (a game on the day before too)."""
    s = set(days)
    return [d for d in days if (date.fromisoformat(d) - timedelta(days=1)).isoformat() in s]


def week_teams(sched: dict, mon: date) -> dict:
    """{team: (game dates in the week mon..mon+6, back-to-back second dates in that week)}, every team in the schedule.

    ponytail: a back-to-back that starts on the Sunday before `mon` is not seen (the schedule starts on `mon`)."""
    week = {(mon + timedelta(days=i)).isoformat() for i in range(7)}
    tdays = team_days([g for g in sched["games"] if not g["post"]])
    return {t: ([d for d in ds if d in week], [d for d in back_to_backs(ds) if d in week]) for t, ds in tdays.items()}


def b2b_label(second: str) -> str:
    """'2026-11-25' -> 'Nov 24-25' (both days of the back-to-back)."""
    d2 = date.fromisoformat(second)
    d1 = d2 - timedelta(days=1)
    return "%s %d-%s" % (d1.strftime("%b"), d1.day, d2.day if d1.month == d2.month else d2.strftime("%b ") + str(d2.day))


def report_all_teams(sched: dict, today: date) -> str:
    mon = today - timedelta(days=today.weekday())
    rows = week_teams(sched, mon)
    L = ["# NBA games per team — %s to %s (%d teams)" % (day_label(mon), day_label(mon + timedelta(days=6)), len(rows)),
         "", "League dates are US Eastern. Schedule updated %s." % sched["fetched"], "",
         "| Team | Games | Game days | Back-to-backs |", "|---|---|---|---|"]
    for t in sorted(rows, key=lambda t: (-len(rows[t][0]), t)):
        days, b2b = rows[t]
        L.append("| %s | %d | %s | %s |" % (t, len(days), " ".join(date.fromisoformat(d).strftime("%a") for d in days) or "-",
                                          ", ".join(b2b_label(d) for d in b2b) or "-"))
    L += ["", FOOTER]
    return "\n".join(L)


# ---------- names ----------

def norm_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(w for w in s.split() if w not in {"jr", "sr", "ii", "iii", "iv", "v"})


def lookup_player(client_factory, raw: str) -> list:
    """Candidates from BALLDONTLIE's player search (cached a week per search word)."""
    words = norm_name(raw).split()
    if not words:
        return []
    # A misspelled last name finds nothing, so fall back to the first name, then the first letters of the last name.
    for term in dict.fromkeys([words[-1], words[0], words[-1][:4]]):
        if len(term) >= 3:
            found = _search(client_factory, term)
            if found:
                return found
    return []


def _search(client_factory, term: str) -> list:
    cache = read_cache("players.json", PLAYER_MAX_AGE_H * 10) or {}
    hit = cache.get(term)
    if hit is None or time.time() - hit.get("at", 0) > PLAYER_MAX_AGE_H * 3600:
        found = client_factory().paginate("/v1/players", [("search", term)])
        hit = {"at": time.time(), "players": [
            {"name": clean("%s %s" % (p.get("first_name", ""), p.get("last_name", ""))),
             "team": clean((p.get("team") or {}).get("abbreviation")),
             "draft": p.get("draft_year") or 0} for p in found]}
        cache[term] = hit
        write_cache("players.json", cache)
    return hit["players"]


def match_roster(client_factory, names: list, playing_teams: set) -> tuple[list, list, list]:
    """Returns (players [{name, team}], unmatched names, notes about corrected spellings).

    The free plan has no "active players" list, so among same-name players the one on a team with games in the
    window and the latest draft year wins. ponytail: heuristic; exact for current players with unique names."""
    out, unmatched, notes = [], [], []
    for raw in names:
        raw = raw.strip()
        if not raw:
            continue
        pool = {}
        for c in lookup_player(client_factory, raw):
            if c["team"]:
                pool.setdefault(norm_name(c["name"]), []).append(c)
        key = norm_name(raw)
        hit = pool.get(key)
        if not hit:
            close = difflib.get_close_matches(key, list(pool), n=1, cutoff=0.8)
            if close:
                hit = pool[close[0]]
                notes.append("'%s' matched to %s" % (clean(raw), hit[0]["name"]))
        if hit:
            best = sorted(hit, key=lambda c: (c["team"] in playing_teams, c["draft"]), reverse=True)[0]
            p = {"name": best["name"], "team": best["team"]}
            if p not in out:  # the same player typed twice counts once
                out.append(p)
        else:
            unmatched.append(clean(raw))
    return out, unmatched, notes


# ---------- report ----------

def day_label(d: date) -> str:
    return d.strftime("%a %b ") + str(d.day)


def report(sched: dict, today: date, zone: str, roster=None, slots: int = DEFAULT_SLOTS, unmatched=(), notes=()) -> str:
    games = [g for g in sched["games"] if not g["post"]]
    mon, nmon, end = two_weeks(today)
    tdays = team_days(games)
    this_w = [(mon + timedelta(days=i)).isoformat() for i in range(7)]
    next_w = [(nmon + timedelta(days=i)).isoformat() for i in range(7)]
    per_day = {}
    for g in games:
        per_day.setdefault(g["date"], []).append(g)

    def count(team, week):
        return sum(1 for d in tdays.get(team, []) if d in week)

    L = ["# NBA weekly games grid — %s to %s (this week and next)" % (day_label(mon), day_label(end)), "",
         "League dates are US Eastern; tip-off times are shown for %s. Schedule updated %s." % (tz_label(zone), sched["fetched"]),
         ""]
    for n in notes:
        L.append("- Spelling corrected: %s" % n)
    if notes:
        L.append("")
    if roster is not None:
        days = [d for d in this_w + next_w if d >= today.isoformat()]
        L += ["## Your roster", "", "| Player | Team | This week | Next week | Back-to-backs |", "|---|---|---|---|---|"]
        for p in roster:
            b2b = [day_label(date.fromisoformat(d)) for d in back_to_backs(tdays.get(p["team"], [])) if d in days]
            L.append("| %s | %s | %d | %d | %s |" % (p["name"], p["team"], count(p["team"], this_w), count(p["team"], next_w),
                                                  ", ".join(b2b) or "-"))
        if unmatched:
            L += ["", "Not found (check the spelling): %s" % ", ".join(unmatched)]
        L += ["", "## Open slots by day (%d starting slots)" % slots, "", "| Day | Your players with a game | Open slots |",
              "|---|---|---|"]
        for d in days:
            n = sum(1 for p in roster if d in tdays.get(p["team"], []))
            L.append("| %s | %d | %s |" % (day_label(date.fromisoformat(d)), n, max(0, slots - n) or "-"))
        L.append("")
    L += ["## Games per team", "", "| Team | This week | Next week | Back-to-backs |", "|---|---|---|---|"]
    for t in sorted(tdays, key=lambda t: (-(count(t, this_w) + count(t, next_w)), t)):
        b2b = [day_label(date.fromisoformat(d)) for d in back_to_backs(tdays[t]) if d in this_w + next_w]
        L.append("| %s | %d | %d | %s |" % (t, count(t, this_w), count(t, next_w), ", ".join(b2b) or "-"))
    L += ["", "## Light days (%d or fewer games: fewer of your players are likely to play, so these are the days with open slots to fill with a streamer)" % LIGHT_DAY_MAX, ""]
    light = [d for d in this_w + next_w if d >= today.isoformat() and len(per_day.get(d, [])) <= LIGHT_DAY_MAX]
    for d in light:
        gs = per_day.get(d, [])
        tips = []
        for g in sorted(gs, key=lambda g: g["start"] or ""):
            when = ""
            if g["start"]:
                lt = to_local(datetime.strptime(g["start"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc), zone)
                when = " " + lt.strftime("%a ") + lt.strftime("%I:%M %p").lstrip("0")
            tips.append("%s @ %s%s" % (g["away"], g["home"], when))
        L.append("- %s: %d games%s" % (day_label(date.fromisoformat(d)), len(gs), (" — " + "; ".join(tips)) if tips else ""))
    if not light:
        L.append("- None in the rest of this window.")
    L += ["", FOOTER]
    return "\n".join(L)


# ---------- commands ----------

def make_client_factory():
    holder = {}

    def factory():
        if "c" not in holder:
            key, _ = load_key()
            if not key:
                raise Fail("api_key_missing", "No BALLDONTLIE API key is set up yet.", KEY_HELP)
            holder["c"] = Client(key)
        return holder["c"]
    return factory


def cmd_grid(a) -> None:
    zone = resolve_tz(a.tz) if a.tz else _read_config().get("timezone") or "America/New_York"
    if not 1 <= a.slots <= 20:
        raise Fail("bad_slots", "Starting slots must be between 1 and 20.", "Example: --slots 10")
    today = league_today(datetime.now(timezone.utc))
    factory = make_client_factory()
    sched = load_schedule(factory, today)
    if a.teams:
        print(report_all_teams(sched, today))
        return
    roster = unmatched = notes = None
    if a.roster:
        names = [n for n in a.roster.replace("\n", ",").split(",") if n.strip()][:30]
        if len(names) > 1:
            print("Looking up %d names (up to %d seconds on the free plan; names looked up before are cached)..."
                  % (len(names), int(len(names) * MIN_INTERVAL_S)), file=sys.stderr)
        playing = set(team_days(sched["games"]))
        roster, unmatched, notes = match_roster(factory, names, playing)
    print(report(sched, today, zone, roster, a.slots, unmatched or (), notes or ()))


def cmd_settings(a) -> None:
    cfg = _read_config()
    if a.tz:
        cfg["timezone"] = resolve_tz(a.tz)
        _write_config(cfg)
    print("SETTINGS: timezone=%s" % cfg.get("timezone", "America/New_York"))


def cmd_setkey(a) -> None:
    if sys.stdin.isatty():
        import getpass
        key = getpass.getpass("Paste your BALLDONTLIE API key (input hidden): ")
    else:
        key = sys.stdin.readline()
    save_key(key)
    print("KEY SAVED to %s (only this computer; it is sent only to api.balldontlie.io)." % CONFIG)


def cmd_status(a) -> None:
    key, src = load_key()
    print("API key: %s" % ("set (%s)" % src if key else "not set. " + KEY_HELP))
    sched = read_cache("schedule.json", 10 ** 6)
    print("Schedule cache: %s" % (sched["fetched"] if sched else "none yet"))
    print("Time zone: %s" % _read_config().get("timezone", "America/New_York"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Free NBA weekly games grid & back-to-back finder")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("grid")
    p.add_argument("--roster", help='comma-separated names, e.g. "Victor Wembanyama, Luka Doncic"')
    p.add_argument("--slots", type=int, default=DEFAULT_SLOTS, help="daily starting slots (default 10)")
    p.add_argument("--tz", help="city or IANA time zone for tip-off times (default: saved setting or US Eastern)")
    p.add_argument("--teams", choices=["all"], help="all: every team's games this week (Mon-Sun) instead of the grid")
    p = sub.add_parser("settings")
    p.add_argument("--tz")
    sub.add_parser("setkey")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    try:
        {"grid": cmd_grid, "settings": cmd_settings, "setkey": cmd_setkey, "status": cmd_status}[a.cmd](a)
    except Fail as exc:
        print("ERROR [%s]: %s" % (exc.kind, exc), file=sys.stderr)
        print("HOW TO FIX: %s" % exc.fix, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
