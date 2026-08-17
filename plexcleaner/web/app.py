"""FastAPI application: pages, JSON API, background scan runner."""
from __future__ import annotations

import json
import logging
import secrets
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..actions import Executor, PlanError, build_plan, cancel_plan
from ..config import Config
from ..db import Database
from ..scan import Scanner, Services
from .security import COOKIE, NetworkGuard, SessionAuth, issue_csrf

log = logging.getLogger(__name__)

HERE = Path(__file__).parent


class ScanRunner:
    """Runs one scan at a time in a worker thread and exposes its state."""

    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.state: dict[str, Any] = {"running": False, "message": "idle", "scan_id": None, "error": None}

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, trigger: str = "manual") -> bool:
        with self._lock:
            if self.running:
                return False
            self.state = {"running": True, "message": "scanning…", "scan_id": None, "error": None}
            self._thread = threading.Thread(target=self._run, args=(trigger,), daemon=True)
            self._thread.start()
            return True

    def _run(self, trigger: str) -> None:
        services = Services(self.cfg)
        try:
            scan_id = Scanner(self.cfg, self.db, services).run(trigger)
            self.state = {"running": False, "message": "complete", "scan_id": scan_id, "error": None}
        except Exception as exc:
            log.exception("scan failed")
            self.state = {"running": False, "message": "failed", "scan_id": None, "error": str(exc)}
        finally:
            services.close()


def create_app(cfg: Config, db: Database | None = None) -> FastAPI:
    db = db or Database(cfg.db_path)
    app = FastAPI(title="PlexCleaner", version=__version__, docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # One key for both the middleware instance and the helper used by /login,
    # so a config with no secret_key still produces cookies both sides accept.
    secret_key = cfg.app.secret_key or secrets.token_hex(32)
    max_age = cfg.app.session_hours * 3600
    auth = SessionAuth(app, secret_key, cfg.app.password, max_age)
    app.add_middleware(SessionAuth, secret_key=secret_key, password=cfg.app.password,
                       max_age_seconds=max_age)
    # Added last so it runs first: origin is checked before anything else.
    app.add_middleware(NetworkGuard, allowed_networks=cfg.allowed_cidrs())

    runner = ScanRunner(cfg, db)
    app.state.cfg = cfg
    app.state.db = db
    app.state.runner = runner

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        scan = db.latest_scan()
        base = {
            "request": request,
            "cfg": cfg,
            "version": __version__,
            "dry_run": cfg.safety.dry_run,
            "confirm_phrase": cfg.safety.confirm_phrase,
            "scan": scan,
            "scan_stats": json.loads(scan["stats"]) if scan else {},
            "auth_enabled": bool(cfg.app.password),
        }
        response = templates.TemplateResponse(name, {**base, **ctx})
        if not request.cookies.get("plexcleaner_csrf"):
            issue_csrf(response)
        return response

    # -- auth ------------------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, error: str = ""):
        if not cfg.app.password:
            return RedirectResponse("/", status_code=302)
        return templates.TemplateResponse("login.html", {"request": request, "error": error,
                                                         "version": __version__})

    @app.post("/login")
    def login(request: Request, password: str = Form("")):
        if not auth.check_password(password):
            return RedirectResponse("/login?error=Incorrect+password", status_code=302)
        response = RedirectResponse("/", status_code=302)
        response.set_cookie(COOKIE, auth.make_token(), httponly=True, samesite="strict", path="/")
        issue_csrf(response)
        return response

    @app.post("/logout")
    def logout():
        response = RedirectResponse("/login", status_code=302)
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__}

    # -- pages -----------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        problems = cfg.validate()
        recent_plans = db.query("SELECT * FROM plan ORDER BY id DESC LIMIT 10")
        return page(request, "dashboard.html", problems=problems, recent_plans=recent_plans,
                    runner=runner.state)

    @app.get("/media", response_class=HTMLResponse)
    def media_page(request: Request):
        return page(request, "media.html")

    @app.get("/users", response_class=HTMLResponse)
    def users_page(request: Request):
        return page(request, "users.html")

    @app.get("/plans/{plan_id}", response_class=HTMLResponse)
    def plan_page(request: Request, plan_id: int):
        plan = db.query_one("SELECT * FROM plan WHERE id = ?", (plan_id,))
        if not plan:
            return HTMLResponse("Plan not found", status_code=404)
        items = db.query("SELECT * FROM plan_item WHERE plan_id = ? ORDER BY kind, id", (plan_id,))
        parsed = [{
            "id": r["id"], "kind": r["kind"], "title": r["title"], "ref": r["ref"],
            "size_gb": round(r["size_bytes"] / 1_000_000_000, 2), "status": r["status"],
            "steps": json.loads(r["steps"] or "[]"), "result": json.loads(r["result"] or "{}"),
        } for r in items]
        return page(request, "plan.html", plan=plan, plan_summary=json.loads(plan["summary"] or "{}"),
                    items=parsed)

    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request, limit: int = 300):
        rows = db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (min(limit, 2000),))
        entries = [{**dict(r), "detail": json.loads(r["detail"] or "{}")} for r in rows]
        return page(request, "audit.html", entries=entries)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        protections = [dict(r) for r in db.protections()]
        return page(request, "settings.html", cfg=cfg, problems=cfg.validate(),
                    protections=protections, config_path=cfg.source_path)

    # -- JSON API --------------------------------------------------------
    @app.get("/api/status")
    def api_status():
        scan = db.latest_scan()
        return {
            "version": __version__,
            "dry_run": cfg.safety.dry_run,
            "runner": runner.state,
            "scan": dict(scan) if scan else None,
            "stats": json.loads(scan["stats"]) if scan else {},
        }

    @app.post("/api/scan")
    def api_scan():
        if not runner.start("manual"):
            return JSONResponse({"error": "a scan is already running"}, status_code=409)
        return {"started": True}

    @app.get("/api/test-connections")
    def api_test():
        services = Services(cfg)
        try:
            return {"results": services.test_all()}
        finally:
            services.close()

    @app.get("/api/media")
    def api_media(verdict: str = "candidate", kind: str = "", library: str = "",
                  search: str = "", sort: str = "size", limit: int = 500, offset: int = 0):
        scan = db.latest_scan()
        if not scan:
            return {"items": [], "total": 0, "scan_id": None}

        where = ["scan_id = ?"]
        params: list[Any] = [scan["id"]]
        if verdict and verdict != "all":
            where.append("verdict = ?")
            params.append(verdict)
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if library:
            where.append("plex_library = ?")
            params.append(library)
        if search:
            where.append("title LIKE ?")
            params.append(f"%{search}%")

        order = {
            "size": "size_bytes DESC",
            "title": "title ASC",
            "watched": "last_watched_at ASC",
            "added": "added_at ASC",
        }.get(sort, "size_bytes DESC")

        clause = " AND ".join(where)
        total = db.query_one(f"SELECT COUNT(*) AS c FROM media_item WHERE {clause}", tuple(params))
        rows = db.query(
            f"SELECT * FROM media_item WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, min(limit, 2000), offset),
        )
        return {
            "scan_id": scan["id"],
            "total": total["c"] if total else 0,
            "items": [_media_json(r) for r in rows],
            "libraries": [r["plex_library"] for r in db.query(
                "SELECT DISTINCT plex_library FROM media_item WHERE scan_id = ? AND plex_library IS NOT NULL"
                " ORDER BY plex_library", (scan["id"],))],
        }

    @app.get("/api/users")
    def api_users(verdict: str = "candidate"):
        scan = db.latest_scan()
        if not scan:
            return {"items": [], "scan_id": None}
        if verdict and verdict != "all":
            rows = db.query("SELECT * FROM user_account WHERE scan_id = ? AND verdict = ? ORDER BY last_seen_at ASC",
                            (scan["id"], verdict))
        else:
            rows = db.query("SELECT * FROM user_account WHERE scan_id = ? ORDER BY last_seen_at ASC",
                            (scan["id"],))
        return {"scan_id": scan["id"], "items": [_user_json(r) for r in rows]}

    @app.post("/api/plans")
    async def api_create_plan(request: Request):
        body = await request.json()
        scan = db.latest_scan()
        if not scan:
            return JSONResponse({"error": "no completed scan yet"}, status_code=400)
        try:
            plan_id = build_plan(
                db, cfg,
                scan_id=int(body.get("scan_id") or scan["id"]),
                media_ids=[int(i) for i in body.get("media_ids", [])],
                user_ids=[int(i) for i in body.get("user_ids", [])],
            )
        except PlanError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"plan_id": plan_id}

    @app.post("/api/plans/{plan_id}/execute")
    async def api_execute(plan_id: int, request: Request):
        body = await request.json()
        executor = Executor(cfg, db)
        try:
            results = executor.execute(
                plan_id,
                confirm=str(body.get("confirm", "")),
                force_live=bool(body.get("live", False)),
            )
        except PlanError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        finally:
            executor.services.close()
        return results

    @app.post("/api/plans/{plan_id}/cancel")
    def api_cancel(plan_id: int):
        cancel_plan(db, plan_id)
        return {"cancelled": plan_id}

    @app.post("/api/protections")
    async def api_add_protection(request: Request):
        body = await request.json()
        kind, ref = str(body.get("kind", "")), str(body.get("ref", ""))
        if kind not in ("media", "user") or not ref:
            return JSONResponse({"error": "kind must be media or user, and ref is required"}, status_code=400)
        db.add_protection(kind, ref, label=str(body.get("label", "")), note=str(body.get("note", "")))
        db.audit(service="protection", action="add", target=f"{kind}:{ref}", dry_run=True, ok=True)
        return {"protected": ref}

    @app.delete("/api/protections")
    async def api_remove_protection(request: Request):
        body = await request.json()
        db.remove_protection(str(body.get("kind", "")), str(body.get("ref", "")))
        return {"removed": body.get("ref")}

    return app


