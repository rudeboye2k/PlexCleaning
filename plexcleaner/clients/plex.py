"""Plex client.

Library inventory goes over raw HTTP: one JSON call per section with the
extra fields requested inline, which keeps a 10k-item library to a handful of
round trips instead of one per item. Account and sharing operations go through
plexapi, which already encodes the awkward plex.tv endpoints correctly.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from ..models import parse_ts
from .base import ClientError, HttpClient, ServiceStatus

log = logging.getLogger(__name__)

MOVIE, SHOW = "movie", "show"
PAGE = 500


def _tags(nodes: Any) -> list[str]:
    """Plex returns [{"tag": "keep"}]; be forgiving about anything else."""
    out = []
    for node in nodes or []:
        tag = node.get("tag") if isinstance(node, dict) else node
        if tag:
            out.append(str(tag))
    return out


class PlexClient(HttpClient):
    service = "plex"

    def __init__(self, url: str, token: str, *, timeout: int = 60, verify_ssl: bool = False):
        super().__init__(url, timeout=timeout, verify_ssl=verify_ssl, headers={"X-Plex-Token": token})
        self.token = token
        self._account = None
        self._machine_id: str | None = None

    # -- server ----------------------------------------------------------
    def identity(self) -> dict[str, Any]:
        body = self.get_json("/identity") or {}
        return body.get("MediaContainer", body)

    @property
    def machine_id(self) -> str:
        if self._machine_id is None:
            self._machine_id = str(self.identity().get("machineIdentifier") or "")
        return self._machine_id

    def test(self) -> ServiceStatus:
        try:
            info = self.identity()
            return ServiceStatus("plex", True, "connected", str(info.get("version", "")))
        except ClientError as exc:
            return ServiceStatus("plex", False, str(exc))

    # -- libraries -------------------------------------------------------
    def sections(self) -> list[dict[str, Any]]:
        body = self.get_json("/library/sections") or {}
        return body.get("MediaContainer", {}).get("Directory", []) or []

    def media_sections(self, include: Iterable[str] = (), exclude: Iterable[str] = ()) -> list[dict[str, Any]]:
        include_set = {s.strip().lower() for s in include if s and s.strip()}
        exclude_set = {s.strip().lower() for s in exclude if s and s.strip()}
        out = []
        for section in self.sections():
            if section.get("type") not in (MOVIE, SHOW):
                continue
            name = str(section.get("title", "")).lower()
            if include_set and name not in include_set:
                continue
            if name in exclude_set:
                continue
            out.append(section)
        return out

    def section_items(self, section_id: str) -> list[dict[str, Any]]:
        """All top-level items in a section, with GUIDs, labels and collections inline."""
        out: list[dict[str, Any]] = []
        offset = 0
        params = {
            "includeGuids": 1,
            "includeCollections": 1,
            "includeLabels": 1,
            "excludeAllLeaves": 1,
        }
        while True:
            headers = {"X-Plex-Container-Start": str(offset), "X-Plex-Container-Size": str(PAGE)}
            resp = self.request("GET", f"/library/sections/{section_id}/all", params=params, headers=headers)
            container = (resp.json() or {}).get("MediaContainer", {})
            rows = container.get("Metadata", []) or []
            out.extend(rows)
            total = int(container.get("totalSize") or container.get("size") or 0)
            offset += PAGE
            if not rows or offset >= total:
                break
        return out

    @staticmethod
    def parse_item(row: dict[str, Any], section: dict[str, Any]) -> dict[str, Any]:
        """Flatten one Plex metadata row into the fields the scanner cares about."""
        guids = {}
        for guid in row.get("Guid", []) or []:
            gid = str(guid.get("id", ""))
            if "://" in gid:
                scheme, _, value = gid.partition("://")
                guids[scheme] = value
        # Legacy agents put the id in the top-level guid string instead.
        legacy = str(row.get("guid", ""))
        for scheme in ("tmdb", "tvdb", "imdb"):
            if scheme not in guids and legacy.startswith(f"com.plexapp.agents.{scheme}"):
                guids[scheme] = legacy.split("://")[-1].split("?")[0]

        size = 0
        for media in row.get("Media", []) or []:
            for part in media.get("Part", []) or []:
                size += int(part.get("size") or 0)

        return {
            "rating_key": str(row.get("ratingKey", "")),
            "title": row.get("title") or "(untitled)",
            "year": row.get("year"),
            "type": row.get("type"),
            "section_id": str(section.get("key", "")),
            "library": section.get("title"),
            "tmdb_id": guids.get("tmdb"),
            "tvdb_id": guids.get("tvdb"),
            "imdb_id": guids.get("imdb"),
            "added_at": parse_ts(row.get("addedAt")),
            "last_viewed_at": parse_ts(row.get("lastViewedAt")),
            "view_count": int(row.get("viewCount") or 0),
            "leaf_count": int(row.get("leafCount") or 0),
            "viewed_leaf_count": int(row.get("viewedLeafCount") or 0),
            "size_bytes": size,
            "labels": _tags(row.get("Label")),
            "collections": _tags(row.get("Collection")),
        }

    # -- destructive -----------------------------------------------------
    def delete_item(self, rating_key: str) -> dict[str, Any]:
        """Delete an item and its files from Plex.

        Requires "Allow media deletion" to be enabled in the Plex server
        settings, otherwise Plex answers 401.
        """
        self.request("DELETE", f"/library/metadata/{rating_key}")
        return {"deleted": rating_key}

    def refresh_section(self, section_id: str) -> dict[str, Any]:
        self.request("GET", f"/library/sections/{section_id}/refresh")
        return {"refreshed": section_id}

    def empty_trash(self, section_id: str) -> dict[str, Any]:
        self.request("PUT", f"/library/sections/{section_id}/emptyTrash")
        return {"emptied": section_id}

    # -- account / sharing ------------------------------------------------
    @property
    def account(self):
        """Lazily built plexapi MyPlexAccount — only needed for user operations."""
        if self._account is None:
            try:
                from plexapi.myplex import MyPlexAccount
            except ImportError as exc:  # pragma: no cover
                raise ClientError(self.service, f"plexapi is required for user operations: {exc}") from exc
            try:
                self._account = MyPlexAccount(token=self.token)
            except Exception as exc:
                raise ClientError(self.service, f"could not reach plex.tv with this token: {exc}") from exc
        return self._account

    def shared_users(self) -> list[dict[str, Any]]:
        """Everyone with access to this server, flattened.

        `share_id` is the id of the share on *this* server and is what gets
        deleted to revoke access while leaving the friendship intact.
        """
        machine_id = self.machine_id
        out: list[dict[str, Any]] = []
        try:
            users = self.account.users()
        except Exception as exc:
            raise ClientError(self.service, f"could not list plex users: {exc}") from exc

        for user in users:
            share_id = None
            has_share = False
            for share in getattr(user, "servers", []) or []:
                if str(getattr(share, "machineIdentifier", "")) == machine_id:
                    has_share = True
                    share_id = str(getattr(share, "id", "") or "")
                    break
            out.append({
                "id": str(getattr(user, "id", "") or ""),
                "username": getattr(user, "username", "") or getattr(user, "title", "") or "",
                "title": getattr(user, "title", "") or "",
                "email": getattr(user, "email", None),
                "home": bool(getattr(user, "home", False)),
                "friend": bool(getattr(user, "friend", False)),
                "restricted": bool(getattr(user, "restricted", False)),
                "share_id": share_id,
                "has_share": has_share,
            })
        return out

    def owner_identity(self) -> dict[str, Any]:
        acct = self.account
        return {
            "id": str(getattr(acct, "id", "") or ""),
            "username": getattr(acct, "username", "") or "",
            "email": getattr(acct, "email", "") or "",
            "title": getattr(acct, "title", "") or "",
        }

    def unshare(self, username: str) -> dict[str, Any]:
        """Revoke this server's libraries from a user, keeping the friendship."""
        machine_id = self.machine_id
        user = self.account.user(username)
        for share in getattr(user, "servers", []) or []:
            if str(getattr(share, "machineIdentifier", "")) == machine_id:
                share.delete()
                return {"unshared": username, "machine_id": machine_id}
        # No share object to delete — fall back to removing every shared section.
        server = next(
            (r for r in self.account.resources() if str(getattr(r, "clientIdentifier", "")) == machine_id),
            None,
        )
        if server is None:
            return {"unshared": username, "note": "user had no share on this server"}
        self.account.updateFriend(user, server, sections=[], removeSections=True)
        return {"unshared": username, "machine_id": machine_id, "via": "updateFriend"}

    def remove_friend(self, username: str) -> dict[str, Any]:
        user = self.account.user(username)
        if getattr(user, "home", False):
            self.account.removeHomeUser(user)
            return {"removed_home_user": username}
        self.account.removeFriend(user)
        return {"removed_friend": username}
