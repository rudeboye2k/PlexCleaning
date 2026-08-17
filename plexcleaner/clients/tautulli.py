"""Tautulli client. Tautulli is the source of truth for *who watched what, when*.

Everything goes through /api/v2 with cmd= and apikey=. Responses are shaped as
{"response": {"result": "success", "data": ...}}.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .base import ClientError, HttpClient, ServiceStatus

log = logging.getLogger(__name__)

PAGE = 1000


class TautulliClient(HttpClient):
    service = "tautulli"

    def __init__(self, url: str, api_key: str, *, timeout: int = 60, verify_ssl: bool = False):
        super().__init__(url, timeout=timeout, verify_ssl=verify_ssl)
        self.api_key = api_key

    def cmd(self, command: str, **params: Any) -> Any:
        payload = {"apikey": self.api_key, "cmd": command, **{k: v for k, v in params.items() if v is not None}}
        body = self.get_json("/api/v2", params=payload)
        if not isinstance(body, dict) or "response" not in body:
            raise ClientError(self.service, f"unexpected response to {command}: {str(body)[:200]}")
        response = body["response"]
        if response.get("result") != "success":
            raise ClientError(self.service, f"{command} failed: {response.get('message') or response}")
        return response.get("data")

    def test(self) -> ServiceStatus:
        try:
            info = self.cmd("get_tautulli_info") or {}
            version = info.get("tautulli_version") or info.get("version") or ""
            return ServiceStatus("tautulli", True, "connected", str(version))
        except ClientError as exc:
            # Older builds do not expose get_tautulli_info; fall back to a cheap call.
            try:
                self.cmd("get_server_friendly_name")
                return ServiceStatus("tautulli", True, "connected", "")
            except ClientError:
                return ServiceStatus("tautulli", False, str(exc))

    # -- libraries -------------------------------------------------------
    def libraries(self) -> list[dict[str, Any]]:
        data = self.cmd("get_libraries") or []
        return data if isinstance(data, list) else data.get("data", [])

    def library_media_info(self, section_id: str, section_type: str | None = None) -> list[dict[str, Any]]:
        """Per-item last_played / play_count for a whole library section.

        This is the cheapest way to get watch state for thousands of items —
        one paginated call per library rather than one call per item.
        """
        out: list[dict[str, Any]] = []
        start = 0
        while True:
            data = self.cmd(
                "get_library_media_info",
                section_id=section_id,
                section_type=section_type,
                start=start,
                length=PAGE,
                refresh="false",
            ) or {}
            rows = data.get("data", []) if isinstance(data, dict) else []
            out.extend(rows)
            total = int(data.get("recordsFiltered") or data.get("recordsTotal") or 0) if isinstance(data, dict) else 0
            start += PAGE
            if not rows or start >= total:
                break
        return out

    # -- history ---------------------------------------------------------
    def history_since(self, days: int, section_id: str | None = None) -> list[dict[str, Any]]:
        """All playback events in the last N days.

        Used as the authoritative "was this touched recently" signal, and to
        count distinct watchers per item.
        """
        after = (datetime.now(timezone.utc) - timedelta(days=max(days, 1))).strftime("%Y-%m-%d")
        out: list[dict[str, Any]] = []
        start = 0
        while True:
            data = self.cmd(
                "get_history",
                after=after,
                section_id=section_id,
                start=start,
                length=PAGE,
                order_column="date",
                order_dir="desc",
            ) or {}
            rows = data.get("data", []) if isinstance(data, dict) else []
            out.extend(rows)
            total = int(data.get("recordsFiltered") or data.get("recordsTotal") or 0) if isinstance(data, dict) else 0
            start += PAGE
            if not rows or start >= total or start > 200_000:
                break
        return out

    def last_history_for_user(self, user_id: str) -> dict[str, Any] | None:
        data = self.cmd(
            "get_history", user_id=user_id, length=1, start=0,
            order_column="date", order_dir="desc",
        ) or {}
        rows = data.get("data", []) if isinstance(data, dict) else []
        return rows[0] if rows else None

    # -- users -----------------------------------------------------------
    def users(self) -> list[dict[str, Any]]:
        """Users with last_seen where available.

        get_users_table carries last_seen/plays but is not part of the
        documented API on every build, so fall back to get_users and
        backfill last_seen from each user's most recent history row.
        """
        try:
            data = self.cmd("get_users_table", length=PAGE, order_column="last_seen", order_dir="desc") or {}
            rows = data.get("data", []) if isinstance(data, dict) else []
            if rows:
                return rows
        except ClientError as exc:
            log.debug("get_users_table unavailable, falling back to get_users: %s", exc)

        rows = self.cmd("get_users") or []
        rows = rows if isinstance(rows, list) else []
        for row in rows:
            if row.get("last_seen"):
                continue
            try:
                last = self.last_history_for_user(str(row.get("user_id")))
            except ClientError:
                last = None
            if last:
                row["last_seen"] = last.get("stopped") or last.get("date")
        return rows

    # -- destructive -----------------------------------------------------
    def delete_user(self, user_id: str) -> Any:
        return self.cmd("delete_user", user_id=user_id)

    def delete_user_history(self, user_id: str) -> Any:
        return self.cmd("delete_all_user_history", user_id=user_id)

    def delete_history_rows(self, row_ids: list[str]) -> Any:
        return self.cmd("delete_history", row_ids=",".join(str(r) for r in row_ids))
