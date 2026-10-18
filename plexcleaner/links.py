"""Deep links from a candidate row into the service that holds it.

Built server-side rather than in the browser so the URL shapes live in one
testable place, and so the templates stay dumb.

A caveat worth knowing: these are built from the URLs you configured, which are
what the *container* uses to reach each service. If you configured something
only the container can resolve — a Docker network alias like `http://plex:32400`
— the link will not open from your laptop. Configure addresses your browser can
also reach (an IP and port is the safe choice) and these just work.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .config import Config


@dataclass
class Link:
    service: str        # plex | tautulli | sonarr | radarr | seerr | tmdb | imdb
    label: str          # shown in the tooltip
    icon: str
    url: str
    external: bool = False   # leaves the LAN, so no referrer

    def to_dict(self) -> dict[str, Any]:
        return {"service": self.service, "label": self.label, "icon": self.icon,
                "url": self.url, "external": self.external}


def _base(url: str | None) -> str:
    return (url or "").rstrip("/")


class LinkBuilder:
    """Builds the icon row for a media item.

    `plex_machine_id` comes from the last scan; without it the Plex deep link
    cannot be addressed, so that one icon is simply omitted.
    """

    def __init__(self, cfg: Config, plex_machine_id: str = ""):
        self.cfg = cfg
        self.machine_id = plex_machine_id or ""

    def for_row(self, row: Any, detail: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        detail = detail or {}
        kind = row["kind"] if _has(row, "kind") else detail.get("kind", "movie")
        is_show = kind == "show"
        links: list[Link] = []

        rating_key = _get(row, "plex_rating_key")
        if self.cfg.plex.enabled and rating_key and self.machine_id and self.cfg.plex.url:
            key = quote(f"/library/metadata/{rating_key}", safe="")
            links.append(Link(
                "plex", "Open in Plex", "🎬",
                f"{_base(self.cfg.plex.url)}/web/index.html"
                f"#!/server/{self.machine_id}/details?key={key}",
            ))

        if self.cfg.tautulli.enabled and rating_key and self.cfg.tautulli.url:
            links.append(Link(
                "tautulli", "Watch history in Tautulli", "📊",
                f"{_base(self.cfg.tautulli.url)}/info?rating_key={rating_key}",
            ))

        instance = _get(row, "arr_instance")
        slug = detail.get("arr_slug")
        if instance:
            arr = self.cfg.arr_by_name(str(instance))
            if arr and arr.url:
                # Sonarr and Radarr both route their UI on the title slug; the
                # numeric id is an API detail and does not resolve in the UI.
                if slug:
                    path = f"/series/{slug}" if is_show else f"/movie/{slug}"
                    links.append(Link(instance, f"Open in {instance}",
                                      "📺" if is_show else "🍿",
                                      f"{_base(arr.url)}{path}"))
                else:
                    # Pre-existing scans have no slug stored; land on the list
                    # rather than dropping the link entirely.
                    links.append(Link(instance, f"Open {instance} (re-scan for a direct link)",
                                      "📺" if is_show else "🍿",
                                      f"{_base(arr.url)}/{'series' if is_show else 'movie'}"))

        tmdb = _get(row, "tmdb_id")
        if self.cfg.seerr.enabled and self.cfg.seerr.url and tmdb:
            # Seerr addresses both movies and TV by TMDB id, never TVDB.
            links.append(Link(
                "seerr", "Open in Seerr", "🎟️",
                f"{_base(self.cfg.seerr.url)}/{'tv' if is_show else 'movie'}/{tmdb}",
            ))

        if tmdb:
            links.append(Link(
                "tmdb", "Look up on TMDB", "🔎",
                f"https://www.themoviedb.org/{'tv' if is_show else 'movie'}/{tmdb}",
                external=True,
            ))

        imdb = _get(row, "imdb_id")
        if imdb:
            links.append(Link("imdb", "Look up on IMDb", "★",
                              f"https://www.imdb.com/title/{imdb}/", external=True))

        return [link.to_dict() for link in links]


def _has(row: Any, key: str) -> bool:
    try:
        return key in row.keys()
    except AttributeError:
        return isinstance(row, dict) and key in row


def _get(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None
