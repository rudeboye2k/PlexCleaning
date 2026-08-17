"""Sonarr and Radarr clients (both v3 API).

The two services are near-identical apart from the resource name, the id
fields they carry, and — annoyingly — the spelling of the import-exclusion
query parameter.
"""
from __future__ import annotations

import logging
from typing import Any

from ..models import parse_ts
from .base import ClientError, HttpClient, ServiceStatus, as_bool_param

log = logging.getLogger(__name__)


class ArrClient(HttpClient):
    service = "arr"
    resource = ""            # "series" | "movie"
    exclusion_param = ""     # "addImportListExclusion" | "addImportExclusion"

    def __init__(self, name: str, url: str, api_key: str, *, timeout: int = 60, verify_ssl: bool = False):
        super().__init__(url, timeout=timeout, verify_ssl=verify_ssl, headers={"X-Api-Key": api_key})
        self.name = name
        self.service = name
        self._tags: dict[int, str] | None = None

    def test(self) -> ServiceStatus:
        try:
            status = self.get_json("/api/v3/system/status") or {}
            return ServiceStatus(self.name, True, "connected", str(status.get("version", "")))
        except ClientError as exc:
            return ServiceStatus(self.name, False, str(exc))

    def tags(self) -> dict[int, str]:
        if self._tags is None:
            try:
                rows = self.get_json("/api/v3/tag") or []
                self._tags = {int(r["id"]): str(r.get("label", "")) for r in rows if "id" in r}
            except ClientError as exc:
                log.warning("%s: could not load tags: %s", self.name, exc)
                self._tags = {}
        return self._tags

    def tag_labels(self, tag_ids: list[Any]) -> list[str]:
        lookup = self.tags()
        return [lookup[int(t)] for t in tag_ids or [] if int(t) in lookup]

    def all_items(self) -> list[dict[str, Any]]:
        rows = self.get_json(f"/api/v3/{self.resource}") or []
        return [self.parse_item(r) for r in rows]

    def parse_item(self, row: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def delete_item(self, item_id: int, *, delete_files: bool, add_exclusion: bool) -> dict[str, Any]:
        params = {
            "deleteFiles": as_bool_param(delete_files),
            self.exclusion_param: as_bool_param(add_exclusion),
        }
        self.delete(f"/api/v3/{self.resource}/{item_id}", params=params)
        return {"deleted": item_id, "params": params}

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        try:
            row = self.get_json(f"/api/v3/{self.resource}/{item_id}")
        except ClientError as exc:
            if exc.status == 404:
                return None
            raise
        return self.parse_item(row) if row else None


class SonarrClient(ArrClient):
    resource = "series"
    exclusion_param = "addImportListExclusion"

    def parse_item(self, row: dict[str, Any]) -> dict[str, Any]:
        stats = row.get("statistics") or {}
        return {
            "id": int(row.get("id", 0)),
            "instance": self.name,
            "kind": "show",
            "title": row.get("title") or "",
            "year": row.get("year"),
            "tvdb_id": str(row["tvdbId"]) if row.get("tvdbId") else None,
            "tmdb_id": str(row["tmdbId"]) if row.get("tmdbId") else None,
            "imdb_id": row.get("imdbId") or None,
            "status": row.get("status"),
            "ended": bool(row.get("ended", False)),
            "monitored": bool(row.get("monitored", False)),
            "tags": self.tag_labels(row.get("tags", [])),
            "path": row.get("path"),
            "added": parse_ts(row.get("added")),
            "size_bytes": int(stats.get("sizeOnDisk") or 0),
            "episode_count": int(stats.get("episodeFileCount") or 0),
        }


class RadarrClient(ArrClient):
    resource = "movie"
    exclusion_param = "addImportExclusion"

    def parse_item(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": int(row.get("id", 0)),
            "instance": self.name,
            "kind": "movie",
            "title": row.get("title") or "",
            "year": row.get("year"),
            "tmdb_id": str(row["tmdbId"]) if row.get("tmdbId") else None,
            "tvdb_id": None,
            "imdb_id": row.get("imdbId") or None,
            "status": row.get("status"),
            "ended": False,
            "monitored": bool(row.get("monitored", False)),
            "tags": self.tag_labels(row.get("tags", [])),
            "path": row.get("path"),
            "added": parse_ts(row.get("added")),
            "size_bytes": int(row.get("sizeOnDisk") or 0),
            "episode_count": 0,
            "has_file": bool(row.get("hasFile", False)),
        }


def build_arr(kind: str, cfg) -> ArrClient:
    cls = SonarrClient if kind == "sonarr" else RadarrClient
    return cls(cfg.name, cfg.url, cfg.api_key, timeout=cfg.timeout, verify_ssl=cfg.verify_ssl)
