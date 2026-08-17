"""Web layer: the network guard and auth are what keep this thing internal."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from plexcleaner.web.app import create_app


class _SourceAddress:
    """ASGI shim that pins the client address TestClient reports.

    Starlette's TestClient hardcodes it, and the whole point of the network
    guard is that it reacts to that address.
    """

    def __init__(self, app, ip: str):
        self.app = app
        self.ip = ip

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": (self.ip, 12345)}
        await self.app(scope, receive, send)


def client_for(cfg, db, source_ip="10.12.128.20"):
    return TestClient(_SourceAddress(create_app(cfg, db), source_ip))


def authorized(cfg, db, source_ip="10.12.128.20"):
    """A client that has loaded a page and therefore holds a CSRF cookie."""
    c = client_for(cfg, db, source_ip)
    c.get("/")
    return c


def csrf(c) -> dict[str, str]:
    return {"X-CSRF-Token": c.cookies.get("plexcleaner_csrf") or ""}


class TestNetworkGuard:
    def test_allowed_network_gets_through(self, cfg, db):
        assert client_for(cfg, db, "10.12.128.20").get("/api/status").status_code == 200

    def test_the_bind_address_itself_is_allowed(self, cfg, db):
        assert client_for(cfg, db, "10.12.128.4").get("/api/status").status_code == 200

    def test_loopback_is_always_allowed(self, cfg, db):
        assert client_for(cfg, db, "127.0.0.1").get("/api/status").status_code == 200

    def test_outside_address_is_refused(self, cfg, db):
        res = client_for(cfg, db, "203.0.113.9").get("/api/status")
        assert res.status_code == 403
        assert "internal only" in res.text

    def test_a_different_private_subnet_is_still_refused(self, cfg, db):
        """Being on RFC1918 space is not the same as being on the allow list."""
        assert client_for(cfg, db, "192.168.1.50").get("/api/status").status_code == 403

    def test_healthz_bypasses_the_guard_for_container_checks(self, cfg, db):
        assert client_for(cfg, db, "203.0.113.9").get("/healthz").status_code == 200

    def test_guard_applies_to_pages_too(self, cfg, db):
        assert client_for(cfg, db, "203.0.113.9").get("/").status_code == 403


class TestAuth:
    @pytest.fixture
    def secured(self, cfg, db):
        cfg.app.password = "hunter2"
        return client_for(cfg, db)

    def test_api_requires_a_session(self, secured):
        assert secured.get("/api/status").status_code == 401

    def test_pages_redirect_to_login(self, secured):
        res = secured.get("/", follow_redirects=False)
        assert res.status_code == 302
        assert res.headers["location"] == "/login"

    def test_login_page_is_public(self, secured):
        assert secured.get("/login").status_code == 200

    def test_wrong_password_is_rejected(self, secured):
        res = secured.post("/login", data={"password": "wrong"}, follow_redirects=False)
        assert "error" in res.headers["location"]

    def test_correct_password_grants_access(self, secured):
        secured.post("/login", data={"password": "hunter2"}, follow_redirects=False)
        assert secured.get("/api/status").status_code == 200

    def test_write_without_csrf_token_is_refused(self, secured):
        secured.post("/login", data={"password": "hunter2"}, follow_redirects=False)
        res = secured.post("/api/scan", headers={"X-CSRF-Token": "bogus"})
        assert res.status_code == 403

    def test_write_with_matching_csrf_token_is_allowed(self, secured):
        secured.post("/login", data={"password": "hunter2"}, follow_redirects=False)
        token = secured.cookies.get("plexcleaner_csrf")
        res = secured.post("/api/scan", headers={"X-CSRF-Token": token})
        assert res.status_code in (200, 409)

    def test_no_password_configured_means_no_login_wall(self, cfg, db):
        cfg.app.password = ""
        assert client_for(cfg, db).get("/api/status").status_code == 200


class TestEndpoints:
    def test_status_reports_dry_run(self, cfg, db):
        body = client_for(cfg, db).get("/api/status").json()
        assert body["dry_run"] is True

    def test_media_endpoint_is_empty_before_any_scan(self, cfg, db):
        body = client_for(cfg, db).get("/api/media").json()
        assert body["items"] == []
        assert body["scan_id"] is None

    def test_media_endpoint_returns_candidates(self, cfg, db, movie):
        scan_id = db.insert("scan", {"started_at": "2026-08-17T00:00:00+00:00", "status": "complete"})
        movie.verdict = "candidate"
        db.insert("media_item", movie.to_row(scan_id))
        body = client_for(cfg, db).get("/api/media").json()
        assert len(body["items"]) == 1
        assert body["items"][0]["title"] == "Old Movie"
        assert body["items"][0]["size_gb"] == 8.0

    def test_media_search_filter(self, cfg, db, movie):
        scan_id = db.insert("scan", {"started_at": "2026-08-17T00:00:00+00:00", "status": "complete"})
        movie.verdict = "candidate"
        db.insert("media_item", movie.to_row(scan_id))
        assert client_for(cfg, db).get("/api/media?search=Nothing").json()["items"] == []
        assert len(client_for(cfg, db).get("/api/media?search=Old").json()["items"]) == 1

    def test_pages_render(self, cfg, db):
        c = client_for(cfg, db)
        for path in ("/", "/media", "/users", "/audit", "/settings"):
            assert c.get(path).status_code == 200, path

    def test_creating_a_plan_with_nothing_selected_is_a_400(self, cfg, db):
        db.insert("scan", {"started_at": "2026-08-17T00:00:00+00:00", "status": "complete"})
        c = authorized(cfg, db)
        res = c.post("/api/plans", json={"media_ids": [], "user_ids": []}, headers=csrf(c))
        assert res.status_code == 400

    def test_protection_round_trip(self, cfg, db):
        c = authorized(cfg, db)
        assert c.post("/api/protections", headers=csrf(c),
                      json={"kind": "media", "ref": "movie:tmdb:1",
                            "label": "Keeper"}).status_code == 200
        assert db.protected_refs("media") == {"movie:tmdb:1"}
        c.request("DELETE", "/api/protections", headers=csrf(c),
                  json={"kind": "media", "ref": "movie:tmdb:1"})
        assert db.protected_refs("media") == set()

    def test_protection_rejects_a_bad_kind(self, cfg, db):
        c = authorized(cfg, db)
        res = c.post("/api/protections", json={"kind": "wat", "ref": "x"}, headers=csrf(c))
        assert res.status_code == 400

    def test_csrf_is_enforced_even_with_no_password(self, cfg, db):
        """Otherwise any LAN page a browser loads could POST a deletion here."""
        cfg.app.password = ""
        c = authorized(cfg, db)
        assert c.post("/api/protections",
                      json={"kind": "media", "ref": "movie:tmdb:1"}).status_code == 403

    def test_reads_do_not_need_a_csrf_token(self, cfg, db):
        assert client_for(cfg, db).get("/api/media").status_code == 200
