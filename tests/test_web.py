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


class TestMediaSorting:
    """Every column header is clickable and toggles direction."""

    @pytest.fixture
    def seeded(self, store, db, cfg):
        from plexcleaner.models import MediaItem, parse_ts
        scan_id = db.insert("scan", {"started_at": "2026-10-18T00:00:00+00:00",
                                     "status": "complete"})
        rows = [
            # title,        lib,     size_gb, watched,      added,        plays
            ("apple show",  "TV",      5,  "2020-01-01", "2019-01-01",  2),
            ("Banana Film", "Movies", 50,  None,         "2018-01-01", 10),
            ("cherry Film", "Movies", 20,  "2024-01-01", "2023-01-01",  0),
        ]
        for title, lib, gb, watched, added, plays in rows:
            item = MediaItem(
                kind="show" if lib == "TV" else "movie", title=title,
                plex_library=lib, size_bytes=gb * 1_000_000_000,
                last_watched_at=parse_ts(watched), added_at=parse_ts(added),
                play_count=plays, verdict="candidate",
            )
            db.insert("media_item", item.to_row(scan_id))
        return client_for(store, db)

    def titles(self, client, query=""):
        body = client.get(f"/api/media?{query}").json()
        return [i["title"] for i in body["items"]], body

    def test_default_is_largest_first(self, seeded):
        titles, body = self.titles(seeded)
        assert titles == ["Banana Film", "cherry Film", "apple show"]
        assert (body["sort"], body["dir"]) == ("size", "desc")

    def test_size_ascending(self, seeded):
        titles, _ = self.titles(seeded, "sort=size&dir=asc")
        assert titles == ["apple show", "cherry Film", "Banana Film"]

    def test_title_sort_ignores_case(self, seeded):
        """Without COLLATE NOCASE, "Banana" would sort before "apple"."""
        titles, _ = self.titles(seeded, "sort=title&dir=asc")
        assert titles == ["apple show", "Banana Film", "cherry Film"]

    def test_title_descending(self, seeded):
        titles, _ = self.titles(seeded, "sort=title&dir=desc")
        assert titles == ["cherry Film", "Banana Film", "apple show"]

    def test_never_watched_sorts_first_ascending(self, seeded):
        """Ascending means longest-unwatched first, and never is the extreme."""
        titles, _ = self.titles(seeded, "sort=watched&dir=asc")
        assert titles[0] == "Banana Film"

    def test_most_recently_watched_first_descending(self, seeded):
        titles, _ = self.titles(seeded, "sort=watched&dir=desc")
        assert titles[0] == "cherry Film"

    def test_added_oldest_first_by_default(self, seeded):
        titles, body = self.titles(seeded, "sort=added")
        assert titles[0] == "Banana Film"
        assert body["dir"] == "asc"

    def test_plays_highest_first_by_default(self, seeded):
        titles, body = self.titles(seeded, "sort=plays")
        assert titles[0] == "Banana Film"
        assert body["dir"] == "desc"

    def test_library_sort(self, seeded):
        titles, _ = self.titles(seeded, "sort=library&dir=asc")
        assert titles[-1] == "apple show"   # "TV" sorts after "Movies"

    def test_every_advertised_column_is_sortable(self, seeded):
        from plexcleaner.web.app import MEDIA_SORTS
        for key in MEDIA_SORTS:
            _, body = self.titles(seeded, f"sort={key}")
            assert body["sort"] == key, key

    def test_an_unknown_sort_key_falls_back_rather_than_erroring(self, seeded):
        """Guards the f-string in ORDER BY against anything unexpected."""
        _, body = self.titles(seeded, "sort=; DROP TABLE media_item--")
        assert body["sort"] == "size"
        assert db_intact(seeded)

    def test_a_bogus_direction_falls_back_to_the_column_default(self, seeded):
        _, body = self.titles(seeded, "sort=size&dir=sideways")
        assert body["dir"] == "desc"

    def test_sorting_survives_filtering(self, seeded):
        titles, _ = self.titles(seeded, "kind=movie&sort=title&dir=asc")
        assert titles == ["Banana Film", "cherry Film"]


def db_intact(client) -> bool:
    return client.get("/api/media").status_code == 200


class TestMediaLinks:
    def test_rows_carry_service_links(self, store, db, cfg, movie):
        scan_id = db.insert("scan", {"started_at": "2026-10-18T00:00:00+00:00",
                                     "status": "complete"})
        movie.verdict = "candidate"
        movie.arr_slug = "old-movie-2001"
        db.insert("media_item", movie.to_row(scan_id))
        db.set_setting("plex_machine_id", "machine-1")

        item = client_for(store, db).get("/api/media").json()["items"][0]
        services = {l["service"]: l["url"] for l in item["links"]}
        assert services["radarr"] == "http://radarr:7878/movie/old-movie-2001"
        assert services["seerr"] == "http://seerr:5055/movie/500"
        assert "machine-1" in services["plex"]

    def test_links_are_empty_rather_than_absent_without_a_machine_id(self, store, db, cfg, movie):
        scan_id = db.insert("scan", {"started_at": "2026-10-18T00:00:00+00:00",
                                     "status": "complete"})
        movie.verdict = "candidate"
        db.insert("media_item", movie.to_row(scan_id))
        item = client_for(store, db).get("/api/media").json()["items"][0]
        assert isinstance(item["links"], list)
        assert all(l["service"] != "plex" for l in item["links"])
