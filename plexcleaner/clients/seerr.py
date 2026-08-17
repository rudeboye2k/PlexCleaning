"""Overseerr / Jellyseerr / Seerr client.

All three expose the same /api/v1 surface authenticated with X-Api-Key.
"""
from __future__ import annotations

import logging
from typing import Any

from ..models import parse_ts
from .base import ClientError, HttpClient, ServiceStatus

log = logging.getLogger(__name__)

PAGE = 100


class SeerrClient(HttpClient):
    service = "seerr"

    def __init__(self, url: str, api_key: str, *, timeout: int = 60, verify_ssl: bool = False):
        super().__init__(url, timeout=timeout, verify_ssl=verify_ssl, headers={"X-Api-Key": api_key})

    def test(self) -> ServiceStatus:
        try:
            status = self.get_json("/api/v1/status") or {}
            return ServiceStatus("seerr", True, "connected", str(status.get("version", "")))
        except ClientError as exc:
            return ServiceStatus("seerr", False, str(exc))

    def _paginate(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        skip = 0
        while True:
            body = self.get_json(path, params={**(params or {}), "take": PAGE, "skip": skip}) or {}
            rows = body.get("results", []) if isinstance(body, dict) else []
            out.extend(rows)
            info = body.get("pageInfo", {}) if isinstance(body, dict) else {}
            total = int(info.get("results") or 0)
            skip += PAGE
            if not rows or skip >= total:
                break
        return out

    # -- media -----------------------------------------------------------
    def media(self) -> list[dict[str, Any]]:
        """Every media entry Seerr knows about, keyed for cross-referencing."""
        rows = self._paginate("/api/v1/media", {"sort": "added"})
        out = []
        for row in rows:
            out.append({
                "id": int(row.get("id", 0)),
                "media_type": row.get("mediaType"),
                "tmdb_id": str(row["tmdbId"]) if row.get("tmdbId") else None,
                "tvdb_id": str(row["tvdbId"]) if row.get("tvdbId") else None,
                "imdb_id": row.get("imdbId") or None,
                "rating_key": str(row.get("ratingKey")) if row.get("ratingKey") else None,
                "status": row.get("status"),
                "service_id": row.get("serviceId"),
                "external_service_id": row.get("externalServiceId"),
                "created_at": parse_ts(row.get("createdAt")),
            })
        return out

    def requests(self) -> list[dict[str, Any]]:
        """Requests with their requester, used to protect recently requested media."""
        rows = self._paginate("/api/v1/request", {"sort": "added", "filter": "all"})
        out = []
        for row in rows:
            media = row.get("media") or {}
            user = row.get("requestedBy") or {}
            out.append({
                "id": int(row.get("id", 0)),
                "status": row.get("status"),
                "media_id": int(media.get("id", 0)) if media.get("id") else None,
                "tmdb_id": str(media["tmdbId"]) if media.get("tmdbId") else None,
                "tvdb_id": str(media["tvdbId"]) if media.get("tvdbId") else None,
                "media_type": row.get("type") or media.get("mediaType"),
                "requested_at": parse_ts(row.get("createdAt")),
                "requested_by": user.get("email") or user.get("plexUsername") or user.get("displayName"),
                "requested_by_id": int(user["id"]) if user.get("id") else None,
            })
        return out

    def delete_media(self, media_id: int) -> dict[str, Any]:
        """Remove the media entry from Seerr so it becomes requestable again."""
        self.delete(f"/api/v1/media/{media_id}")
        return {"deleted_media": media_id}

    def delete_media_file(self, media_id: int) -> dict[str, Any]:
        """Ask Seerr to delete the underlying file via its *arr connection."""
        self.delete(f"/api/v1/media/{media_id}/file")
        return {"deleted_file_for_media": media_id}

    # -- users -----------------------------------------------------------
    def users(self) -> list[dict[str, Any]]:
        rows = self._paginate("/api/v1/user", {"sort": "created"})
        out = []
        for row in rows:
            out.append({
                "id": int(row.get("id", 0)),
                "email": row.get("email"),
                "plex_username": row.get("plexUsername"),
                "display_name": row.get("displayName") or row.get("username"),
                "plex_id": str(row["plexId"]) if row.get("plexId") else None,
                "user_type": row.get("userType"),
                "created_at": parse_ts(row.get("createdAt")),
                "last_login_at": parse_ts(row.get("lastLoginAt")),
                "request_count": int(row.get("requestCount") or 0),
                "is_admin": bool((row.get("permissions") or 0) & 2),  # ADMIN bit
            })
        return out

    def delete_user(self, user_id: int) -> dict[str, Any]:
        self.delete(f"/api/v1/user/{user_id}")
        return {"deleted_user": user_id}
