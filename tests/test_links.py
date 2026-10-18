"""Per-service deep links shown under each media title."""
from __future__ import annotations

from plexcleaner.links import LinkBuilder


def build(cfg, row: dict, detail: dict | None = None, machine_id: str = "machine-1"):
    links = LinkBuilder(cfg, machine_id).for_row(row, detail or {})
    return {link["service"]: link["url"] for link in links}


MOVIE = {"kind": "movie", "plex_rating_key": "101", "arr_instance": "radarr",
         "tmdb_id": "500", "imdb_id": "tt0500"}
SHOW = {"kind": "show", "plex_rating_key": "202", "arr_instance": "sonarr",
        "tmdb_id": "1399", "imdb_id": "tt0944947"}


class TestPlex:
    def test_link_targets_the_item(self, cfg):
        url = build(cfg, MOVIE)["plex"]
        assert url.startswith("http://plex:32400/web/index.html#!/server/machine-1/details?key=")
        # The metadata path must be percent-encoded or Plex Web drops it.
        assert "%2Flibrary%2Fmetadata%2F101" in url

    def test_omitted_without_a_machine_id(self, cfg):
        """A scan predating the machine-id capture cannot address Plex."""
        assert "plex" not in build(cfg, MOVIE, machine_id="")

    def test_omitted_when_plex_is_disabled(self, cfg):
        cfg.plex.enabled = False
        assert "plex" not in build(cfg, MOVIE)

    def test_omitted_without_a_rating_key(self, cfg):
        assert "plex" not in build(cfg, {**MOVIE, "plex_rating_key": None})


class TestArr:
    def test_radarr_uses_the_movie_slug(self, cfg):
        url = build(cfg, MOVIE, {"arr_slug": "old-movie-2001"})["radarr"]
        assert url == "http://radarr:7878/movie/old-movie-2001"

    def test_sonarr_uses_the_series_slug(self, cfg):
        url = build(cfg, SHOW, {"arr_slug": "old-show"})["sonarr"]
        assert url == "http://sonarr:8989/series/old-show"

    def test_falls_back_to_the_list_without_a_slug(self, cfg):
        """Rows from an older scan have no slug; still give them somewhere useful."""
        assert build(cfg, MOVIE)["radarr"] == "http://radarr:7878/movie"
        assert build(cfg, SHOW)["sonarr"] == "http://sonarr:8989/series"

    def test_omitted_when_no_arr_matched(self, cfg):
        links = build(cfg, {**MOVIE, "arr_instance": None})
        assert "radarr" not in links and "sonarr" not in links

    def test_unknown_instance_name_is_skipped(self, cfg):
        """A renamed instance must not produce a link to nowhere."""
        assert build(cfg, {**MOVIE, "arr_instance": "radarr-4k"}) .get("radarr-4k") is None


class TestSeerr:
    def test_movies_use_the_movie_route(self, cfg):
        assert build(cfg, MOVIE)["seerr"] == "http://seerr:5055/movie/500"

    def test_shows_use_the_tv_route_with_the_tmdb_id(self, cfg):
        """Seerr addresses TV by TMDB id, never TVDB — this is the easy mistake."""
        assert build(cfg, SHOW)["seerr"] == "http://seerr:5055/tv/1399"

    def test_omitted_without_a_tmdb_id(self, cfg):
        assert "seerr" not in build(cfg, {**SHOW, "tmdb_id": None})

    def test_omitted_when_seerr_is_disabled(self, cfg):
        cfg.seerr.enabled = False
        assert "seerr" not in build(cfg, MOVIE)


class TestExternal:
    def test_tmdb_and_imdb_lookups(self, cfg):
        links = build(cfg, SHOW)
        assert links["tmdb"] == "https://www.themoviedb.org/tv/1399"
        assert links["imdb"] == "https://www.imdb.com/title/tt0944947/"

    def test_movie_tmdb_route(self, cfg):
        assert build(cfg, MOVIE)["tmdb"] == "https://www.themoviedb.org/movie/500"

    def test_external_links_are_flagged_so_the_referrer_is_dropped(self, cfg):
        links = {l["service"]: l for l in LinkBuilder(cfg, "m").for_row(MOVIE, {})}
        assert links["tmdb"]["external"] is True
        assert links["imdb"]["external"] is True
        # Internal ones keep the referrer; they are on the user's own LAN.
        assert links["plex"]["external"] is False


class TestTautulli:
    def test_links_to_the_item_history(self, cfg):
        assert build(cfg, MOVIE)["tautulli"] == "http://tautulli:8181/info?rating_key=101"

    def test_omitted_when_disabled(self, cfg):
        cfg.tautulli.enabled = False
        assert "tautulli" not in build(cfg, MOVIE)


class TestGeneral:
    def test_trailing_slashes_do_not_double_up(self, cfg):
        cfg.plex.url = "http://plex:32400/"
        cfg.seerr.url = "http://seerr:5055/"
        links = build(cfg, MOVIE)
        assert "32400//web" not in links["plex"]
        assert links["seerr"] == "http://seerr:5055/movie/500"

    def test_an_item_with_nothing_known_gets_no_links(self, cfg):
        assert build(cfg, {"kind": "movie"}) == {}

    def test_every_link_has_an_icon_and_a_label(self, cfg):
        for link in LinkBuilder(cfg, "m").for_row(SHOW, {"arr_slug": "s"}):
            assert link["icon"] and link["label"] and link["url"]
