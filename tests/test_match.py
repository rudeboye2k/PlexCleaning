from __future__ import annotations

from plexcleaner.match import Index, match_user, normalize_title, seerr_kind, title_key
from plexcleaner.models import MediaItem


class TestNormalisation:
    def test_strips_articles_and_punctuation(self):
        assert normalize_title("The Lord of the Rings!") == "lordoftherings"
        assert normalize_title("Spider-Man: No Way Home") == "spidermannowayhome"

    def test_title_key_includes_year(self):
        assert title_key("Dune", 2021) == "dune|2021"
        assert title_key("Dune", None) == "dune|"

    def test_seerr_kind_maps_tv_to_show(self):
        assert seerr_kind("tv") == "show"
        assert seerr_kind("movie") == "movie"


class TestIndex:
    def test_matches_on_tmdb_id(self):
        index = Index()
        index.add({"kind": "movie", "id": 1, "tmdb_id": "603", "title": "The Matrix", "year": 1999})
        item = MediaItem(kind="movie", title="Matrix, The", year=1999, tmdb_id="603")
        assert index.find_for(item)["id"] == 1

    def test_ids_are_namespaced_so_they_cannot_collide(self):
        """A TMDB id of 603 must never match a TVDB id of 603."""
        index = Index()
        index.add({"kind": "show", "id": 1, "tvdb_id": "603", "title": "Show A", "year": 2000})
        item = MediaItem(kind="show", title="Different", year=1990, tmdb_id="603")
        assert index.find_for(item) is None

    def test_kind_must_agree(self):
        index = Index()
        index.add({"kind": "movie", "id": 1, "tmdb_id": "77", "title": "Thing", "year": 2000})
        item = MediaItem(kind="show", title="Thing", year=2000, tmdb_id="77")
        assert index.find_for(item) is None

    def test_falls_back_to_title_and_year(self):
        index = Index()
        index.add({"kind": "movie", "id": 5, "title": "The Thing", "year": 1982})
        item = MediaItem(kind="movie", title="Thing, The", year=1982)
        assert index.find_for(item) is None  # "Thing, The" normalises differently
        exact = MediaItem(kind="movie", title="The Thing", year=1982)
        assert index.find_for(exact)["id"] == 5

    def test_ambiguous_title_match_is_refused(self):
        """Two 1982 films called The Thing means we must not guess."""
        index = Index()
        index.add({"kind": "movie", "id": 5, "title": "The Thing", "year": 1982})
        index.add({"kind": "movie", "id": 6, "title": "The Thing", "year": 1982})
        item = MediaItem(kind="movie", title="The Thing", year=1982)
        assert index.find_for(item) is None

    def test_id_match_beats_title_match(self):
        index = Index()
        index.add({"kind": "movie", "id": 1, "tmdb_id": "111", "title": "Wrong Title", "year": 1999})
        index.add({"kind": "movie", "id": 2, "title": "Right Title", "year": 1999})
        item = MediaItem(kind="movie", title="Right Title", year=1999, tmdb_id="111")
        assert index.find_for(item)["id"] == 1

    def test_plex_rating_key_match(self):
        index = Index()
        index.add({"kind": "movie", "id": 3, "rating_key": "9001", "title": "X", "year": 2020})
        item = MediaItem(kind="movie", title="Unknown", plex_rating_key="9001")
        assert index.find_for(item)["id"] == 3


class TestUserMatching:
    POOL = [
        {"email": "a@example.com", "username": "alice", "plex_id": "1", "_key": "a"},
        {"email": "b@example.com", "username": "bob", "plex_id": "2", "_key": "b"},
    ]

    def test_plex_id_wins_over_email(self):
        hit = match_user(self.POOL, email="b@example.com", plex_id="1")
        assert hit["_key"] == "a"

    def test_email_is_case_insensitive(self):
        assert match_user(self.POOL, email="B@EXAMPLE.COM")["_key"] == "b"

    def test_username_fallback(self):
        assert match_user(self.POOL, username="Bob")["_key"] == "b"

    def test_no_match_returns_none(self):
        assert match_user(self.POOL, email="nobody@example.com", username="nobody") is None

    def test_blank_identifiers_do_not_match_blank_fields(self):
        pool = [{"email": None, "username": "", "plex_id": None, "_key": "ghost"}]
        assert match_user(pool, email="", username="") is None
