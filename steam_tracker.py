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
    return results


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

    return {
        "owned": len(results),
        "with_ach": len(with_ach),
        "started": len(started),
        "perfect": sum(g["unlocked"] == g["total"] for g in started),
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
