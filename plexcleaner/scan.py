"""The scan: inventory every service, merge into unified records, apply rules, persist.

A scan never changes anything. It only produces the candidate lists that a
plan is later built from.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

from .clients import ClientError, PlexClient, SeerrClient, TautulliClient
from .clients.arr import build_arr
from .config import Config
from .db import Database, utcnow
from .match import Index, match_user, seerr_kind
from .models import MediaItem, UserAccount, parse_ts
from .rules import evaluate_media, evaluate_users

log = logging.getLogger(__name__)


class Services:
    """Lazily constructed clients for whatever is enabled in the config."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.plex = PlexClient(cfg.plex.url, cfg.plex.token, timeout=cfg.plex.timeout,
                               verify_ssl=cfg.plex.verify_ssl) if cfg.plex.enabled else None
        self.tautulli = TautulliClient(cfg.tautulli.url, cfg.tautulli.api_key, timeout=cfg.tautulli.timeout,
                                       verify_ssl=cfg.tautulli.verify_ssl) if cfg.tautulli.enabled else None
        self.seerr = SeerrClient(cfg.seerr.url, cfg.seerr.api_key, timeout=cfg.seerr.timeout,
                                 verify_ssl=cfg.seerr.verify_ssl) if cfg.seerr.enabled else None
        self.sonarr = [build_arr("sonarr", c) for c in cfg.sonarr if c.enabled]
        self.radarr = [build_arr("radarr", c) for c in cfg.radarr if c.enabled]

    def arr_config(self, instance_name: str):
        for c in self.cfg.sonarr + self.cfg.radarr:
            if c.name == instance_name:
                return c
        return None

    def arr_client(self, instance_name: str):
        for client in self.sonarr + self.radarr:
            if client.name == instance_name:
                return client
        return None

    def test_all(self) -> list[dict[str, Any]]:
        out = []
        if self.plex:
            out.append(self.plex.test().to_dict())
        if self.tautulli:
            out.append(self.tautulli.test().to_dict())
        for client in self.sonarr + self.radarr:
            out.append(client.test().to_dict())
        if self.seerr:
            out.append(self.seerr.test().to_dict())
        return out

    def close(self) -> None:
        for client in [self.plex, self.tautulli, self.seerr, *self.sonarr, *self.radarr]:
            if client:
                try:
                    client.close()
                except Exception:  # pragma: no cover - best effort
                    pass


