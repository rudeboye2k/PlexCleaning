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


def client_for(store, db, source_ip="10.12.128.20"):
    return TestClient(_SourceAddress(create_app(store, db), source_ip))


def authorized(store, db, source_ip="10.12.128.20"):
    """A client that has loaded a page and therefore holds a CSRF cookie."""
    c = client_for(store, db, source_ip)
    c.get("/")
    return c


def csrf(c) -> dict[str, str]:
    return {"X-CSRF-Token": c.cookies.get("plexcleaner_csrf") or ""}


class TestNetworkGuard:
    def test_allowed_network_gets_through(self, store, db, cfg):
        assert client_for(store, db, "10.12.128.20").get("/api/status").status_code == 200

    def test_the_bind_address_itself_is_allowed(self, store, db, cfg):
        assert client_for(store, db, "10.12.128.4").get("/api/status").status_code == 200

    def test_loopback_is_always_allowed(self, store, db, cfg):
        assert client_for(store, db, "127.0.0.1").get("/api/status").status_code == 200

    def test_outside_address_is_refused(self, store, db, cfg):
        res = client_for(store, db, "203.0.113.9").get("/api/status")
        assert res.status_code == 403
        assert "internal only" in res.text

    def test_a_different_private_subnet_is_still_refused(self, store, db, cfg):
        """Being on RFC1918 space is not the same as being on the allow list."""
        assert client_for(store, db, "192.168.1.50").get("/api/status").status_code == 403

    def test_healthz_bypasses_the_guard_for_container_checks(self, store, db, cfg):
        assert client_for(store, db, "203.0.113.9").get("/healthz").status_code == 200

    def test_guard_applies_to_pages_too(self, store, db, cfg):
        assert client_for(store, db, "203.0.113.9").get("/").status_code == 403


class TestAuth:
    @pytest.fixture
    def secured(self, store, db, cfg):
        # Saved through the store, because middleware reads config live rather
        # than from a value captured at startup.
        store.save({"app": {"password": "hunter2"}}, actor="test")
        return client_for(store, db)

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

    def test_no_password_configured_means_no_login_wall(self, store, db, cfg):
        assert store.current().app.password == ""
        assert client_for(store, db).get("/api/status").status_code == 200


class TestEndpoints:
    def test_status_reports_dry_run(self, store, db, cfg):
        body = client_for(store, db).get("/api/status").json()
        assert body["dry_run"] is True

    def test_media_endpoint_is_empty_before_any_scan(self, store, db, cfg):
        body = client_for(store, db).get("/api/media").json()
        assert body["items"] == []
        assert body["scan_id"] is None

    def test_media_endpoint_returns_candidates(self, store, db, cfg, movie):
        scan_id = db.insert("scan", {"started_at": "2026-08-17T00:00:00+00:00", "status": "complete"})
        movie.verdict = "candidate"
        db.insert("media_item", movie.to_row(scan_id))
        body = client_for(store, db).get("/api/media").json()
        assert len(body["items"]) == 1
        assert body["items"][0]["title"] == "Old Movie"
        assert body["items"][0]["size_gb"] == 8.0

    def test_media_search_filter(self, store, db, cfg, movie):
        scan_id = db.insert("scan", {"started_at": "2026-08-17T00:00:00+00:00", "status": "complete"})
        movie.verdict = "candidate"
        db.insert("media_item", movie.to_row(scan_id))
        assert client_for(store, db).get("/api/media?search=Nothing").json()["items"] == []
        assert len(client_for(store, db).get("/api/media?search=Old").json()["items"]) == 1

    def test_pages_render(self, store, db, cfg):
        c = client_for(store, db)
        for path in ("/", "/media", "/users", "/audit", "/settings"):
            assert c.get(path).status_code == 200, path

    def test_creating_a_plan_with_nothing_selected_is_a_400(self, store, db, cfg):
        db.insert("scan", {"started_at": "2026-08-17T00:00:00+00:00", "status": "complete"})
        c = authorized(store, db)
        res = c.post("/api/plans", json={"media_ids": [], "user_ids": []}, headers=csrf(c))
        assert res.status_code == 400

    def test_protection_round_trip(self, store, db, cfg):
        c = authorized(store, db)
        assert c.post("/api/protections", headers=csrf(c),
                      json={"kind": "media", "ref": "movie:tmdb:1",
                            "label": "Keeper"}).status_code == 200
        assert db.protected_refs("media") == {"movie:tmdb:1"}
        c.request("DELETE", "/api/protections", headers=csrf(c),
                  json={"kind": "media", "ref": "movie:tmdb:1"})
        assert db.protected_refs("media") == set()

    def test_protection_rejects_a_bad_kind(self, store, db, cfg):
        c = authorized(store, db)
        res = c.post("/api/protections", json={"kind": "wat", "ref": "x"}, headers=csrf(c))
        assert res.status_code == 400

    def test_csrf_is_enforced_even_with_no_password(self, store, db, cfg):
        """Otherwise any LAN page a browser loads could POST a deletion here."""
        assert store.current().app.password == ""
        c = authorized(store, db)
        assert c.post("/api/protections",
                      json={"kind": "media", "ref": "movie:tmdb:1"}).status_code == 403

    def test_reads_do_not_need_a_csrf_token(self, store, db, cfg):
        assert client_for(store, db).get("/api/media").status_code == 200


