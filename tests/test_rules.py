from __future__ import annotations

from plexcleaner.config import MediaRules, UserRules
from plexcleaner.rules import evaluate_media, evaluate_users

from conftest import NOW, ago


def verdict(item, rules=None, **kw):
    evaluate_media([item], rules or MediaRules(), now=NOW, **kw)
    return item.verdict


def user_verdict(user, rules=None, **kw):
    evaluate_users([user], rules or UserRules(), now=NOW, **kw)
    return user.verdict


class TestMediaStaleness:
    def test_long_unwatched_is_a_candidate(self, movie):
        assert verdict(movie) == "candidate"
        assert "700d ago" in movie.reasons[0]

    def test_recently_watched_is_kept(self, movie):
        movie.last_watched_at = ago(30)
        assert verdict(movie) == "keep"

    def test_never_watched_past_grace_is_a_candidate(self, movie):
        movie.last_watched_at = None
        movie.added_at = ago(400)
        assert verdict(movie) == "candidate"
        assert "Never watched" in movie.reasons[0]

    def test_never_watched_inside_grace_is_kept(self, movie):
        movie.last_watched_at = None
        movie.added_at = ago(90)
        assert verdict(movie) == "keep"

    def test_recently_added_is_never_proposed(self, movie):
        """Even a never-watched item is left alone below min_age_days."""
        movie.last_watched_at = None
        movie.added_at = ago(5)
        assert verdict(movie) == "keep"

    def test_missing_added_date_is_not_proposed(self, movie):
        movie.last_watched_at = None
        movie.added_at = None
        assert verdict(movie) == "keep"

    def test_size_floor_keeps_small_items(self, movie):
        movie.size_bytes = 50_000_000
        assert verdict(movie, MediaRules(min_size_mb=500)) == "keep"


class TestMediaProtections:
    # The keep-lists are empty by default and come from config.yaml, so these
    # tests pass them explicitly. Matching is case-insensitive.
    KEEPERS = MediaRules(
        protect_plex_labels=["keep", "favorite"],
        protect_collections=["Christmas"],
        protect_arr_tags=["permanent"],
    )

    def test_plex_label_protects(self, movie):
        movie.labels = ["Keep"]
        assert verdict(movie, self.KEEPERS) == "protected"

    def test_collection_protects(self, movie):
        movie.collections = ["christmas"]
        assert verdict(movie, self.KEEPERS) == "protected"

    def test_arr_tag_protects(self, movie):
        movie.arr_tags = ["permanent"]
        assert verdict(movie, self.KEEPERS) == "protected"

    def test_continuing_series_protected(self, show):
        show.arr_status = "continuing"
        show.arr_monitored = True
        assert verdict(show) == "protected"

    def test_ended_series_still_a_candidate(self, show):
        assert verdict(show) == "candidate"

    def test_continuing_but_unmonitored_is_not_protected(self, show):
        show.arr_status = "continuing"
        show.arr_monitored = False
        assert verdict(show) == "candidate"

    def test_in_progress_protected(self, show):
        show.in_progress = True
        assert verdict(show) == "protected"

    def test_recent_seerr_request_protects(self, movie):
        movie.seerr_requested_at = ago(10)
        movie.seerr_requested_by = "friend@example.com"
        assert verdict(movie) == "protected"
        assert "Requested in Seerr" in movie.reasons[0]

    def test_old_seerr_request_does_not_protect(self, movie):
        movie.seerr_requested_at = ago(400)
        assert verdict(movie) == "candidate"

    def test_permanent_keep_list_wins(self, movie):
        assert verdict(movie, protected_refs={movie.ref}) == "protected"

    def test_library_not_in_include_list_is_protected(self, movie):
        assert verdict(movie, MediaRules(include_libraries=["Anime"])) == "protected"

    def test_protection_beats_staleness(self, movie):
        """An item can be both ancient and protected — protection must win."""
        movie.last_watched_at = ago(5000)
        movie.labels = ["favorite"]
        assert verdict(movie, self.KEEPERS) == "protected"


class TestUsers:
    def test_inactive_user_is_a_candidate(self, stale_user):
        assert user_verdict(stale_user) == "candidate"
        assert "Inactive for 400d" in stale_user.reasons[0]

    def test_recent_playback_keeps_user(self, stale_user):
        stale_user.last_seen_at = ago(10)
        assert user_verdict(stale_user) == "keep"

    def test_recent_seerr_login_keeps_user(self, stale_user):
        """Someone who requests but streams elsewhere is still active."""
        stale_user.last_login_at = ago(5)
        assert user_verdict(stale_user) == "keep"

    def test_admin_protected(self, stale_user):
        stale_user.is_admin = True
        assert user_verdict(stale_user) == "protected"

    def test_home_user_protected(self, stale_user):
        stale_user.is_home = True
        assert user_verdict(stale_user) == "protected"

    def test_explicit_protect_list_by_email(self, stale_user):
        rules = UserRules(protect_users=["LAPSED@example.com"])
        assert user_verdict(stale_user, rules) == "protected"

    def test_never_active_past_grace(self, stale_user):
        stale_user.last_seen_at = None
        stale_user.first_seen_at = ago(200)
        assert user_verdict(stale_user) == "candidate"

    def test_never_active_inside_grace(self, stale_user):
        stale_user.last_seen_at = None
        stale_user.first_seen_at = ago(10)
        assert user_verdict(stale_user) == "keep"

    def test_no_activity_and_no_creation_date_is_kept(self, stale_user):
        stale_user.last_seen_at = None
        stale_user.first_seen_at = None
        assert user_verdict(stale_user) == "keep"
