"""Tests for plan building and execution — the code paths that delete things."""
from __future__ import annotations

import json

import pytest

from plexcleaner.actions import (Executor, PlanError, build_media_steps, build_plan,
                                 build_user_steps)
from plexcleaner.models import Step


class FakeArr:
    def __init__(self, name):
        self.name = name
        self.calls = []

    def delete_item(self, item_id, *, delete_files, add_exclusion):
        self.calls.append((item_id, delete_files, add_exclusion))
        return {"deleted": item_id}

    def close(self):
        pass


class FakePlex:
    def __init__(self):
        self.calls = []

    def delete_item(self, key):
        self.calls.append(("delete_item", key))
        return {"ok": True}

    def refresh_section(self, key):
        self.calls.append(("refresh_section", key))
        return {"ok": True}

    def unshare(self, username):
        self.calls.append(("unshare", username))
        return {"ok": True}

    def remove_friend(self, username):
        self.calls.append(("remove_friend", username))
        return {"ok": True}

    def close(self):
        pass


class FakeSeerr:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def delete_media(self, media_id):
        self.calls.append(("delete_media", media_id))
        if self.fail:
            raise ValueError("seerr exploded")
        return {"ok": True}

    def delete_media_file(self, media_id):
        self.calls.append(("delete_media_file", media_id))
        return {"ok": True}

    def delete_user(self, user_id):
        self.calls.append(("delete_user", user_id))
        return {"ok": True}

    def close(self):
        pass


class FakeServices:
    def __init__(self, cfg, *, seerr_fails=False):
        self.cfg = cfg
        self.plex = FakePlex()
        self.seerr = FakeSeerr(fail=seerr_fails)
        self.tautulli = None
        self.sonarr = [FakeArr("sonarr")]
        self.radarr = [FakeArr("radarr")]

    def arr_client(self, name):
        return next((c for c in self.sonarr + self.radarr if c.name == name), None)

    def close(self):
        pass


class TestStepBuilding:
    def test_movie_deletes_via_radarr_then_seerr_then_refreshes_plex(self, movie, cfg):
        steps = build_media_steps(movie, cfg)
        assert [(s.service, s.action) for s in steps] == [
            ("radarr", "delete_movie"),
            ("seerr", "delete_media"),
            ("plex", "refresh_section"),
        ]

    def test_show_deletes_via_sonarr(self, show, cfg):
        steps = build_media_steps(show, cfg)
        assert steps[0].service == "sonarr"
        assert steps[0].action == "delete_series"
        assert steps[0].params["delete_files"] is True
        assert steps[0].params["add_exclusion"] is True

    def test_plex_deletes_directly_when_no_arr_owns_the_item(self, movie, cfg):
        """Without an *arr match nothing else removes the files, so Plex must."""
        movie.arr_instance = None
        movie.arr_id = None
        steps = build_media_steps(movie, cfg)
        assert ("plex", "delete_item") in [(s.service, s.action) for s in steps]
        assert ("plex", "refresh_section") not in [(s.service, s.action) for s in steps]

    def test_plex_only_refreshes_when_arr_already_deleted_files(self, movie, cfg):
        steps = build_media_steps(movie, cfg)
        plex_steps = [s for s in steps if s.service == "plex"]
        assert [s.action for s in plex_steps] == ["refresh_section"]

    def test_arr_keeping_files_means_plex_deletes_them(self, movie, cfg):
        cfg.radarr[0].delete_files = False
        steps = build_media_steps(movie, cfg)
        assert ("plex", "delete_item") in [(s.service, s.action) for s in steps]

    def test_seerr_file_deletion_skipped_when_arr_handles_it(self, movie, cfg):
        cfg.seerr.delete_file_via_seerr = True
        steps = build_media_steps(movie, cfg)
        assert "delete_media_file" not in [s.action for s in steps]

    def test_seerr_file_deletion_used_when_no_arr_match(self, movie, cfg):
        cfg.seerr.delete_file_via_seerr = True
        movie.arr_instance = None
        movie.arr_id = None
        steps = build_media_steps(movie, cfg)
        assert "delete_media_file" in [s.action for s in steps]

    def test_disabled_services_produce_no_steps(self, movie, cfg):
        cfg.seerr.enabled = False
        cfg.plex.enabled = False
        cfg.radarr[0].enabled = False
        assert build_media_steps(movie, cfg) == []

    def test_user_steps_default_to_unshare(self, stale_user, cfg):
        steps = build_user_steps(stale_user, cfg)
        assert [(s.service, s.action) for s in steps] == [
            ("seerr", "delete_user"),
            ("plex", "unshare"),
        ]

    def test_user_remove_friend_mode(self, stale_user, cfg):
        cfg.rules.users.plex_action = "remove_friend"
        steps = build_user_steps(stale_user, cfg)
        assert ("plex", "remove_friend") in [(s.service, s.action) for s in steps]

    def test_user_plex_action_none(self, stale_user, cfg):
        cfg.rules.users.plex_action = "none"
        steps = build_user_steps(stale_user, cfg)
        assert all(s.service != "plex" for s in steps)

    def test_unshare_skipped_when_user_has_no_share(self, stale_user, cfg):
        stale_user.has_plex_share = False
        steps = build_user_steps(stale_user, cfg)
        assert all(s.service != "plex" for s in steps)


def _seed(db, cfg, item, verdict="candidate"):
    scan_id = db.insert("scan", {"started_at": "2026-08-17T00:00:00+00:00", "status": "complete"})
    item.verdict = verdict
    table = "media_item" if hasattr(item, "kind") else "user_account"
    row_id = db.insert(table, item.to_row(scan_id))
    return scan_id, row_id


