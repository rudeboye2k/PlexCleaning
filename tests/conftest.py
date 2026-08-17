from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plexcleaner.config import (AppConfig, ArrConfig, Config, MediaRules, PlexConfig, RulesConfig,
                                SafetyConfig, SeerrConfig, TautulliConfig, UserRules)
from plexcleaner.db import Database
from plexcleaner.models import MediaItem, UserAccount

NOW = datetime(2026, 8, 17, tzinfo=timezone.utc)


def ago(days: int) -> datetime:
    return NOW - timedelta(days=days)


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        app=AppConfig(host="10.12.128.4", port=8585, allowed_networks=["10.12.128.0/24"],
                      secret_key="s" * 32, password="", data_dir=str(tmp_path)),
        safety=SafetyConfig(dry_run=True, confirm_phrase="DELETE",
                            max_media_deletions_per_run=10, max_user_removals_per_run=3,
                            max_gigabytes_per_run=1000, snapshot_before_delete=False),
        plex=PlexConfig(enabled=True, url="http://plex:32400", token="tok"),
        tautulli=TautulliConfig(enabled=True, url="http://tautulli:8181", api_key="key"),
        sonarr=[ArrConfig(name="sonarr", enabled=True, url="http://sonarr:8989", api_key="k",
                          delete_files=True, add_import_exclusion=True)],
        radarr=[ArrConfig(name="radarr", enabled=True, url="http://radarr:7878", api_key="k",
                          delete_files=True, add_import_exclusion=True)],
        seerr=SeerrConfig(enabled=True, url="http://seerr:5055", api_key="k"),
        rules=RulesConfig(media=MediaRules(), users=UserRules()),
    )


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "test.db")


@pytest.fixture
def movie() -> MediaItem:
    return MediaItem(
        kind="movie", title="Old Movie", year=2001,
        plex_rating_key="101", plex_section_id="1", plex_library="Movies",
        tmdb_id="500", imdb_id="tt0500",
        added_at=ago(900), last_watched_at=ago(700),
        size_bytes=8_000_000_000,
        arr_instance="radarr", arr_id=42,
        seerr_media_id=7,
    )


@pytest.fixture
def show() -> MediaItem:
    return MediaItem(
        kind="show", title="Old Show", year=2010,
        plex_rating_key="202", plex_section_id="2", plex_library="TV Shows",
        tvdb_id="900",
        added_at=ago(1200), last_watched_at=ago(800),
        size_bytes=40_000_000_000, episode_count=60,
        arr_instance="sonarr", arr_id=13, arr_status="ended", arr_monitored=True,
        seerr_media_id=9,
    )


@pytest.fixture
def stale_user() -> UserAccount:
    return UserAccount(
        username="lapsed", email="lapsed@example.com",
        plex_user_id="55", tautulli_user_id="55", seerr_user_id=8,
        has_plex_share=True, last_seen_at=ago(400), plays=12,
    )
