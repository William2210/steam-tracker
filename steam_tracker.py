"""
Steam Achievement Tracker - v1 (command line)

Pulls every game you own from the Steam Web API, checks your achievements
for each one, and reports:
  - your average completion (the same way Steam calculates it)
  - total achievements unlocked across your whole library
  - perfect games
  - the games closest to 100%, so you know what to work on next

Results are cached in cache.json so re-runs are fast and only re-check
games after CACHE_MAX_AGE_HOURS. Use --refresh to force a full re-check.
"""

"""Notes for self in order to ensure application will run across devices...

To start the dashboard again, run uvicorn app:app --reload in the project folder and open http://localhost:8000
.env is never uploaded, so each device needs its own copy. Copy your key across by hand, not through Git."""

import html
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

API = "https://api.steampowered.com"
CACHE_FILE = Path("cache.json")
CACHE_MAX_AGE_HOURS = 12
SHOW_TOP = 20  # how many "closest to 100%" games to list

load_dotenv(override=True)  # .env wins over stale values VS Code injected
API_KEY = os.getenv("STEAM_API_KEY")
STEAM_ID = os.getenv("STEAM_ID")


# ---------------------------------------------------------------- API calls

def steam_get(path, **params):
    """Make a GET request to the Steam Web API with your key attached."""
    params["key"] = API_KEY
    return requests.get(f"{API}/{path}", params=params, timeout=15)


def resolve_steam_id(value):
    """Accept either a 17-digit SteamID64 or your custom profile URL name."""
    if value.isdigit() and len(value) == 17:
        return value
    r = steam_get("ISteamUser/ResolveVanityURL/v1/", vanityurl=value)
    r.raise_for_status()
    data = r.json()["response"]
    if data.get("success") != 1:
        sys.exit(f"Couldn't find a Steam profile called '{value}'.")
    return data["steamid"]


def get_owned_games(steam_id):
    """Return a list of {appid, name, playtime_forever, ...} for every owned game."""
    r = steam_get(
        "IPlayerService/GetOwnedGames/v1/",
        steamid=steam_id,
        include_appinfo=1,
        include_played_free_games=1,
    )
    r.raise_for_status()
    games = r.json()["response"].get("games", [])
    if not games:
        sys.exit("No games came back. Check your profile AND 'Game details' "
                 "are set to Public in Steam's privacy settings.")
    return games


def get_achievements(steam_id, appid):
    """Return (unlocked, total) for one game, or (0, 0) if it has no achievements."""
    r = steam_get(
        "ISteamUserStats/GetPlayerAchievements/v1/",
        steamid=steam_id,
        appid=appid,
    )
    if r.status_code == 403:
        sys.exit("Steam refused to share achievements. Set 'Game details' "
                 "to Public in your Steam privacy settings.")
    if r.status_code != 200:
        return 0, 0  # usually "Requested app has no stats"

    stats = r.json().get("playerstats", {})
    achievements = stats.get("achievements") or []
    unlocked = sum(a["achieved"] for a in achievements)
    return unlocked, len(achievements)


# ---------------------------------------------------------------- one game in detail

STORE_API = "https://store.steampowered.com/api/appdetails"


def get_store_info(appid):
    """Description, developer, genres etc. from the Steam store.

    This is the store's own (keyless) endpoint, so the API key isn't sent.
    Returns {} if the game has no store page (delisted, tools, etc.).
    """
    try:
        r = requests.get(STORE_API, params={"appids": appid, "l": "english"}, timeout=10)
        entry = r.json().get(str(appid)) or {}
    except (requests.RequestException, ValueError):
        return {}
    if not entry.get("success"):
        return {}
    d = entry["data"]
    return {
        "name": d.get("name"),
        "description": html.unescape(d.get("short_description") or ""),
        "developers": d.get("developers") or [],
        "publishers": d.get("publishers") or [],
        "release_date": (d.get("release_date") or {}).get("date"),
        "genres": [g["description"] for g in d.get("genres") or []],
        "header_image": d.get("header_image"),
    }


class StoreRateLimited(Exception):
    """The store API said slow down (HTTP 429)."""


def get_metacritic(appid):
    """Metacritic score for one game, or None if it doesn't have one.

    Raises StoreRateLimited when the store wants us to back off, and
    requests.RequestException on network trouble, so neither gets cached as
    "no score".
    """
    r = requests.get(STORE_API, params={"appids": appid, "l": "english"}, timeout=10)
    if r.status_code == 429:
        raise StoreRateLimited()
    r.raise_for_status()
    entry = r.json().get(str(appid)) or {}
    if not entry.get("success"):
        return None
    return (entry["data"].get("metacritic") or {}).get("score")


