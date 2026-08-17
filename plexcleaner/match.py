"""Cross-service identity matching.

Every service names the same show differently, so items are matched on a
priority ladder: external database ids first (exact and unambiguous), then
Plex rating key, then a normalised title+year as a last resort.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable

_ARTICLES = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)
_NOISE = re.compile(r"[^a-z0-9]+")


def normalize_title(title: str | None) -> str:
    if not title:
        return ""
    text = _ARTICLES.sub("", str(title).strip().lower())
    return _NOISE.sub("", text)


def title_key(title: str | None, year: Any) -> str:
    norm = normalize_title(title)
    try:
        year_part = str(int(year)) if year else ""
    except (TypeError, ValueError):
        year_part = ""
    return f"{norm}|{year_part}"


class Index:
    """Multi-key lookup over a collection of records.

    Records are dicts. Ids are registered under namespaced keys so a TMDB id
    of "603" can never collide with a TVDB id of "603".
    """

    def __init__(self, kind_field: str = "kind"):
        self.kind_field = kind_field
        self._by_id: dict[str, Any] = {}
        self._by_title: dict[str, list[Any]] = {}

    @staticmethod
    def _id_keys(record: Any, get: Callable[[Any, str], Any]) -> list[str]:
        keys = []
        for field, prefix in (("tmdb_id", "tmdb"), ("tvdb_id", "tvdb"), ("imdb_id", "imdb"),
                              ("rating_key", "plex"), ("plex_rating_key", "plex")):
            value = get(record, field)
            if value:
                keys.append(f"{prefix}:{value}")
        return keys

    def add(self, record: Any, get: Callable[[Any, str], Any] | None = None) -> None:
        get = get or _dict_get
        kind = get(record, self.kind_field) or ""
        for key in self._id_keys(record, get):
            self._by_id.setdefault(f"{kind}|{key}", record)
        tkey = f"{kind}|{title_key(get(record, 'title'), get(record, 'year'))}"
        if not tkey.endswith("|"):
            self._by_title.setdefault(tkey, []).append(record)

    def add_all(self, records: Iterable[Any], get: Callable[[Any, str], Any] | None = None) -> None:
        for record in records:
            self.add(record, get)

    def find(self, kind: str, *, tmdb_id: str | None = None, tvdb_id: str | None = None,
             imdb_id: str | None = None, rating_key: str | None = None,
             title: str | None = None, year: Any = None) -> Any | None:
        for prefix, value in (("tmdb", tmdb_id), ("tvdb", tvdb_id), ("imdb", imdb_id), ("plex", rating_key)):
            if not value:
                continue
            hit = self._by_id.get(f"{kind}|{prefix}:{value}")
            if hit is not None:
                return hit
        if title:
            matches = self._by_title.get(f"{kind}|{title_key(title, year)}", [])
            # Only trust a title match when it is unambiguous.
            if len(matches) == 1:
                return matches[0]
        return None

    def find_for(self, item) -> Any | None:
        """Convenience wrapper for a MediaItem."""
        return self.find(
            item.kind,
            tmdb_id=item.tmdb_id,
            tvdb_id=item.tvdb_id,
            imdb_id=item.imdb_id,
            rating_key=item.plex_rating_key,
            title=item.title,
            year=item.year,
        )


def _dict_get(record: Any, field: str) -> Any:
    if isinstance(record, dict):
        return record.get(field)
    return getattr(record, field, None)


def seerr_kind(media_type: str | None) -> str:
    """Seerr says "tv"/"movie"; internally shows are "show"."""
    return "show" if str(media_type or "").lower() in {"tv", "show", "series"} else "movie"


def match_user(candidates: Iterable[dict[str, Any]], *, email: str | None = None,
               username: str | None = None, plex_id: str | None = None) -> dict[str, Any] | None:
    """Find a user record by plex id, then email, then username — in that order."""
    email_l = (email or "").strip().lower()
    user_l = (username or "").strip().lower()
    pool = list(candidates)

    if plex_id:
        for row in pool:
            if str(row.get("plex_id") or row.get("plex_user_id") or "") == str(plex_id):
                return row
    if email_l:
        for row in pool:
            if str(row.get("email") or "").strip().lower() == email_l:
                return row
    if user_l:
        for row in pool:
            names = {
                str(row.get("username") or "").strip().lower(),
                str(row.get("plex_username") or "").strip().lower(),
                str(row.get("display_name") or "").strip().lower(),
                str(row.get("title") or "").strip().lower(),
            }
            if user_l in names - {""}:
                return row
    return None