class TestPlanBuilding:
    def test_builds_a_plan_from_a_candidate(self, db, cfg, movie):
        scan_id, row_id = _seed(db, cfg, movie)
        plan_id = build_plan(db, cfg, scan_id=scan_id, media_ids=[row_id], user_ids=[])
        items = db.query("SELECT * FROM plan_item WHERE plan_id = ?", (plan_id,))
        assert len(items) == 1
        assert json.loads(items[0]["steps"])[0]["service"] == "radarr"

    def test_refuses_protected_items(self, db, cfg, movie):
        scan_id, row_id = _seed(db, cfg, movie, verdict="protected")
        with pytest.raises(PlanError):
            build_plan(db, cfg, scan_id=scan_id, media_ids=[row_id], user_ids=[])

    def test_refuses_items_on_the_permanent_keep_list(self, db, cfg, movie):
        scan_id, row_id = _seed(db, cfg, movie)
        db.add_protection("media", movie.ref)
        with pytest.raises(PlanError):
            build_plan(db, cfg, scan_id=scan_id, media_ids=[row_id], user_ids=[])

    def test_refuses_an_empty_selection(self, db, cfg):
        with pytest.raises(PlanError):
            build_plan(db, cfg, scan_id=1, media_ids=[], user_ids=[])

    def test_snapshot_is_stored_with_the_plan(self, db, cfg, movie):
        scan_id, row_id = _seed(db, cfg, movie)
        plan_id = build_plan(db, cfg, scan_id=scan_id, media_ids=[row_id], user_ids=[])
        snapshot = json.loads(db.query("SELECT snapshot FROM plan_item WHERE plan_id = ?",
                                       (plan_id,))[0]["snapshot"])
        assert snapshot["title"] == "Old Movie"
        assert snapshot["tmdb_id"] == "500"


class TestExecution:
    def _plan(self, db, cfg, movie):
        scan_id, row_id = _seed(db, cfg, movie)
        return build_plan(db, cfg, scan_id=scan_id, media_ids=[row_id], user_ids=[])

    def test_dry_run_calls_nothing(self, db, cfg, movie):
        plan_id = self._plan(db, cfg, movie)
        services = FakeServices(cfg)
        results = Executor(cfg, db, services).execute(plan_id)
        assert results["dry_run"] is True
        assert results["done"] == 1
        assert services.radarr[0].calls == []
        assert services.plex.calls == []

    def test_dry_run_is_still_audited(self, db, cfg, movie):
        plan_id = self._plan(db, cfg, movie)
        Executor(cfg, db, FakeServices(cfg)).execute(plan_id)
        rows = db.query("SELECT * FROM audit_log WHERE action = 'delete_movie'")
        assert len(rows) == 1
        assert rows[0]["dry_run"] == 1

    def test_live_run_requires_the_confirm_phrase(self, db, cfg, movie):
        cfg.safety.dry_run = False
        plan_id = self._plan(db, cfg, movie)
        with pytest.raises(PlanError, match="Confirmation phrase"):
            Executor(cfg, db, FakeServices(cfg)).execute(plan_id, confirm="nope", force_live=True)

    def test_global_dry_run_cannot_be_overridden_by_the_request(self, db, cfg, movie):
        """safety.dry_run is the master switch — force_live must not defeat it."""
        cfg.safety.dry_run = True
        plan_id = self._plan(db, cfg, movie)
        services = FakeServices(cfg)
        results = Executor(cfg, db, services).execute(plan_id, confirm="DELETE", force_live=True)
        assert results["dry_run"] is True
        assert services.radarr[0].calls == []

    def test_live_run_calls_services_in_order(self, db, cfg, movie):
        cfg.safety.dry_run = False
        plan_id = self._plan(db, cfg, movie)
        services = FakeServices(cfg)
        results = Executor(cfg, db, services).execute(plan_id, confirm="DELETE", force_live=True)
        assert results["done"] == 1
        assert services.radarr[0].calls == [(42, True, True)]
        assert services.seerr.calls == [("delete_media", 7)]
        assert services.plex.calls == [("refresh_section", "1")]

    def test_failed_step_stops_the_rest_of_that_item(self, db, cfg, movie):
        """If Seerr fails, Plex must not be refreshed — the state is unknown."""
        cfg.safety.dry_run = False
        plan_id = self._plan(db, cfg, movie)
        services = FakeServices(cfg, seerr_fails=True)
        results = Executor(cfg, db, services).execute(plan_id, confirm="DELETE", force_live=True)
        assert results["failed"] == 1
        assert services.plex.calls == []

    def test_media_cap_is_enforced(self, db, cfg, movie):
        cfg.safety.max_media_deletions_per_run = 0
        plan_id = self._plan(db, cfg, movie)
        with pytest.raises(PlanError, match="exceeds the cap"):
            Executor(cfg, db, FakeServices(cfg)).execute(plan_id)

    def test_gigabyte_cap_is_enforced(self, db, cfg, movie):
        cfg.safety.max_gigabytes_per_run = 1
        plan_id = self._plan(db, cfg, movie)  # fixture is 8 GB
        with pytest.raises(PlanError, match="GB exceeds the cap"):
            Executor(cfg, db, FakeServices(cfg)).execute(plan_id)

    def test_a_plan_cannot_be_executed_twice(self, db, cfg, movie):
        plan_id = self._plan(db, cfg, movie)
        executor = Executor(cfg, db, FakeServices(cfg))
        executor.execute(plan_id)
        with pytest.raises(PlanError, match="not a draft"):
            executor.execute(plan_id)

    def test_unknown_step_is_reported_not_silently_skipped(self, db, cfg):
        executor = Executor(cfg, db, FakeServices(cfg))
        outcome = executor._run_step(
            Step(service="nope", action="destroy", target="1"),
            dry_run=False, plan_id=1, actor="test",
        )
        assert outcome["ok"] is False