class TestSettingsPortal:
    def test_schema_is_served(self, store, db, cfg):
        body = client_for(store, db).get("/api/schema").json()
        keys = [s["key"] for s in body["sections"]]
        assert "plex" in keys and "rules.media" in keys
        assert [w["key"] for w in body["wizard"]][0] == "welcome"

    def test_get_settings_redacts_secrets(self, store, db, cfg):
        store.save({"plex": {"token": "plex-secret-xyzzy"},
                    "sonarr": [{"name": "sonarr", "enabled": True,
                                "url": "http://sonarr:8989", "api_key": "arr-secret-xyzzy"}]})
        raw = client_for(store, db).get("/api/settings").text
        assert "xyzzy" not in raw
        assert "__unchanged__" in raw

    def test_saving_applies_without_a_restart(self, store, db, cfg):
        """The whole point of the store: no container bounce to change settings."""
        c = authorized(store, db)
        res = c.post("/api/settings", headers=csrf(c),
                     json={"config": {"rules": {"media": {"unwatched_days": 999}}}})
        assert res.status_code == 200
        # Same running app instance, no reload.
        assert store.current().rules.media.unwatched_days == 999

    def test_network_guard_picks_up_a_saved_change_immediately(self, store, db, cfg):
        c = authorized(store, db, "10.12.128.20")
        c.post("/api/settings", headers=csrf(c),
               json={"config": {"app": {"allowed_networks": ["192.168.5.0/24"]}}})
        # The client's own subnet is no longer allowed, on the very next request.
        assert c.get("/api/status").status_code == 403

    def test_setting_a_password_mid_wizard_does_not_lock_you_out(self, empty_store, db):
        """Regression: the Access step used to 401 the very next wizard step."""
        c = client_for(empty_store, db)
        c.get("/setup")
        res = c.post("/api/settings", headers=csrf(c),
                     json={"config": {"app": {"password": "hunter2"}}})
        assert res.status_code == 200
        assert res.cookies.get("plexcleaner_session")
        # The next step goes through without a separate login round trip.
        nxt = c.post("/api/settings", headers=csrf(c), json={
            "config": {"plex": {"enabled": True, "url": "http://p:32400", "token": "t"}}})
        assert nxt.status_code == 200

    def test_leaving_dry_run_needs_the_confirm_phrase(self, store, db, cfg):
        c = authorized(store, db)
        res = c.post("/api/settings", headers=csrf(c),
                     json={"config": {"safety": {"dry_run": False}}})
        assert res.status_code == 400
        assert "DELETE" in res.json()["error"]
        assert store.current().safety.dry_run is True

    def test_leaving_dry_run_works_with_the_phrase(self, store, db, cfg):
        c = authorized(store, db)
        res = c.post("/api/settings", headers=csrf(c),
                     json={"config": {"safety": {"dry_run": False}}, "confirm": "DELETE"})
        assert res.status_code == 200
        assert store.current().safety.dry_run is False

    def test_invalid_settings_return_400_not_500(self, store, db, cfg):
        c = authorized(store, db)
        res = c.post("/api/settings", headers=csrf(c),
                     json={"config": {"app": {"allowed_networks": ["nonsense"]}}})
        assert res.status_code == 400

    def test_export_is_yaml_and_redacted(self, store, db, cfg):
        res = client_for(store, db).get("/api/settings/export")
        assert res.status_code == 200
        assert "__unchanged__" in res.text
        assert "attachment" in res.headers["content-disposition"]

    def test_scan_is_refused_until_configured(self, empty_store, db):
        c = client_for(empty_store, db)
        c.get("/setup")
        res = c.post("/api/scan", headers=csrf(c))
        assert res.status_code == 400
        assert "Plex or Tautulli" in res.json()["error"]

    def test_unconfigured_install_redirects_to_the_wizard(self, empty_store, db):
        c = client_for(empty_store, db)
        res = c.get("/", follow_redirects=False)
        assert res.status_code == 302
        assert res.headers["location"] == "/setup"

    def test_setup_page_renders_on_a_fresh_install(self, empty_store, db):
        assert client_for(empty_store, db).get("/setup").status_code == 200

    def test_test_service_reports_a_bad_target_cleanly(self, store, db, cfg):
        """An unreachable service must come back as ok=false, not a 500."""
        c = authorized(store, db)
        res = c.post("/api/test-service", headers=csrf(c), json={
            "kind": "sonarr", "name": "nope", "url": "http://127.0.0.1:9", "api_key": "x",
            "timeout": 5})
        assert res.status_code == 200
        assert res.json()["ok"] is False

    def test_test_service_rejects_an_unknown_kind(self, store, db, cfg):
        c = authorized(store, db)
        res = c.post("/api/test-service", headers=csrf(c), json={"kind": "wat"})
        assert res.json()["ok"] is False
