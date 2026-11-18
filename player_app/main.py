"""Rally player app: the phone-first, player-facing side (docs/PLAYER_APP_ARCHITECTURE.md).

Run:  uvicorn player_app.main:app --port 8600   (from the repo root, same runtime/ as the club dashboard)

It shares the club dashboard's engine and SQLite files, so a manager approval appears here as an invite
and a player's tap appears on the dashboard. No state lives in this process.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).resolve().parent.parent
try:  # same .env as the dashboard (ANTHROPIC_API_KEY)
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from rally.engine import RallyEngine  # noqa: E402
from rally.player_side import QUICK_REPLIES, PlayerError, PlayerSide  # noqa: E402

HERE = Path(__file__).resolve().parent
app = FastAPI(title="Rally · player app", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
DASHBOARD_URL = os.environ.get("RALLY_DASHBOARD_URL", "http://localhost:8517")

_lock = threading.RLock()  # one engine, many request threads: serialise engine access
_engine = RallyEngine(ROOT)
side = PlayerSide(_engine)


def _ensure() -> None:
    """The dashboard's Reset deletes runtime/. Reopen on the fresh files instead of failing."""
    if not _engine.storage.db_path.exists() or not _engine.storage.get_meta("sim_now"):
        _engine._open()


def _back(pid: str, path: str = "", msg: str = "", err: str = "") -> RedirectResponse:
    q = f"?m={quote(msg)}" if msg else f"?err={quote(err)}" if err else ""
    return RedirectResponse(f"/p/{pid}{path}{q}", status_code=303)


def _page(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.update(request=request, msg=request.query_params.get("m"), err=request.query_params.get("err"),
               dashboard_url=DASHBOARD_URL, live=_engine.llm.live, parser=_engine.llm.label)
    return templates.TemplateResponse(request, name, ctx)


@app.get("/", response_class=HTMLResponse)
def pick(request: Request):
    with _lock:
        _ensure()
        _engine.refresh()
        roster = side.roster()
    return _page(request, "pick.html", invited=[r for r in roster if r["active_invites"]],
                 organisers=[r for r in roster if r["organiser"]], everyone=roster)


@app.get("/p/{pid}", response_class=HTMLResponse)
def home(request: Request, pid: str):
    with _lock:
        _ensure()
        try:
            data = side.home(pid)
        except PlayerError:
            return RedirectResponse("/", status_code=303)
        version = side.version(pid)
    return _page(request, "home.html", pid=pid, tab="home", version=version, quick=QUICK_REPLIES, **data)


@app.post("/p/{pid}/reply/{invite_id}")
def reply(pid: str, invite_id: str, text: str = Form("")):
    with _lock:
        _ensure()
        try:
            side.reply(pid, invite_id, text)
        except (PlayerError, ValueError) as exc:
            return _back(pid, err=str(exc))
    return _back(pid, msg="Reply sent")


@app.post("/p/{pid}/pay/{invite_id}")
def pay(pid: str, invite_id: str):
    with _lock:
        _ensure()
        try:
            msg = side.pay(pid, invite_id)
        except (PlayerError, ValueError) as exc:
            return _back(pid, err=str(exc))
    return _back(pid, msg=msg)


@app.post("/p/{pid}/organise/{match_id}/decide")
def organiser_decide(pid: str, match_id: str, mode: str = Form(...)):
    with _lock:
        _ensure()
        try:
            side.organiser_decide(pid, match_id, mode)
        except (PlayerError, ValueError) as exc:
            return _back(pid, err=str(exc))
    if mode == "rally":
        return _back(pid, f"/organise/{match_id}")
    return _back(pid, msg="Got it. Rally will stay out of this one.")


@app.get("/p/{pid}/organise/{match_id}", response_class=HTMLResponse)
def organiser_choose(request: Request, pid: str, match_id: str):
    with _lock:
        _ensure()
        try:
            data = side.home(pid)
            card = next((c for c in data["organiser_cards"] if c.match_id == match_id), None)
            if card is None:
                raise PlayerError("That isn't one of your games.")
            if card.stage != "choose":
                return _back(pid)
            candidates = side.organiser_candidates(pid, match_id)
        except PlayerError as exc:
            return _back(pid, err=str(exc))
        version = side.version(pid)
    return _page(request, "organise.html", pid=pid, tab="home", version=version, card=card, candidates=candidates,
                 **{k: data[k] for k in ("profile", "short_name", "club", "now_label", "provider_ok")})


@app.post("/p/{pid}/organise/{match_id}/approve")
async def organiser_approve(request: Request, pid: str, match_id: str):
    form = await request.form()
    ids = form.getlist("player")
    with _lock:
        _ensure()
        try:
            side.organiser_approve(pid, match_id, list(ids))
        except (PlayerError, ValueError) as exc:
            return _back(pid, f"/organise/{match_id}", err=str(exc))
    return _back(pid, msg=f"Rally is asking {len(ids)} player{'s' if len(ids) != 1 else ''}, one at a time")


@app.get("/p/{pid}/settings", response_class=HTMLResponse)
def settings(request: Request, pid: str):
    with _lock:
        _ensure()
        try:
            data = side.home(pid)
            s = side.settings(pid)
        except PlayerError:
            return RedirectResponse("/", status_code=303)
        version = side.version(pid)
    return _page(request, "settings.html", pid=pid, tab="settings", version=version, s=s,
                 **{k: data[k] for k in ("profile", "short_name", "club", "now_label", "provider_ok")})


@app.post("/p/{pid}/settings")
async def save_settings(request: Request, pid: str):
    form = await request.form()
    with _lock:
        _ensure()
        try:
            side.save_settings(pid, list(form.getlist("band")), str(form.get("channel", "")),
                               int(form.get("max_per_week", "0") or 0))
        except (PlayerError, ValueError) as exc:
            return _back(pid, "/settings", err=str(exc))
    return _back(pid, "/settings", msg="Saved")


@app.post("/p/{pid}/opt")
def opt(pid: str, on: str = Form(...)):
    with _lock:
        _ensure()
        try:
            side.set_opt_in(pid, on == "1")
        except (PlayerError, ValueError) as exc:
            return _back(pid, "/settings", err=str(exc))
    return _back(pid, "/settings", msg="Invites are on" if on == "1" else "Invites are off. You won't get any more.")


@app.post("/p/{pid}/free")
def post_free(pid: str, day: str = Form(...), start: int = Form(...), end: int = Form(...)):
    with _lock:
        _ensure()
        try:
            side.post_free(pid, day, start, end)
        except (PlayerError, ValueError) as exc:
            return _back(pid, err=str(exc))
    return _back(pid, msg="Got it. Here's what fits, and you'll move up the list for games that come up.")


@app.post("/p/{pid}/free/{avail_id}/cancel")
def cancel_free(pid: str, avail_id: int):
    with _lock:
        _ensure()
        try:
            side.cancel_free(pid, avail_id)
        except (PlayerError, ValueError) as exc:
            return _back(pid, err=str(exc))
    return _back(pid, msg="Cancelled.")


@app.post("/p/{pid}/join/{opp_id}")
def join(pid: str, opp_id: str):
    with _lock:
        _ensure()
        try:
            msg = side.self_join(pid, opp_id)
        except (PlayerError, ValueError) as exc:
            return _back(pid, err=str(exc))
    return _back(pid, msg=msg)


@app.get("/api/p/{pid}/version")
def version(pid: str):
    with _lock:
        _ensure()
        _engine.refresh()
        return JSONResponse({"version": side.version(pid)})