def get_game_detail(steam_id, appid):
    """Everything the game page shows: store info plus every achievement,
    whether you've unlocked it (and when), and what % of players have it."""
    appid = str(appid)

    # 1. The game's achievement list: display names, descriptions, icons.
    r = steam_get("ISteamUserStats/GetSchemaForGame/v2/", appid=appid, l="english")
    schema = []
    if r.status_code == 200:
        schema = ((r.json().get("game") or {}).get("availableGameStats") or {}) \
            .get("achievements") or []

    # 2. Your progress on each one.
    r = steam_get("ISteamUserStats/GetPlayerAchievements/v1/",
                  steamid=steam_id, appid=appid, l="english")
    if r.status_code == 403:
        raise RuntimeError("Steam refused to share achievements. Set 'Game details' "
                           "to Public in your Steam privacy settings.")
    mine = {}
    if r.status_code == 200:
        for a in (r.json().get("playerstats") or {}).get("achievements") or []:
            mine[a["apiname"]] = a

    # 3. Global unlock rates, so you can spot the easy ones.
    r = steam_get("ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/", gameid=appid)
    rarity = {}
    if r.status_code == 200:
        for a in (r.json().get("achievementpercentages") or {}).get("achievements") or []:
            rarity[a["name"]] = float(a["percent"])

    # If the schema call failed, fall back to the names in your own progress.
    if not schema:
        schema = [{"name": k, "displayName": v.get("name", k),
                   "description": v.get("description", "")} for k, v in mine.items()]

    achievements = []
    for a in schema:
        p = mine.get(a["name"], {})
        unlocked = bool(p.get("achieved"))
        achievements.append({
            "name": a.get("displayName") or a["name"],
            # Hidden achievements leave description out of the schema;
            # your own progress call usually still has it.
            "description": a.get("description") or p.get("description") or "",
            "hidden": bool(a.get("hidden")),
            "unlocked": unlocked,
            "unlocktime": p.get("unlocktime", 0) if unlocked else 0,
            "icon": a.get("icon") if unlocked else (a.get("icongray") or a.get("icon")),
            "global_pct": rarity.get(a["name"]),
        })

    return {
        "appid": appid,
        "store": get_store_info(appid),
        "achievements": achievements,
        "unlocked": sum(a["unlocked"] for a in achievements),
        "total": len(achievements),
    }


# ---------------------------------------------------------------- caching

def load_cache():
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return {}


def save_cache(cache):
    CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def is_fresh(entry):
    age_hours = (time.time() - entry["fetched_at"]) / 3600
    return age_hours < CACHE_MAX_AGE_HOURS


# Review scores don't change, so they live in their own file that a full
# re-check leaves alone. appid -> score, or null for "checked, has none".
METACRITIC_FILE = Path("metacritic.json")
STORE_DELAY = 1.5  # the store allows roughly 200 requests per 5 minutes


def load_metacritic():
    if METACRITIC_FILE.exists():
        return json.loads(METACRITIC_FILE.read_text(encoding="utf-8"))
    return {}


def save_metacritic(scores):
    METACRITIC_FILE.write_text(json.dumps(scores, indent=2), encoding="utf-8")


def fill_metacritic(games, on_progress=None):
    """Look up Metacritic scores for any games we haven't checked yet.

    Stops early if the store rate-limits us; the rest are picked up on the
    next run.
    """
    scores = load_metacritic()
    missing = [g for g in games if str(g["appid"]) not in scores]
    try:
        for i, game in enumerate(missing, start=1):
            appid = str(game["appid"])
            name = game.get("name", f"App {appid}")
            print(f"\rMetacritic {i}/{len(missing)}: {name[:50]:<50}", end="", flush=True)
            if on_progress:
                on_progress(i, len(missing), f"Metacritic score for {name}")
            try:
                scores[appid] = get_metacritic(appid)
            except requests.RequestException:
                pass  # try again next run
            time.sleep(STORE_DELAY)
    except StoreRateLimited:
        print("\rStore rate limit hit; remaining Metacritic scores will be fetched next run.")
    finally:
        save_metacritic(scores)
    print("\r" + " " * 80 + "\r", end="")
    return scores


