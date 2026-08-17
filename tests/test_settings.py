"""The settings store: layering, persistence, live reload and the safety lock."""
from __future__ import annotations

import pytest
import yaml

from plexcleaner.config import REDACTED, ConfigError
from plexcleaner.db import Database
from plexcleaner.settings_store import SettingsStore, env_overlay


def make_store(tmp_path, environ=None, yaml_text=None) -> SettingsStore:
    yaml_path = None
    if yaml_text is not None:
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(yaml_text, encoding="utf-8")
    db = Database(tmp_path / "s.db")
    return SettingsStore(db, yaml_path=yaml_path, environ=environ or {})


class TestEnvOverlay:
    def test_service_is_enabled_once_url_and_key_are_present(self):
        overlay = env_overlay({"PLEX_URL": "http://p:32400", "PLEX_TOKEN": "abc"})
        assert overlay["plex"] == {"url": "http://p:32400", "token": "abc", "enabled": True}

    def test_url_without_a_key_does_not_enable(self):
        overlay = env_overlay({"PLEX_URL": "http://p:32400"})
        assert "enabled" not in overlay["plex"]

    def test_booleans_accept_common_spellings(self):
        assert env_overlay({"PLEXCLEANER_DRY_RUN": "false"})["safety"]["dry_run"] is False
        assert env_overlay({"PLEXCLEANER_DRY_RUN": "yes"})["safety"]["dry_run"] is True
        assert env_overlay({"PLEXCLEANER_TRUST_PROXY": "1"})["app"]["trust_proxy"] is True

    def test_networks_parse_as_a_comma_separated_list(self):
        overlay = env_overlay({"PLEXCLEANER_ALLOWED_NETWORKS": "10.0.0.0/8, 192.168.1.0/24"})
        assert overlay["app"]["allowed_networks"] == ["10.0.0.0/8", "192.168.1.0/24"]

    def test_a_junk_number_is_ignored_rather_than_crashing(self):
        assert "app" not in env_overlay({"PLEXCLEANER_PORT": "not-a-port"})

    def test_first_arr_instance_from_plain_variables(self):
        overlay = env_overlay({"SONARR_URL": "http://s:8989", "SONARR_API_KEY": "k"})
        assert overlay["sonarr"] == [{"name": "sonarr", "enabled": True,
                                     "url": "http://s:8989", "api_key": "k"}]

    def test_second_arr_instance_is_numbered(self):
        overlay = env_overlay({
            "SONARR_URL": "http://s:8989", "SONARR_API_KEY": "k",
            "SONARR_2_URL": "http://s4k:8989", "SONARR_2_API_KEY": "k2",
            "SONARR_2_NAME": "sonarr-4k",
        })
        assert [i["name"] for i in overlay["sonarr"]] == ["sonarr", "sonarr-4k"]

    def test_empty_values_are_skipped(self):
        """Container UIs love to leave blank variables lying around."""
        assert env_overlay({"PLEX_URL": "", "PLEX_TOKEN": ""}) == {}


class TestLayering:
    def test_defaults_apply_with_nothing_configured(self, tmp_path):
        cfg = make_store(tmp_path).current()
        assert cfg.safety.dry_run is True
        assert cfg.plex.enabled is False
        assert "192.168.0.0/16" in cfg.app.allowed_networks

    def test_env_beats_defaults(self, tmp_path):
        cfg = make_store(tmp_path, {"PLEXCLEANER_UNWATCHED_DAYS": "730"}).current()
        assert cfg.rules.media.unwatched_days == 730

    def test_env_beats_the_yaml_file(self, tmp_path):
        store = make_store(
            tmp_path,
            {"PLEXCLEANER_UNWATCHED_DAYS": "900"},
            yaml_text=yaml.safe_dump({"rules": {"media": {"unwatched_days": 500}}}),
        )
        assert store.current().rules.media.unwatched_days == 900

    def test_yaml_beats_defaults(self, tmp_path):
        store = make_store(tmp_path, yaml_text=yaml.safe_dump(
            {"rules": {"media": {"unwatched_days": 500}}}))
        assert store.current().rules.media.unwatched_days == 500

    def test_saved_settings_beat_the_environment(self, tmp_path):
        """What you save in the web UI is what applies — that is the whole point."""
        store = make_store(tmp_path, {"PLEXCLEANER_UNWATCHED_DAYS": "900"})
        store.save({"rules": {"media": {"unwatched_days": 100}}})
        assert store.current().rules.media.unwatched_days == 100

    def test_shadowed_env_is_reported(self, tmp_path):
        store = make_store(tmp_path, {"PLEXCLEANER_UNWATCHED_DAYS": "900"})
        assert store.shadowed_env() == []
        store.save({"rules": {"media": {"unwatched_days": 100}}})
        assert "rules.media.unwatched_days" in store.shadowed_env()

    def test_yaml_interpolates_environment_variables(self, tmp_path):
        store = make_store(
            tmp_path,
            yaml_text="plex:\n  enabled: true\n  url: http://p:32400\n  token: ${MY_TOKEN}\n",
        )
        import os
        os.environ["MY_TOKEN"] = "from-env"
        try:
            assert store.reload().plex.token == "from-env"
        finally:
            del os.environ["MY_TOKEN"]

    def test_provenance_labels_each_source(self, tmp_path):
        store = make_store(
            tmp_path,
            {"PLEXCLEANER_INACTIVE_DAYS": "400"},
            yaml_text=yaml.safe_dump({"rules": {"media": {"min_age_days": 45}}}),
        )
        store.save({"safety": {"confirm_phrase": "ERASE"}})
        prov = store.provenance()
        assert prov["rules.users.inactive_days"] == "env"
        assert prov["rules.media.min_age_days"] == "file"
        assert prov["safety.confirm_phrase"] == "saved"
        assert prov["rules.media.unwatched_days"] == "default"


