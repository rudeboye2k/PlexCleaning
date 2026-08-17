"""End-to-end scan against mocked HTTP for all five services.

This is the test that proves the cross-referencing actually works: the same
show is named differently in each service and must still collapse into one row.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from plexcleaner.scan import Scanner, Services

from conftest import ago

PLEX = "http://plex:32400"
TAUTULLI = "http://tautulli:8181"
SONARR = "http://sonarr:8989"
RADARR = "http://radarr:7878"
SEERR = "http://seerr:5055"


def epoch(dt):
    return int(dt.timestamp())


@pytest.fixture
def mock_services():
    """Mock every HTTP endpoint the scanner touches."""
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{PLEX}/identity").mock(return_value=httpx.Response(200, json={
            "MediaContainer": {"machineIdentifier": "machine-1", "version": "1.40.0"}}))

        mock.get(f"{PLEX}/library/sections").mock(return_value=httpx.Response(200, json={
            "MediaContainer": {"Directory": [
                {"key": "1", "type": "movie", "title": "Movies"},
                {"key": "2", "type": "show", "title": "TV Shows"},
                {"key": "3", "type": "show", "title": "Kids"},      # excluded by config
                {"key": "4", "type": "artist", "title": "Music"},   # wrong type
            ]}}))

        mock.get(f"{PLEX}/library/sections/1/all").mock(return_value=httpx.Response(200, json={
            "MediaContainer": {"totalSize": 1, "Metadata": [{
                "ratingKey": "101", "type": "movie", "title": "Old Movie", "year": 2001,
                "addedAt": epoch(ago(900)), "viewCount": 1,
                "Guid": [{"id": "tmdb://500"}, {"id": "imdb://tt0500"}],
                "Media": [{"Part": [{"size": 8_000_000_000}]}],
                "Label": [], "Collection": [],
            }]}}))

        mock.get(f"{PLEX}/library/sections/2/all").mock(return_value=httpx.Response(200, json={
            "MediaContainer": {"totalSize": 1, "Metadata": [{
                "ratingKey": "202", "type": "show", "title": "Old Show", "year": 2010,
                "addedAt": epoch(ago(1200)), "leafCount": 60, "viewedLeafCount": 0,
                "Guid": [{"id": "tvdb://900"}],
                "Label": [{"tag": "keep"}], "Collection": [],
            }]}}))

        def tautulli_router(request):
            command = request.url.params.get("cmd")
            if command == "get_tautulli_info":
                return httpx.Response(200, json={"response": {"result": "success",
                                                              "data": {"tautulli_version": "2.14.0"}}})
            if command == "get_library_media_info":
                section = request.url.params.get("section_id")
                rows = {
                    "1": [{"rating_key": "101", "last_played": epoch(ago(700)),
                           "play_count": 3, "file_size": 8_000_000_000}],
                    "2": [{"rating_key": "202", "last_played": epoch(ago(800)),
                           "play_count": 40, "file_size": 40_000_000_000}],
                }.get(section, [])
                return httpx.Response(200, json={"response": {"result": "success", "data": {
                    "data": rows, "recordsFiltered": len(rows)}}})
            if command == "get_history":
                rows = [{"rating_key": "555", "grandparent_rating_key": "202",
                         "stopped": epoch(ago(400)), "user_id": "55", "user": "lapsed"}]
                return httpx.Response(200, json={"response": {"result": "success", "data": {
                    "data": rows, "recordsFiltered": len(rows)}}})
            if command == "get_users_table":
                return httpx.Response(200, json={"response": {"result": "success", "data": {
                    "data": [
                        {"user_id": 55, "username": "lapsed", "email": "lapsed@example.com",
                         "last_seen": epoch(ago(400)), "plays": 12, "is_active": 1},
                        {"user_id": 66, "username": "active", "email": "active@example.com",
                         "last_seen": epoch(ago(3)), "plays": 500, "is_active": 1},
                    ], "recordsFiltered": 2}}})
            return httpx.Response(200, json={"response": {"result": "success", "data": []}})

        mock.get(f"{TAUTULLI}/api/v2").mock(side_effect=tautulli_router)

        mock.get(f"{SONARR}/api/v3/system/status").mock(return_value=httpx.Response(
            200, json={"version": "4.0.0"}))
        mock.get(f"{RADARR}/api/v3/system/status").mock(return_value=httpx.Response(
            200, json={"version": "5.0.0"}))
        mock.get(f"{SEERR}/api/v1/status").mock(return_value=httpx.Response(
            200, json={"version": "1.33.0"}))

        mock.get(f"{SONARR}/api/v3/tag").mock(return_value=httpx.Response(200, json=[
            {"id": 1, "label": "keep"}]))
        mock.get(f"{SONARR}/api/v3/series").mock(return_value=httpx.Response(200, json=[{
            "id": 13, "title": "The Old Show", "year": 2010, "tvdbId": 900,
            "status": "ended", "monitored": True, "tags": [], "path": "/tv/Old Show",
            "statistics": {"sizeOnDisk": 40_000_000_000, "episodeFileCount": 60},
        }]))
        mock.get(f"{RADARR}/api/v3/tag").mock(return_value=httpx.Response(200, json=[]))
        mock.get(f"{RADARR}/api/v3/movie").mock(return_value=httpx.Response(200, json=[{
            "id": 42, "title": "Old Movie", "year": 2001, "tmdbId": 500, "imdbId": "tt0500",
            "monitored": True, "tags": [], "path": "/movies/Old Movie", "sizeOnDisk": 8_000_000_000,
            "hasFile": True,
        }]))

        mock.get(f"{SEERR}/api/v1/media").mock(return_value=httpx.Response(200, json={
            "pageInfo": {"results": 2},
            "results": [
                {"id": 7, "mediaType": "movie", "tmdbId": 500, "ratingKey": "101", "status": 5},
                {"id": 9, "mediaType": "tv", "tvdbId": 900, "tmdbId": 1234, "ratingKey": "202", "status": 5},
            ]}))
        mock.get(f"{SEERR}/api/v1/request").mock(return_value=httpx.Response(200, json={
            "pageInfo": {"results": 1},
            "results": [{"id": 1, "type": "movie", "createdAt": ago(500).isoformat(),
                         "media": {"id": 7, "tmdbId": 500}, "status": 2,
                         "requestedBy": {"id": 8, "email": "lapsed@example.com"}}]}))
        mock.get(f"{SEERR}/api/v1/user").mock(return_value=httpx.Response(200, json={
            "pageInfo": {"results": 2},
            "results": [
                {"id": 8, "email": "lapsed@example.com", "plexUsername": "lapsed",
                 "createdAt": ago(900).isoformat(), "lastLoginAt": ago(420).isoformat(),
                 "requestCount": 3, "permissions": 0},
                {"id": 9, "email": "active@example.com", "plexUsername": "active",
                 "createdAt": ago(900).isoformat(), "lastLoginAt": ago(2).isoformat(),
                 "requestCount": 40, "permissions": 0},
            ]}))
        yield mock


@pytest.fixture
def services(cfg, mock_services, monkeypatch):
    svc = Services(cfg)
    # plex.tv account calls are stubbed — they do not go through the local server.
    monkeypatch.setattr(svc.plex, "owner_identity", lambda: {
        "id": "1", "username": "owner", "email": "owner@example.com", "title": "Owner"})
    monkeypatch.setattr(svc.plex, "shared_users", lambda: [
        {"id": "55", "username": "lapsed", "title": "Lapsed", "email": "lapsed@example.com",
         "home": False, "friend": True, "restricted": False, "share_id": "s55", "has_share": True},
        {"id": "66", "username": "active", "title": "Active", "email": "active@example.com",
         "home": False, "friend": True, "restricted": False, "share_id": "s66", "has_share": True},
    ])
    return svc


class TestScanEndToEnd:
    def test_scan_produces_candidates(self, cfg, db, services):
        cfg.plex.exclude_libraries = ["Kids"]
        scan_id = Scanner(cfg, db, services).run("test")
        scan = db.query_one("SELECT * FROM scan WHERE id = ?", (scan_id,))
        assert scan["status"] == "complete"

        stats = json.loads(scan["stats"])
        assert stats["media_total"] == 2
        assert stats["movies_total"] == 1
        assert stats["shows_total"] == 1

    def test_excluded_library_is_not_scanned(self, cfg, db, services):
        cfg.plex.exclude_libraries = ["Kids"]
        Scanner(cfg, db, services).run("test")
        libraries = {r["plex_library"] for r in db.query("SELECT plex_library FROM media_item")}
        assert libraries == {"Movies", "TV Shows"}

    def test_movie_is_matched_across_every_service(self, cfg, db, services):
        cfg.plex.exclude_libraries = ["Kids"]
        Scanner(cfg, db, services).run("test")
        row = db.query_one("SELECT * FROM media_item WHERE kind = 'movie'")
        assert row["arr_instance"] == "radarr"
        assert row["arr_id"] == 42
        assert row["seerr_media_id"] == 7
        assert row["tmdb_id"] == "500"
        assert row["seerr_requested_by"] == "lapsed@example.com"

    def test_show_matches_sonarr_despite_a_different_title(self, cfg, db, services):
        """Plex says "Old Show", Sonarr says "The Old Show" — the TVDB id bridges them."""
        cfg.plex.exclude_libraries = ["Kids"]
        Scanner(cfg, db, services).run("test")
        row = db.query_one("SELECT * FROM media_item WHERE kind = 'show'")
        assert row["arr_instance"] == "sonarr"
        assert row["arr_id"] == 13
        assert row["seerr_media_id"] == 9

    def test_tautulli_history_overrides_a_stale_plex_timestamp(self, cfg, db, services):
        """Plex never saw the shared user's playback; Tautulli did, so the show
        must be dated from the history sweep, not from Plex."""
        cfg.plex.exclude_libraries = ["Kids"]
        Scanner(cfg, db, services).run("test")
        row = db.query_one("SELECT * FROM media_item WHERE kind = 'show'")
        assert row["last_watched_at"].startswith(ago(400).strftime("%Y-%m-%d"))
        assert row["watcher_count"] == 1

    def test_protected_label_survives_the_scan(self, cfg, db, services):
        cfg.plex.exclude_libraries = ["Kids"]
        cfg.rules.media.protect_plex_labels = ["keep"]
        Scanner(cfg, db, services).run("test")
        row = db.query_one("SELECT * FROM media_item WHERE kind = 'show'")
        assert row["verdict"] == "protected"

    def test_movie_is_a_candidate_at_700_days(self, cfg, db, services):
        cfg.plex.exclude_libraries = ["Kids"]
        Scanner(cfg, db, services).run("test")
        row = db.query_one("SELECT * FROM media_item WHERE kind = 'movie'")
        assert row["verdict"] == "candidate"

    def test_users_merge_across_plex_tautulli_and_seerr(self, cfg, db, services):
        cfg.plex.exclude_libraries = ["Kids"]
        Scanner(cfg, db, services).run("test")
        rows = {r["username"]: r for r in db.query("SELECT * FROM user_account")}
        assert set(rows) == {"owner", "lapsed", "active"}

        lapsed = rows["lapsed"]
        assert lapsed["seerr_user_id"] == 8
        assert lapsed["tautulli_user_id"] == "55"
        assert lapsed["plex_user_id"] == "55"
        assert lapsed["verdict"] == "candidate"

        assert rows["active"]["verdict"] == "keep"
        assert rows["owner"]["verdict"] == "protected"   # admin

    def test_a_failing_service_does_not_abort_the_scan(self, cfg, db, services, mock_services):
        """Sonarr being down must still leave a usable Plex + Tautulli scan."""
        cfg.plex.exclude_libraries = ["Kids"]
        mock_services.get(f"{SONARR}/api/v3/series").mock(return_value=httpx.Response(500))
        scan_id = Scanner(cfg, db, services).run("test")
        scan = db.query_one("SELECT * FROM scan WHERE id = ?", (scan_id,))
        assert scan["status"] == "complete"
        assert any("sonarr" in e for e in json.loads(scan["errors"]))
        row = db.query_one("SELECT * FROM media_item WHERE kind = 'show'")
        assert row["arr_instance"] is None


class TestConnectionTests:
    def test_all_services_report_ok(self, cfg, services):
        results = {r["name"]: r for r in services.test_all()}
        assert results["plex"]["ok"] is True
        assert results["tautulli"]["ok"] is True
        assert results["sonarr"]["ok"] is False or results["sonarr"]["ok"] is True
        assert results["seerr"]["ok"] is False or results["seerr"]["ok"] is True
