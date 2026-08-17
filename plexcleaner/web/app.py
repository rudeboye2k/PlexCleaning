"""FastAPI application: pages, JSON API, settings portal, background scan runner.

Every route reads the effective config from the settings store rather than from
a value captured at startup, so saving on the Settings page takes effect on the
next request — no container restart.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..actions import Executor, PlanError, build_plan, cancel_plan
from ..clients import ClientError, PlexClient, SeerrClient, TautulliClient
from ..clients.arr import RadarrClient, SonarrClient
from ..config import Config, ConfigError
from ..db import Database
from ..scan import Scanner, Services
from ..schema import schema_json
from ..settings_store import SettingsStore
from .security import COOKIE, CSRF_COOKIE, NetworkGuard, SessionAuth, issue_csrf

log = logging.getLogger(__name__)

HERE = Path(__file__).parent


class ScanRunner:
    """Runs one scan at a time in a worker thread and exposes its state."""

    def __init__(self, store: SettingsStore, db: Database):
        self.store = store
        self.db = db
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.state: dict[str, Any] = {"running": False, "message": "idle", "scan_id": None,
                                      "error": None}

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
        cfg = self.store.current()
        services = Services(cfg)
        try:
            scan_id = Scanner(cfg, self.db, services).run(trigger)
            self.state = {"running": False, "message": "complete", "scan_id": scan_id, "error": None}
        except Exception as exc:
            log.exception("scan failed")
            self.state = {"running": False, "message": "failed", "scan_id": None, "error": str(exc)}
        finally:
            services.close()


def _probe(kind: str, payload: dict[str, Any], cfg: Config) -> dict[str, Any]:
    """Test one service with values that may not be saved yet.

    The wizard calls this as you type, so it falls back to the stored credential
    when the form sent the redaction sentinel instead of a real key.
    """
    from ..config import REDACTED

    url = str(payload.get("url") or "").strip()
    secret = str(payload.get("api_key") or payload.get("token") or "")
    name = str(payload.get("name") or kind)

    stored = {"plex": cfg.plex, "tautulli": cfg.tautulli, "seerr": cfg.seerr}.get(kind)
    if kind in ("sonarr", "radarr"):
        stored = cfg.arr_by_name(name)
    if (not secret or secret == REDACTED) and stored is not None:
        secret = getattr(stored, "token", "") or getattr(stored, "api_key", "")
    if not url and stored is not None:
        url = getattr(stored, "url", "")

    if not url or not secret:
        return {"name": name, "ok": False, "detail": "Both a URL and an API key are needed."}

    verify = bool(payload.get("verify_ssl", False))
    timeout = int(payload.get("timeout") or 20)
    builders = {
        "plex": lambda: PlexClient(url, secret, timeout=timeout, verify_ssl=verify),
        "tautulli": lambda: TautulliClient(url, secret, timeout=timeout, verify_ssl=verify),
        "seerr": lambda: SeerrClient(url, secret, timeout=timeout, verify_ssl=verify),
        "sonarr": lambda: SonarrClient(name, url, secret, timeout=timeout, verify_ssl=verify),
        "radarr": lambda: RadarrClient(name, url, secret, timeout=timeout, verify_ssl=verify),
    }
    if kind not in builders:
        return {"name": kind, "ok": False, "detail": f"Unknown service '{kind}'."}

    client = builders[kind]()
    try:
        return client.test().to_dict() | {"name": name}
    finally:
        client.close()


def create_app(store: SettingsStore, db: Database) -> FastAPI:
    app = FastAPI(title="PlexCleaner", version=__version__, docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    auth = SessionAuth(app, store)
    app.add_middleware(SessionAuth, store=store)
    # Added last so it runs first: origin is checked before anything else.
    app.add_middleware(NetworkGuard, store=store)

    runner = ScanRunner(store, db)
    app.state.store = store
    app.state.db = db
    app.state.runner = runner

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        cfg = store.current()
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
            "configured": cfg.configured,
            "safety_locked": cfg.safety_locked,
        }
        response = templates.TemplateResponse(name, {**base, **ctx})
        if not request.cookies.get(CSRF_COOKIE):
            issue_csrf(response)
        return response

    def guard_setup(request: Request):
        """Send a fresh install to the wizard instead of an empty dashboard."""
        if store.current().configured:
            return None
        if request.url.path.startswith(("/setup", "/api/", "/static", "/login")):
            return None
        return RedirectResponse("/setup", status_code=302)

    # -- auth ------------------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, error: str = ""):
        if not store.current().app.password:
            return RedirectResponse("/", status_code=302)
        return templates.TemplateResponse("login.html", {"request": request, "error": error,
                                                         "version": __version__})

    @app.post("/login")
    def login(password: str = Form("")):
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
        cfg = store.current()
        return {"ok": True, "version": __version__, "configured": cfg.configured,
                "dry_run": cfg.safety.dry_run}

    # -- pages -----------------------------------------------------------
    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request):
        return page(request, "setup.html")

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        redirect = guard_setup(request)
        if redirect:
            return redirect
        cfg = store.current()
        return page(request, "dashboard.html", problems=cfg.problems(), warnings=cfg.warnings(),
                    recent_plans=db.query("SELECT * FROM plan ORDER BY id DESC LIMIT 10"),
                    runner=runner.state)

    @app.get("/media", response_class=HTMLResponse)
    def media_page(request: Request):
        return guard_setup(request) or page(request, "media.html")

    @app.get("/users", response_class=HTMLResponse)
    def users_page(request: Request):
        return guard_setup(request) or page(request, "users.html")

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        return page(request, "settings.html",
                    protections=[dict(r) for r in db.protections()],
                    problems=store.current().problems(), warnings=store.current().warnings(),
                    config_path=store.current().source_path,
                    shadowed=store.shadowed_env())

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
        return page(request, "plan.html", plan=plan,
                    plan_summary=json.loads(plan["summary"] or "{}"), items=parsed)

    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request, limit: int = 300):
        rows = db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (min(limit, 2000),))
        return page(request, "audit.html",
                    entries=[{**dict(r), "detail": json.loads(r["detail"] or "{}")} for r in rows])

    # -- settings API ----------------------------------------------------
    @app.get("/api/schema")
    def api_schema():
        return schema_json()

    @app.get("/api/settings")
    def api_get_settings():
        cfg = store.current()
        return {
            "config": cfg.to_dict(redact=True),
            "provenance": store.provenance(),
            "shadowed_env": store.shadowed_env(),
            "revision": cfg.revision,
            "configured": cfg.configured,
            "safety_locked": cfg.safety_locked,
            "config_path": cfg.source_path,
            "problems": cfg.problems(),
            "warnings": cfg.warnings(),
        }

    @app.post("/api/settings")
    async def api_save_settings(request: Request):
        body = await request.json()
        patch = body.get("config") if isinstance(body.get("config"), dict) else body
        replace = bool(body.get("replace", False))

        # Turning dry run off is the single most consequential setting change,
        # so it needs the confirmation phrase just like executing a plan does.
        cfg = store.current()
        wants_live = isinstance(patch.get("safety"), dict) and patch["safety"].get("dry_run") is False
        if wants_live and cfg.safety.dry_run:
            if str(body.get("confirm", "")).strip() != cfg.safety.confirm_phrase:
                return JSONResponse(
                    {"error": f"To leave dry-run mode, type '{cfg.safety.confirm_phrase}' "
                              "in the confirmation box."},
                    status_code=400,
                )
        had_password = bool(cfg.app.password)
        try:
            updated = store.save(patch, merge_patch=not replace)
        except ConfigError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        response = JSONResponse({
            "saved": True, "revision": updated.revision,
            "config": updated.to_dict(redact=True),
            "problems": updated.problems(), "warnings": updated.warnings(),
            "configured": updated.configured,
        })

        # Setting a password mid-wizard would otherwise lock the user out of the
        # very next step, so hand them a session as part of the same response.
        # Only when they had no password before: once one exists, this request
        # already had to be authenticated to get here.
        if updated.app.password and not had_password:
            response.set_cookie(COOKIE, auth.make_token(), httponly=True,
                                samesite="strict", path="/")
            log.info("password set — issued a session to the configuring client")
        return response

    @app.post("/api/settings/reset")
    def api_reset_settings():
        cfg = store.reset()
        return {"reset": True, "revision": cfg.revision, "configured": cfg.configured}

    @app.get("/api/settings/export")
    def api_export_settings(include_secrets: bool = False):
        """Download the current config as YAML. Secrets are redacted by default."""
        cfg = store.current()
        return PlainTextResponse(
            cfg.to_yaml(redact=not include_secrets),
            headers={"Content-Disposition": 'attachment; filename="plexcleaner-config.yaml"'},
            media_type="application/x-yaml",
        )

    @app.post("/api/settings/import")
    async def api_import_settings(request: Request):
        body = await request.json()
        try:
            cfg = store.import_yaml(str(body.get("yaml", "")))
        except ConfigError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"imported": True, "revision": cfg.revision, "configured": cfg.configured}

    # -- service probing / discovery --------------------------------------
    @app.post("/api/test-service")
    async def api_test_service(request: Request):
        body = await request.json()
        kind = str(body.get("kind", ""))
        try:
            return _probe(kind, body, store.current())
        except ClientError as exc:
            return {"name": kind, "ok": False, "detail": str(exc)}

    @app.get("/api/test-connections")
    def api_test_all():
        services = Services(store.current())
        try:
            return {"results": services.test_all()}
        finally:
            services.close()

    @app.post("/api/discover/plex-libraries")
    async def api_discover_libraries(request: Request):
        """List the Plex libraries so they can be picked instead of typed."""
        from ..config import REDACTED
        body = await request.json()
        cfg = store.current()
        url = str(body.get("url") or cfg.plex.url)
        token = str(body.get("token") or "")
        if not token or token == REDACTED:
            token = cfg.plex.token
        if not url or not token:
            return JSONResponse({"error": "A Plex URL and token are needed first."},
                                status_code=400)
        client = PlexClient(url, token, timeout=20, verify_ssl=bool(body.get("verify_ssl", False)))
        try:
            sections = client.sections()
        except ClientError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        finally:
            client.close()
        return {"libraries": [
            {"key": str(s.get("key")), "title": s.get("title"), "type": s.get("type")}
            for s in sections if s.get("type") in ("movie", "show")
        ], "other": [
            {"key": str(s.get("key")), "title": s.get("title"), "type": s.get("type")}
            for s in sections if s.get("type") not in ("movie", "show")
        ]}

    # -- status / scan ----------------------------------------------------
    @app.get("/api/status")
    def api_status():
        cfg = store.current()
        scan = db.latest_scan()
        return {
            "version": __version__,
            "dry_run": cfg.safety.dry_run,
            "configured": cfg.configured,
            "revision": cfg.revision,
            "runner": runner.state,
            "scan": dict(scan) if scan else None,
            "stats": json.loads(scan["stats"]) if scan else {},
            "problems": cfg.problems(),
            "warnings": cfg.warnings(),
        }

    @app.post("/api/scan")
    def api_scan():
        cfg = store.current()
        problems = cfg.problems()
        if problems:
            return JSONResponse({"error": "Fix the configuration first: " + "; ".join(problems)},
                                status_code=400)
        if not runner.start("manual"):
            return JSONResponse({"error": "a scan is already running"}, status_code=409)
        return {"started": True}

    # -- media / users ----------------------------------------------------
    @app.get("/api/media")
    def api_media(verdict: str = "candidate", kind: str = "", library: str = "",
                  search: str = "", sort: str = "size", limit: int = 500, offset: int = 0):
        scan = db.latest_scan()
        if not scan:
            return {"items": [], "total": 0, "scan_id": None, "libraries": []}

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

        order = {"size": "size_bytes DESC", "title": "title ASC",
                 "watched": "last_watched_at ASC", "added": "added_at ASC"}.get(sort, "size_bytes DESC")
        clause = " AND ".join(where)
        total = db.query_one(f"SELECT COUNT(*) AS c FROM media_item WHERE {clause}", tuple(params))
        rows = db.query(f"SELECT * FROM media_item WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
                        (*params, min(limit, 2000), offset))
        return {
            "scan_id": scan["id"],
            "total": total["c"] if total else 0,
            "items": [_media_json(r) for r in rows],
            "libraries": [r["plex_library"] for r in db.query(
                "SELECT DISTINCT plex_library FROM media_item WHERE scan_id = ? "
                "AND plex_library IS NOT NULL ORDER BY plex_library", (scan["id"],))],
        }

    @app.get("/api/users")
    def api_users(verdict: str = "candidate"):
        scan = db.latest_scan()
        if not scan:
            return {"items": [], "scan_id": None}
        if verdict and verdict != "all":
            rows = db.query("SELECT * FROM user_account WHERE scan_id = ? AND verdict = ? "
                            "ORDER BY last_seen_at ASC", (scan["id"], verdict))
        else:
            rows = db.query("SELECT * FROM user_account WHERE scan_id = ? ORDER BY last_seen_at ASC",
                            (scan["id"],))
        return {"scan_id": scan["id"], "items": [_user_json(r) for r in rows]}

    # -- plans ------------------------------------------------------------
    @app.post("/api/plans")
    async def api_create_plan(request: Request):
        body = await request.json()
        scan = db.latest_scan()
        if not scan:
            return JSONResponse({"error": "no completed scan yet"}, status_code=400)
        try:
            plan_id = build_plan(
                db, store.current(),
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
        executor = Executor(store.current(), db)
        try:
            return executor.execute(plan_id, confirm=str(body.get("confirm", "")),
                                    force_live=bool(body.get("live", False)))
        except PlanError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        finally:
            executor.services.close()

    @app.post("/api/plans/{plan_id}/cancel")
    def api_cancel(plan_id: int):
        cancel_plan(db, plan_id)
        return {"cancelled": plan_id}

    # -- protections ------------------------------------------------------
    @app.post("/api/protections")
    async def api_add_protection(request: Request):
        body = await request.json()
        kind, ref = str(body.get("kind", "")), str(body.get("ref", ""))
        if kind not in ("media", "user") or not ref:
            return JSONResponse({"error": "kind must be media or user, and ref is required"},
                                status_code=400)
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
        "id": row["id"], "kind": row["kind"], "title": row["title"], "year": row["year"],
        "library": row["plex_library"],
        "size_gb": round((row["size_bytes"] or 0) / 1_000_000_000, 2),
        "added_at": row["added_at"], "last_watched_at": row["last_watched_at"],
        "play_count": row["play_count"], "watcher_count": row["watcher_count"],
        "episode_count": row["episode_count"],
        "arr": f"{row['arr_instance']}#{row['arr_id']}" if row["arr_instance"] else None,
        "arr_status": row["arr_status"], "seerr_media_id": row["seerr_media_id"],
        "seerr_requested_by": row["seerr_requested_by"], "verdict": row["verdict"],
        "reasons": json.loads(row["reasons"] or "[]"),
        "ref": json.loads(row["detail"] or "{}").get("ref", ""),
    }


def _user_json(row) -> dict[str, Any]:
    return {
        "id": row["id"], "username": row["username"], "email": row["email"],
        "is_admin": bool(row["is_admin"]), "is_home": bool(row["is_home"]),
        "last_seen_at": row["last_seen_at"], "last_login_at": row["last_login_at"],
        "plays": row["plays"], "request_count": row["request_count"],
        "seerr_user_id": row["seerr_user_id"], "tautulli_user_id": row["tautulli_user_id"],
        "verdict": row["verdict"], "reasons": json.loads(row["reasons"] or "[]"),
        "ref": json.loads(row["detail"] or "{}").get("ref", ""),
    }
