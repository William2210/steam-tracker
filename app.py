"""
Steam Achievement Tracker - web dashboard

Serves the same numbers as steam_tracker.py as a web page. The page reads
straight from cache.json, so it loads instantly; refreshing from Steam runs
in the background while the page shows progress.

Run:   uvicorn app:app --reload
Open:  http://localhost:8000
"""

import threading
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

import steam_tracker as st

app = FastAPI(title="Steam Achievement Tracker")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

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
    games = [{**g, "appid": appid} for appid, g in st.load_cache().items()]
    return templates.TemplateResponse(request, "index.html", {
        "games": sorted(games, key=lambda g: g["name"].lower()),
        "summary": st.summarize(games) if games else None,
        "updated_at": max((g["fetched_at"] for g in games), default=None),
        "show_top": st.SHOW_TOP,
        "configured": bool(st.API_KEY and st.STEAM_ID),
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