def _media_json(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "title": row["title"],
        "year": row["year"],
        "library": row["plex_library"],
        "size_gb": round((row["size_bytes"] or 0) / 1_000_000_000, 2),
        "added_at": row["added_at"],
        "last_watched_at": row["last_watched_at"],
        "play_count": row["play_count"],
        "watcher_count": row["watcher_count"],
        "episode_count": row["episode_count"],
        "arr": f"{row['arr_instance']}#{row['arr_id']}" if row["arr_instance"] else None,
        "arr_status": row["arr_status"],
        "seerr_media_id": row["seerr_media_id"],
        "seerr_requested_by": row["seerr_requested_by"],
        "verdict": row["verdict"],
        "reasons": json.loads(row["reasons"] or "[]"),
        "ref": json.loads(row["detail"] or "{}").get("ref", ""),
    }


def _user_json(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "email": row["email"],
        "is_admin": bool(row["is_admin"]),
        "is_home": bool(row["is_home"]),
        "last_seen_at": row["last_seen_at"],
        "last_login_at": row["last_login_at"],
        "plays": row["plays"],
        "request_count": row["request_count"],
        "seerr_user_id": row["seerr_user_id"],
        "tautulli_user_id": row["tautulli_user_id"],
        "verdict": row["verdict"],
        "reasons": json.loads(row["reasons"] or "[]"),
        "ref": json.loads(row["detail"] or "{}").get("ref", ""),
    }