class TestConfiguredFlag:
    def test_a_fresh_install_is_not_configured(self, tmp_path):
        assert make_store(tmp_path).current().configured is False

    def test_env_supplied_service_counts_as_configured(self, tmp_path):
        """A container given credentials up front should skip the wizard."""
        store = make_store(tmp_path, {"PLEX_URL": "http://p:32400", "PLEX_TOKEN": "t"})
        assert store.current().configured is True

    def test_saving_anything_marks_it_configured(self, tmp_path):
        store = make_store(tmp_path)
        store.save({"plex": {"enabled": True, "url": "http://p:32400", "token": "t"}})
        assert store.current().configured is True


class TestSaving:
    def test_save_persists_across_new_store_instances(self, tmp_path):
        store = make_store(tmp_path)
        store.save({"plex": {"enabled": True, "url": "http://p:32400", "token": "t"}})
        reopened = SettingsStore(store.db, yaml_path=None, environ={})
        assert reopened.current().plex.url == "http://p:32400"

    def test_save_bumps_the_revision(self, tmp_path):
        store = make_store(tmp_path)
        before = store.current().revision
        store.save({"safety": {"confirm_phrase": "ERASE"}})
        assert store.current().revision == before + 1

    def test_invalid_settings_are_refused(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(ConfigError, match="no API key"):
            store.save({"plex": {"enabled": True, "url": "http://p:32400", "token": ""}})

    def test_a_refused_save_changes_nothing(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(ConfigError):
            store.save({"app": {"allowed_networks": ["not-a-cidr"]}})
        assert store.current().app.allowed_networks != ["not-a-cidr"]

    def test_bad_url_scheme_is_refused(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(ConfigError, match="http://"):
            store.save({"plex": {"enabled": True, "url": "10.0.0.1:32400", "token": "t"}})

    def test_secrets_survive_a_round_trip_through_the_form(self, tmp_path):
        """The browser never sees real keys, so saving must not wipe them."""
        store = make_store(tmp_path)
        store.save({"plex": {"enabled": True, "url": "http://p:32400", "token": "real-token"}})
        redacted = store.current().to_dict(redact=True)
        assert redacted["plex"]["token"] == REDACTED

        store.save(redacted)
        assert store.current().plex.token == "real-token"

    def test_data_dir_cannot_be_changed_from_the_ui(self, tmp_path):
        """The container mount owns where data lives."""
        store = make_store(tmp_path)
        original = store.current().app.data_dir
        store.save({"app": {"data_dir": "/somewhere/else"}})
        assert store.current().app.data_dir == original

    def test_arr_names_are_made_unique(self, tmp_path):
        store = make_store(tmp_path)
        store.save({
            "plex": {"enabled": True, "url": "http://p:32400", "token": "t"},
            "sonarr": [
                {"name": "sonarr", "enabled": True, "url": "http://a:8989", "api_key": "k"},
                {"name": "sonarr", "enabled": True, "url": "http://b:8989", "api_key": "k"},
            ],
        })
        assert [a.name for a in store.current().sonarr] == ["sonarr", "sonarr-2"]

    def test_reset_falls_back_to_env(self, tmp_path):
        store = make_store(tmp_path, {"PLEXCLEANER_UNWATCHED_DAYS": "900"})
        store.save({"rules": {"media": {"unwatched_days": 100}}})
        assert store.current().rules.media.unwatched_days == 100
        store.reset()
        assert store.current().rules.media.unwatched_days == 900

    def test_saves_are_audited_without_leaking_secrets(self, tmp_path):
        store = make_store(tmp_path)
        store.save({"plex": {"enabled": True, "url": "http://p:32400", "token": "hunter2"}})
        rows = store.db.query("SELECT * FROM audit_log WHERE service = 'settings'")
        assert len(rows) == 1
        assert "hunter2" not in rows[0]["detail"]
        assert "plex.token (secret)" in rows[0]["detail"]


class TestSafetyLock:
    def test_lock_prevents_ui_from_leaving_dry_run(self, tmp_path):
        store = make_store(tmp_path, {"PLEXCLEANER_LOCK_SAFETY": "true"})
        store.save({"safety": {"dry_run": False}})
        assert store.current().safety.dry_run is True
        assert store.current().safety_locked is True

    def test_lock_still_honours_the_environment(self, tmp_path):
        store = make_store(tmp_path, {"PLEXCLEANER_LOCK_SAFETY": "true",
                                      "PLEXCLEANER_DRY_RUN": "false"})
        assert store.current().safety.dry_run is False

    def test_without_the_lock_the_ui_can_go_live(self, tmp_path):
        store = make_store(tmp_path)
        store.save({"safety": {"dry_run": False}})
        assert store.current().safety.dry_run is False

    def test_lock_does_not_block_other_sections(self, tmp_path):
        store = make_store(tmp_path, {"PLEXCLEANER_LOCK_SAFETY": "true"})
        store.save({"rules": {"media": {"unwatched_days": 42}}})
        assert store.current().rules.media.unwatched_days == 42


class TestSecretKey:
    def test_a_key_is_generated_and_persisted(self, tmp_path):
        store = make_store(tmp_path)
        key = store.current().app.secret_key
        assert len(key) == 64
        assert SettingsStore(store.db, yaml_path=None, environ={}).current().app.secret_key == key

    def test_env_supplied_key_wins(self, tmp_path):
        store = make_store(tmp_path, {"PLEXCLEANER_SECRET_KEY": "x" * 40})
        assert store.current().app.secret_key == "x" * 40


class TestYamlImportExport:
    def test_export_redacts_secrets_by_default(self, tmp_path):
        store = make_store(tmp_path)
        # A value that cannot collide with a field name like "secret_key".
        store.save({"plex": {"enabled": True, "url": "http://p:32400", "token": "tok-xyzzy-42"}})
        assert "tok-xyzzy-42" not in store.current().to_yaml(redact=True)
        assert "tok-xyzzy-42" in store.current().to_yaml(redact=False)

    def test_import_replaces_saved_settings(self, tmp_path):
        store = make_store(tmp_path)
        store.save({"safety": {"confirm_phrase": "FIRST"},
                    "plex": {"enabled": True, "url": "http://p:32400", "token": "t"}})
        store.import_yaml(yaml.safe_dump({
            "safety": {"confirm_phrase": "SECOND"},
            "plex": {"enabled": True, "url": "http://q:32400", "token": "t2"},
        }))
        cfg = store.current()
        assert cfg.safety.confirm_phrase == "SECOND"
        assert cfg.plex.url == "http://q:32400"

    def test_import_keeps_redacted_secrets(self, tmp_path):
        """Exporting then re-importing must not blank out every key."""
        store = make_store(tmp_path)
        store.save({"plex": {"enabled": True, "url": "http://p:32400", "token": "keepme"}})
        store.import_yaml(store.current().to_yaml(redact=True))
        assert store.current().plex.token == "keepme"

    def test_invalid_yaml_is_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="not valid YAML"):
            make_store(tmp_path).import_yaml("{{{ not yaml")

    def test_non_mapping_yaml_is_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="mapping"):
            make_store(tmp_path).import_yaml("- just\n- a\n- list\n")


class TestWarnings:
    def test_live_mode_warns(self, tmp_path):
        store = make_store(tmp_path)
        store.save({"safety": {"dry_run": False}})
        assert any("Live mode" in w for w in store.current().warnings())

    def test_missing_password_warns(self, tmp_path):
        assert any("password" in w for w in make_store(tmp_path).current().warnings())

    def test_trust_proxy_warns(self, tmp_path):
        store = make_store(tmp_path, {"PLEXCLEANER_TRUST_PROXY": "true"})
        assert any("Proxy headers" in w for w in store.current().warnings())

    def test_wide_open_networks_warn(self, tmp_path):
        store = make_store(tmp_path, {"PLEXCLEANER_ALLOWED_NETWORKS": "0.0.0.0/0"})
        assert any("0.0.0.0/0" in w for w in store.current().warnings())