# ---------------------------------------------------------------- main logic

def collect(steam_id, refresh=False, on_progress=None):
    """Fetch achievement data for every owned game, using the cache where possible.

    on_progress, if given, is called as on_progress(done, total, name) per game.
    """
    games = get_owned_games(steam_id)
    cache = {} if refresh else load_cache()
    results = []

    for i, game in enumerate(games, start=1):
        appid = str(game["appid"])
        name = game.get("name", f"App {appid}")
        entry = cache.get(appid)

        if entry is None or not is_fresh(entry):
            print(f"\rChecking {i}/{len(games)}: {name[:50]:<50}", end="", flush=True)
            unlocked, total = get_achievements(steam_id, appid)
            entry = {
                "name": name,
                "unlocked": unlocked,
                "total": total,
                "playtime_hours": round(game.get("playtime_forever", 0) / 60, 1),
                "fetched_at": time.time(),
            }
            cache[appid] = entry
            time.sleep(0.1)  # be polite to Steam's servers

        results.append(entry)
        if on_progress:
            on_progress(i, len(games), name)

    print("\r" + " " * 80 + "\r", end="")
    save_cache(cache)

    scores = fill_metacritic(games, on_progress)
    return [{**entry, "metacritic": scores.get(str(game["appid"]))}
            for game, entry in zip(games, results)]


def pct(g):
    return 100 * g["unlocked"] / g["total"]


def summarize(results):
    """Crunch the numbers shared by the terminal report and the web dashboard."""
    with_ach = [g for g in results if g["total"] > 0]
    started = [g for g in with_ach if g["unlocked"] > 0]
    in_progress = [g for g in started if g["unlocked"] < g["total"]]

    # Steam's "Average game completion rate" only counts games where
    # you've unlocked at least one achievement.
    steam_avg = sum(pct(g) for g in started) / len(started) if started else 0

    # Closest to 100%: fewest achievements left first, then highest %.
    in_progress.sort(key=lambda g: (g["total"] - g["unlocked"], -pct(g)))

    # Perfect games, biggest achievement lists first: the most impressive to show off.
    perfect_games = sorted((g for g in started if g["unlocked"] == g["total"]),
                           key=lambda g: (-g["total"], g["name"].lower()))

    return {
        "owned": len(results),
        "with_ach": len(with_ach),
        "started": len(started),
        "perfect": len(perfect_games),
        "perfect_games": perfect_games,
        "untouched": len(with_ach) - len(started),
        "steam_avg": steam_avg,
        "total_unlocked": sum(g["unlocked"] for g in with_ach),
        "total_ach": sum(g["total"] for g in with_ach),
        "in_progress": in_progress,
    }


def report(results):
    s = summarize(results)
    total_unlocked, total_ach = s["total_unlocked"], s["total_ach"]
    in_progress = s["in_progress"]

    print("=" * 60)
    print("  STEAM ACHIEVEMENT SUMMARY")
    print("=" * 60)
    print(f"  Games owned:                 {s['owned']}")
    print(f"  Games with achievements:     {s['with_ach']}")
    print(f"  Started (1+ unlocked):       {s['started']}")
    print(f"  Perfect games:               {s['perfect']}")
    print(f"  Not started yet:             {s['untouched']}")
    print()
    print(f"  Average completion (Steam):  {s['steam_avg']:.1f}%")
    print(f"  All achievements unlocked:   {total_unlocked}/{total_ach} "
          f"({100 * total_unlocked / total_ach:.1f}%)" if total_ach else "")
    print()

    print(f"  CLOSEST TO 100% (top {min(SHOW_TOP, len(in_progress))})")
    print("-" * 60)
    print(f"  {'Game':<34}{'Done':>8}{'%':>7}{'Left':>6}")
    for g in in_progress[:SHOW_TOP]:
        done = f"{g['unlocked']}/{g['total']}"
        print(f"  {g['name'][:33]:<34}{done:>8}{pct(g):>6.0f}%"
              f"{g['total'] - g['unlocked']:>6}")
    print("=" * 60)


def main():
    if not API_KEY or not STEAM_ID:
        sys.exit("Missing STEAM_API_KEY or STEAM_ID. Copy .env.example to .env "
                 "and fill them in.")
    refresh = "--refresh" in sys.argv
    steam_id = resolve_steam_id(STEAM_ID)
    results = collect(steam_id, refresh=refresh)
    report(results)


if __name__ == "__main__":
    main()
