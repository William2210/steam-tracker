"""
Steam Achievement Tracker - web dashboard

Serves the same numbers as steam_tracker.py as a web page. The page reads
straight from cache.json, so it loads instantly; refreshing from Steam runs
in the background while the page shows progress.

Click any game to open /game/<appid>: its store info and full achievement
list, locked and unlocked, with global unlock rates.

From there, /game/<appid>/review shows your own IGN-style review, written in
the editor at /game/<appid>/review/edit and saved to reviews/<appid>.md.

Run:   uvicorn app:app --reload
Open:  http://localhost:8000
"""

import datetime
import threading
import time
from functools import lru_cache
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import reviews as rv
import steam_tracker as st

app = FastAPI(title="Steam Achievement Tracker")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# Screenshots uploaded from the review editor.
rv.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/review-images", StaticFiles(directory=rv.IMAGES_DIR), name="review-images")

# Only one refresh may run at a time; its progress is shared with /status.
refresh_lock = threading.Lock()
status = {"running": False, "done": 0, "total": 0, "current": "", "error": None}


def on_progress(done, total, name):
    status.update(done=done, total=total, current=name)


def run_refresh(full):
    try:
        steam_id = st.resolve_steam_id(st.STEAM_ID)
        st.collect(steam_id, refresh=full, on_progress=on_progress)
    except (Exception, SystemExit) as e:  # steam_tracker uses sys.exit for errors
        status["error"] = str(e) or type(e).__name__
    finally:
        status["running"] = False
        refresh_lock.release()


# ---------------------------------------------------------------- routes

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    # Cache keys are appids; keep them so the page can show game artwork.
    scores = st.load_metacritic()
    games = [{**g, "appid": appid, "metacritic": scores.get(appid)}
             for appid, g in st.load_cache().items()]
    return templates.TemplateResponse(request, "index.html", {
        "games": sorted(games, key=lambda g: g["name"].lower()),
        "summary": st.summarize(games) if games else None,
        "updated_at": max((g["fetched_at"] for g in games), default=None),
        "show_top": st.SHOW_TOP,
        "configured": bool(st.API_KEY and st.STEAM_ID),
        "review_scores": rv.all_scores(),
    })


@app.post("/refresh")
def refresh(background_tasks: BackgroundTasks, full: bool = False):
    """Start a refresh. full=true re-checks every game, ignoring the cache."""
    if not (st.API_KEY and st.STEAM_ID):
        return JSONResponse({"error": "STEAM_API_KEY / STEAM_ID missing from .env"},
                            status_code=400)
    if not refresh_lock.acquire(blocking=False):
        return JSONResponse(status, status_code=409)  # already running
    status.update(running=True, done=0, total=0, current="", error=None)
    background_tasks.add_task(run_refresh, full)
    return status


@app.get("/status")
def get_status():
    return status


# ---------------------------------------------------------------- game page

# Each game page costs 4 Steam calls, so keep results for a few minutes.
# Add ?fresh=1 to the URL (or press Reload on the page) to skip this.
GAME_CACHE_SECONDS = 300
game_cache = {}  # appid -> (fetched_at, detail)


@lru_cache(maxsize=1)
def my_steam_id():
    return st.resolve_steam_id(st.STEAM_ID)  # only hits Steam for vanity names


@app.get("/game/{appid}", response_class=HTMLResponse)
def game_page(request: Request, appid: int, fresh: bool = False):
    entry = st.load_cache().get(str(appid), {})
    detail, error = None, None

    hit = game_cache.get(appid)
    if hit and not fresh and time.time() - hit[0] < GAME_CACHE_SECONDS:
        detail = hit[1]
    elif not (st.API_KEY and st.STEAM_ID):
        error = "STEAM_API_KEY or STEAM_ID is missing from .env."
    else:
        try:
            detail = st.get_game_detail(my_steam_id(), appid)
            game_cache[appid] = (time.time(), detail)
        except (Exception, SystemExit) as e:
            error = str(e) or type(e).__name__

    store = (detail or {}).get("store") or {}
    return templates.TemplateResponse(request, "game.html", {
        "appid": appid,
        "name": store.get("name") or entry.get("name") or f"App {appid}",
        "playtime_hours": entry.get("playtime_hours"),
        "store": store,
        "detail": detail,
        "error": error,
        "review": rv.load(appid),
    })


# ---------------------------------------------------------------- reviews

def game_basics(appid):
    """Name, hours and completion % for a game, from whatever we already have."""
    entry = st.load_cache().get(str(appid), {})
    hit = game_cache.get(appid)
    detail = hit[1] if hit else {}
    name = (detail.get("store") or {}).get("name") or entry.get("name") or f"App {appid}"
    unlocked = detail.get("unlocked", entry.get("unlocked", 0))
    total = detail.get("total", entry.get("total", 0))
    return {
        "name": name,
        "hours": entry.get("playtime_hours"),
        "completion": round(100 * unlocked / total) if total else None,
    }


@app.get("/game/{appid}/review", response_class=HTMLResponse)
def review_page(request: Request, appid: int):
    return templates.TemplateResponse(request, "review.html", {
        "appid": appid, **game_basics(appid), "review": rv.load(appid),
    })


@app.get("/game/{appid}/review/edit", response_class=HTMLResponse)
def review_edit(request: Request, appid: int):
    game = game_basics(appid)
    review = rv.load(appid) or {
        "title": f"{game['name']} Review",
        "platform": "PC",
        "reviewed": datetime.date.today().isoformat(),
        "pros": [], "cons": [], "body": "",
    }
    return templates.TemplateResponse(request, "review_edit.html", {
        "appid": appid, **game, "review": review,
        "verdicts": rv.VERDICTS, "is_new": not rv.path_for(appid).exists(),
    })


@app.post("/game/{appid}/review")
async def review_save(request: Request, appid: int):
    data = await request.json()
    try:
        score = float(data.get("score"))
        assert 1 <= score <= 10
    except (TypeError, ValueError, AssertionError):
        return JSONResponse({"error": "Score must be a number from 1 to 10."}, status_code=400)
    data["score"] = round(score * 2) / 2  # allow half points, like 8.5
    if not str(data.get("title", "")).strip():
        return JSONResponse({"error": "Give the review a title."}, status_code=400)

    # Snapshot your hours and completion at the time of the review, unless
    # it already has them (so later edits don't overwrite the original).
    game = game_basics(appid)
    old = rv.load(appid) or {}
    for key in ("hours", "completion"):
        data[key] = old.get(key) if old.get(key) is not None else game[key]

    rv.save(appid, data)
    return {"ok": True, "url": f"/game/{appid}/review"}


@app.post("/review/preview")
async def review_preview(request: Request):
    """Render Markdown for the editor's Preview tab."""
    body = (await request.json()).get("body", "")
    return {"html": rv.render(body)}


@app.delete("/game/{appid}/review")
def review_delete(appid: int):
    rv.delete(appid)
    return {"ok": True, "url": f"/game/{appid}"}


@app.post("/game/{appid}/review/image")
async def review_image(request: Request, appid: int, name: str):
    """Upload a screenshot. The browser sends the raw file as the body."""
    try:
        url = rv.save_image(appid, name, await request.body())
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"url": url}