class Scanner:
    def __init__(self, cfg: Config, db: Database, services: Services | None = None):
        self.cfg = cfg
        self.db = db
        self.services = services or Services(cfg)
        self.errors: list[str] = []

    # -- entry point -----------------------------------------------------
    def run(self, trigger: str = "manual") -> int:
        scan_id = self.db.insert("scan", {"started_at": utcnow(), "status": "running", "trigger": trigger})
        log.info("scan %s started (%s)", scan_id, trigger)
        try:
            media = self.collect_media()
            users = self.collect_users()

            protected_media = self.db.protected_refs("media")
            protected_users = self.db.protected_refs("user")
            evaluate_media(media, self.cfg.rules.media, protected_refs=protected_media)
            evaluate_users(users, self.cfg.rules.users, protected_refs=protected_users)

            self.db.insert_many("media_item", [m.to_row(scan_id) for m in media])
            self.db.insert_many("user_account", [u.to_row(scan_id) for u in users])

            stats = self._stats(media, users)
            self.db.execute(
                "UPDATE scan SET finished_at = ?, status = ?, stats = ?, errors = ? WHERE id = ?",
                (utcnow(), "complete", json.dumps(stats), json.dumps(self.errors), scan_id),
            )
            self.db.audit(service="scan", action="scan_complete", target=f"scan:{scan_id}",
                          dry_run=True, ok=True, detail=stats)
            log.info("scan %s complete: %s", scan_id, stats)
            return scan_id
        except Exception as exc:
            log.exception("scan %s failed", scan_id)
            self.errors.append(str(exc))
            self.db.execute(
                "UPDATE scan SET finished_at = ?, status = ?, errors = ? WHERE id = ?",
                (utcnow(), "failed", json.dumps(self.errors), scan_id),
            )
            self.db.audit(service="scan", action="scan_failed", target=f"scan:{scan_id}",
                          dry_run=True, ok=False, detail={"error": str(exc)})
            raise

    @staticmethod
    def _stats(media: list[MediaItem], users: list[UserAccount]) -> dict[str, Any]:
        candidates = [m for m in media if m.verdict == "candidate"]
        return {
            "media_total": len(media),
            "media_candidates": len(candidates),
            "movies_total": sum(1 for m in media if m.kind == "movie"),
            "shows_total": sum(1 for m in media if m.kind == "show"),
            "reclaimable_bytes": sum(m.size_bytes for m in candidates),
            "users_total": len(users),
            "user_candidates": sum(1 for u in users if u.verdict == "candidate"),
        }

    def _note_error(self, where: str, exc: Exception) -> None:
        message = f"{where}: {exc}"
        log.warning(message)
        self.errors.append(message)

    # -- media -----------------------------------------------------------
    def collect_media(self) -> list[MediaItem]:
        items = self._plex_inventory()
        if not items:
            items = self._tautulli_inventory()
        else:
            self._apply_tautulli_watch_data(items)
        self._apply_arr_data(items)
        self._apply_seerr_data(items)
        return items

    def _plex_inventory(self) -> list[MediaItem]:
        plex = self.services.plex
        if not plex:
            return []
        items: list[MediaItem] = []
        try:
            sections = plex.media_sections(self.cfg.plex.libraries, self.cfg.plex.exclude_libraries)
        except ClientError as exc:
            self._note_error("plex sections", exc)
            return []

        for section in sections:
            try:
                rows = plex.section_items(str(section.get("key")))
            except ClientError as exc:
                self._note_error(f"plex library {section.get('title')}", exc)
                continue
            for row in rows:
                parsed = plex.parse_item(row, section)
                kind = "show" if parsed["type"] == "show" else "movie"
                if parsed["type"] not in ("show", "movie"):
                    continue
                leaf, viewed_leaf = parsed["leaf_count"], parsed["viewed_leaf_count"]
                items.append(MediaItem(
                    kind=kind,
                    title=parsed["title"],
                    year=parsed["year"],
                    plex_rating_key=parsed["rating_key"],
                    plex_section_id=parsed["section_id"],
                    plex_library=parsed["library"],
                    tmdb_id=parsed["tmdb_id"],
                    tvdb_id=parsed["tvdb_id"],
                    imdb_id=parsed["imdb_id"],
                    added_at=parsed["added_at"],
                    last_watched_at=parsed["last_viewed_at"],
                    play_count=parsed["view_count"],
                    size_bytes=parsed["size_bytes"],
                    episode_count=leaf,
                    in_progress=bool(kind == "show" and leaf and 0 < viewed_leaf < leaf),
                    labels=parsed["labels"],
                    collections=parsed["collections"],
                    detail={"source": "plex", "viewed_leaf_count": viewed_leaf},
                ))
        log.info("plex inventory: %d items across %d libraries", len(items), len(sections))
        return items

    def _tautulli_inventory(self) -> list[MediaItem]:
        """Fallback inventory when Plex is unreachable or disabled."""
        tautulli = self.services.tautulli
        if not tautulli:
            return []
        items: list[MediaItem] = []
        try:
            libraries = tautulli.libraries()
        except ClientError as exc:
            self._note_error("tautulli libraries", exc)
            return []
        excluded = {n.lower() for n in self.cfg.plex.exclude_libraries}
        for library in libraries:
            section_type = library.get("section_type")
            if section_type not in ("movie", "show"):
                continue
            if str(library.get("section_name", "")).lower() in excluded:
                continue
            try:
                rows = tautulli.library_media_info(str(library.get("section_id")), section_type)
            except ClientError as exc:
                self._note_error(f"tautulli library {library.get('section_name')}", exc)
                continue
            for row in rows:
                items.append(MediaItem(
                    kind="show" if section_type == "show" else "movie",
                    title=row.get("title") or "(untitled)",
                    year=_int_or_none(row.get("year")),
                    plex_rating_key=str(row.get("rating_key") or "") or None,
                    plex_section_id=str(library.get("section_id")),
                    plex_library=library.get("section_name"),
                    added_at=parse_ts(row.get("added_at")),
                    last_watched_at=parse_ts(row.get("last_played")),
                    play_count=int(row.get("play_count") or 0),
                    size_bytes=int(row.get("file_size") or 0),
                    detail={"source": "tautulli"},
                ))
        log.info("tautulli inventory (plex unavailable): %d items", len(items))
        return items

    def _apply_tautulli_watch_data(self, items: list[MediaItem]) -> None:
        """Overlay Tautulli's server-wide watch state onto the Plex inventory.

        Plex's own lastViewedAt reflects the owning account only, so a show
        watched daily by a shared user can look untouched. Tautulli sees every
        account, which is why it wins wherever both have an opinion.
        """
        tautulli = self.services.tautulli
        if not tautulli:
            return

        by_rating_key = {i.plex_rating_key: i for i in items if i.plex_rating_key}
        sections = {i.plex_section_id for i in items if i.plex_section_id}

        # 1. Per-library last_played / play_count / file_size.
        for section_id in sorted(s for s in sections if s):
            try:
                rows = tautulli.library_media_info(section_id)
            except ClientError as exc:
                self._note_error(f"tautulli media info for section {section_id}", exc)
                continue
            for row in rows:
                item = by_rating_key.get(str(row.get("rating_key") or ""))
                if not item:
                    continue
                last_played = parse_ts(row.get("last_played"))
                if last_played and (not item.last_watched_at or last_played > item.last_watched_at):
                    item.last_watched_at = last_played
                item.play_count = max(item.play_count, int(row.get("play_count") or 0))
                if not item.size_bytes:
                    item.size_bytes = int(row.get("file_size") or 0)

        # 2. Recent history sweep — catches episodes whose parent show row is
        #    stale, and gives us the distinct-watcher count per item.
        window = max(self.cfg.rules.media.unwatched_days, self.cfg.rules.media.never_watched_after_days) + 1
        try:
            history = tautulli.history_since(window)
        except ClientError as exc:
            self._note_error("tautulli history", exc)
            history = []

        watchers: dict[str, set[str]] = defaultdict(set)
        latest: dict[str, datetime] = {}
        for row in history:
            stamp = parse_ts(row.get("stopped") or row.get("date"))
            if not stamp:
                continue
            user = str(row.get("user_id") or row.get("user") or "")
            for key_field in ("rating_key", "parent_rating_key", "grandparent_rating_key"):
                key = str(row.get(key_field) or "")
                if not key or key == "0":
                    continue
                if user:
                    watchers[key].add(user)
                if key not in latest or stamp > latest[key]:
                    latest[key] = stamp

        for key, stamp in latest.items():
            item = by_rating_key.get(key)
            if item and (not item.last_watched_at or stamp > item.last_watched_at):
                item.last_watched_at = stamp
        for key, users in watchers.items():
            item = by_rating_key.get(key)
            if item:
                item.watcher_count = max(item.watcher_count, len(users))

        log.info("tautulli watch overlay applied from %d history rows", len(history))

    def _apply_arr_data(self, items: list[MediaItem]) -> None:
        index = Index()
        for client in self.services.sonarr + self.services.radarr:
            try:
                records = client.all_items()
            except ClientError as exc:
                self._note_error(f"{client.name} inventory", exc)
                continue
            index.add_all(records)
            log.info("%s inventory: %d items", client.name, len(records))

        for item in items:
            hit = index.find_for(item)
            if not hit:
                continue
            item.arr_instance = hit["instance"]
            item.arr_id = hit["id"]
            item.arr_status = hit.get("status")
            item.arr_monitored = bool(hit.get("monitored"))
            item.arr_tags = list(hit.get("tags") or [])
            item.arr_path = hit.get("path")
            # *arr knows the true on-disk size, including files Plex has not
            # scanned yet, so prefer it when Plex reported nothing.
            if not item.size_bytes and hit.get("size_bytes"):
                item.size_bytes = int(hit["size_bytes"])
            if not item.episode_count and hit.get("episode_count"):
                item.episode_count = int(hit["episode_count"])
            if not item.tvdb_id and hit.get("tvdb_id"):
                item.tvdb_id = hit["tvdb_id"]
            if not item.tmdb_id and hit.get("tmdb_id"):
                item.tmdb_id = hit["tmdb_id"]
            if not item.imdb_id and hit.get("imdb_id"):
                item.imdb_id = hit["imdb_id"]

    def _apply_seerr_data(self, items: list[MediaItem]) -> None:
        seerr = self.services.seerr
        if not seerr:
            return
        try:
            media_rows = seerr.media()
        except ClientError as exc:
            self._note_error("seerr media", exc)
            return
        try:
            request_rows = seerr.requests()
        except ClientError as exc:
            self._note_error("seerr requests", exc)
            request_rows = []

        index = Index()
        for row in media_rows:
            index.add({**row, "kind": seerr_kind(row.get("media_type")), "title": None, "year": None})

        # Latest request per media id, so "who asked for this" reflects the
        # most recent interest rather than the original one.
        latest_request: dict[int, dict[str, Any]] = {}
        for req in request_rows:
            mid = req.get("media_id")
            if not mid:
                continue
            current = latest_request.get(mid)
            if not current or (req.get("requested_at") and current.get("requested_at")
                               and req["requested_at"] > current["requested_at"]):
                latest_request[mid] = req
            elif not current.get("requested_at"):
                latest_request[mid] = req

        for item in items:
            hit = index.find(
                item.kind, tmdb_id=item.tmdb_id, tvdb_id=item.tvdb_id,
                imdb_id=item.imdb_id, rating_key=item.plex_rating_key,
            )
            if not hit:
                continue
            item.seerr_media_id = hit["id"]
            request = latest_request.get(hit["id"])
            if request:
                item.seerr_requested_by = request.get("requested_by")
                item.seerr_requested_at = request.get("requested_at")

    # -- users -----------------------------------------------------------
    def collect_users(self) -> list[UserAccount]:
        users: dict[str, UserAccount] = {}
        owner_email = ""

        # Plex is the authority on who has access at all.
        if self.services.plex:
            try:
                owner = self.services.plex.owner_identity()
                owner_email = (owner.get("email") or "").lower()
                users[_ukey(owner.get("email"), owner.get("username"))] = UserAccount(
                    username=owner.get("username") or "owner",
                    email=owner.get("email"),
                    title=owner.get("title"),
                    plex_user_id=owner.get("id"),
                    is_admin=True,
                    has_plex_share=True,
                    detail={"source": "plex-owner"},
                )
            except ClientError as exc:
                self._note_error("plex owner", exc)
            try:
                for row in self.services.plex.shared_users():
                    key = _ukey(row.get("email"), row.get("username"))
                    users[key] = UserAccount(
                        username=row.get("username") or row.get("title") or key,
                        email=row.get("email"),
                        title=row.get("title"),
                        plex_user_id=row.get("id"),
                        plex_share_id=row.get("share_id"),
                        is_home=bool(row.get("home")),
                        has_plex_share=bool(row.get("has_share")),
                        detail={"source": "plex", "restricted": row.get("restricted")},
                    )
            except ClientError as exc:
                self._note_error("plex users", exc)

        # Tautulli supplies the last-watched timestamp and play counts.
        if self.services.tautulli:
            try:
                rows = self.services.tautulli.users()
            except ClientError as exc:
                self._note_error("tautulli users", exc)
                rows = []
            for row in rows:
                username = row.get("username") or row.get("friendly_name") or ""
                email = row.get("email")
                key = _ukey(email, username)
                user = users.get(key)
                if user is None:
                    user = UserAccount(username=username or key, email=email,
                                       title=row.get("friendly_name"), detail={"source": "tautulli"})
                    users[key] = user
                user.tautulli_user_id = str(row.get("user_id") or "") or None
                last_seen = parse_ts(row.get("last_seen"))
                if last_seen and (not user.last_seen_at or last_seen > user.last_seen_at):
                    user.last_seen_at = last_seen
                user.plays = max(user.plays, int(row.get("plays") or 0))
                if str(row.get("is_admin") or "") in ("1", "True", "true"):
                    user.is_admin = True
                if row.get("is_active") in (0, "0", False):
                    user.detail["tautulli_inactive"] = True

        # Seerr supplies last login, which matters for people who request a lot
        # but stream through a different client.
        if self.services.seerr:
            try:
                rows = self.services.seerr.users()
            except ClientError as exc:
                self._note_error("seerr users", exc)
                rows = []
            pool = [{"email": u.email, "username": u.username, "title": u.title,
                     "plex_id": u.plex_user_id, "_key": key} for key, u in users.items()]
            for row in rows:
                hit = match_user(pool, email=row.get("email"), username=row.get("plex_username")
                                 or row.get("display_name"), plex_id=row.get("plex_id"))
                if hit:
                    user = users[hit["_key"]]
                else:
                    key = _ukey(row.get("email"), row.get("plex_username") or row.get("display_name"))
                    user = users.setdefault(key, UserAccount(
                        username=row.get("plex_username") or row.get("display_name") or key,
                        email=row.get("email"), detail={"source": "seerr"},
                    ))
                user.seerr_user_id = row["id"]
                login = row.get("last_login_at")
                if login and (not user.last_login_at or login > user.last_login_at):
                    user.last_login_at = login
                user.request_count = max(user.request_count, int(row.get("request_count") or 0))
                if not user.first_seen_at:
                    user.first_seen_at = row.get("created_at")
                if row.get("is_admin"):
                    user.is_admin = True

        if owner_email:
            for user in users.values():
                if (user.email or "").lower() == owner_email:
                    user.is_admin = True

        log.info("collected %d users", len(users))
        return list(users.values())


def _ukey(email: str | None, username: str | None) -> str:
    return (email or username or "unknown").strip().lower()


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
