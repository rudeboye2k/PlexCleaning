"""Domain objects shared by the scan, rules and action layers."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def parse_ts(value: Any) -> datetime | None:
    """Best-effort timestamp parsing across the wildly different formats these APIs use."""
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def days_since(dt: datetime | None, now: datetime | None = None) -> int | None:
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max((now - dt).days, 0)


@dataclass
class MediaItem:
    """One movie or one show, merged across every service that knows about it."""
    kind: str                                   # movie | show
    title: str
    year: int | None = None

    plex_rating_key: str | None = None
    plex_section_id: str | None = None
    plex_library: str | None = None

    tmdb_id: str | None = None
    tvdb_id: str | None = None
    imdb_id: str | None = None

    added_at: datetime | None = None
    last_watched_at: datetime | None = None
    play_count: int = 0
    watcher_count: int = 0
    in_progress: bool = False

    size_bytes: int = 0
    episode_count: int = 0

    arr_instance: str | None = None
    arr_id: int | None = None
    arr_status: str | None = None            # continuing | ended | released | ...
    arr_monitored: bool = False
    arr_tags: list[str] = field(default_factory=list)
    arr_path: str | None = None

    seerr_media_id: int | None = None
    seerr_requested_by: str | None = None
    seerr_requested_at: datetime | None = None

    labels: list[str] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)

    verdict: str = "keep"
    reasons: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        """Stable identity used for protections, plan items and the UI."""
        for prefix, value in (("tmdb", self.tmdb_id), ("tvdb", self.tvdb_id), ("imdb", self.imdb_id)):
            if value:
                return f"{self.kind}:{prefix}:{value}"
        if self.plex_rating_key:
            return f"{self.kind}:plex:{self.plex_rating_key}"
        return f"{self.kind}:title:{self.title.lower()}:{self.year or ''}"

    @property
    def size_gb(self) -> float:
        return round(self.size_bytes / 1_000_000_000, 2)

    @property
    def days_unwatched(self) -> int | None:
        return days_since(self.last_watched_at)

    @property
    def days_in_library(self) -> int | None:
        return days_since(self.added_at)

    def to_row(self, scan_id: int) -> dict[str, Any]:
        return {
            "scan_id": scan_id,
            "kind": self.kind,
            "title": self.title,
            "year": self.year,
            "plex_rating_key": self.plex_rating_key,
            "plex_section_id": self.plex_section_id,
            "plex_library": self.plex_library,
            "tmdb_id": self.tmdb_id,
            "tvdb_id": self.tvdb_id,
            "imdb_id": self.imdb_id,
            "added_at": iso(self.added_at),
            "last_watched_at": iso(self.last_watched_at),
            "play_count": self.play_count,
            "watcher_count": self.watcher_count,
            "size_bytes": self.size_bytes,
            "episode_count": self.episode_count,
            "arr_instance": self.arr_instance,
            "arr_id": self.arr_id,
            "arr_status": self.arr_status,
            "arr_monitored": int(self.arr_monitored),
            "arr_tags": json.dumps(self.arr_tags),
            "seerr_media_id": self.seerr_media_id,
            "seerr_requested_by": self.seerr_requested_by,
            "seerr_requested_at": iso(self.seerr_requested_at),
            "labels": json.dumps(self.labels),
            "collections": json.dumps(self.collections),
            "verdict": self.verdict,
            "reasons": json.dumps(self.reasons),
            "detail": json.dumps({**self.detail, "ref": self.ref, "arr_path": self.arr_path}, default=str),
        }

    def snapshot(self) -> dict[str, Any]:
        """Everything needed to re-add this item by hand later."""
        data = asdict(self)
        for key in ("added_at", "last_watched_at", "seerr_requested_at"):
            data[key] = iso(getattr(self, key))
        data["ref"] = self.ref
        return data


@dataclass
class UserAccount:
    """One person, merged across Plex, Tautulli and Seerr."""
    username: str
    email: str | None = None
    title: str | None = None

    plex_user_id: str | None = None
    plex_share_id: str | None = None
    tautulli_user_id: str | None = None
    seerr_user_id: int | None = None

    is_admin: bool = False
    is_home: bool = False
    has_plex_share: bool = False

    last_seen_at: datetime | None = None      # last playback (Tautulli)
    last_login_at: datetime | None = None     # last Seerr login
    first_seen_at: datetime | None = None
    plays: int = 0
    request_count: int = 0

    verdict: str = "keep"
    reasons: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return f"user:{(self.email or self.username or '').lower()}"

    @property
    def last_activity(self) -> datetime | None:
        stamps = [s for s in (self.last_seen_at, self.last_login_at) if s]
        return max(stamps) if stamps else None

    @property
    def days_inactive(self) -> int | None:
        return days_since(self.last_activity)

    def to_row(self, scan_id: int) -> dict[str, Any]:
        return {
            "scan_id": scan_id,
            "username": self.username,
            "email": self.email,
            "title": self.title,
            "plex_user_id": self.plex_user_id,
            "plex_share_id": self.plex_share_id,
            "tautulli_user_id": self.tautulli_user_id,
            "seerr_user_id": self.seerr_user_id,
            "is_admin": int(self.is_admin),
            "is_home": int(self.is_home),
            "last_seen_at": iso(self.last_seen_at),
            "last_login_at": iso(self.last_login_at),
            "plays": self.plays,
            "request_count": self.request_count,
            "verdict": self.verdict,
            "reasons": json.dumps(self.reasons),
            "detail": json.dumps(
                {**self.detail, "ref": self.ref, "has_plex_share": self.has_plex_share}, default=str
            ),
        }

    def snapshot(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("last_seen_at", "last_login_at", "first_seen_at"):
            data[key] = iso(getattr(self, key))
        data["ref"] = self.ref
        return data


@dataclass
class Step:
    """A single service call in a deletion plan."""
    service: str          # sonarr | radarr | seerr | plex | tautulli
    action: str           # delete_series | delete_movie | delete_media | unshare | ...
    target: str
    params: dict[str, Any] = field(default_factory=dict)
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
