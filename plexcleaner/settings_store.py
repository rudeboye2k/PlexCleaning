"""Runtime configuration store.

Four layers, lowest precedence first:

  1. built-in defaults        sane values for a home server
  2. YAML file                optional; for people who prefer files or Git
  3. environment variables    how Container Manager / Portainer inject values
  4. saved settings (SQLite)  what you edit in the web UI — wins

The UI layer winning is deliberate: what you save is what applies. Environment
variables still matter — they seed a brand-new install so a container can come
up already configured, and the Settings page shows a badge on any field whose
environment value is being shadowed by a saved one.

Set PLEXCLEANER_LOCK_SAFETY=true to pin the safety block against UI edits, which
is how you keep dry-run enforced on a shared box.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Any

import yaml

from .config import (Config, ConfigError, DEFAULT_ALLOWED_NETWORKS, defaults_dict,
                     interpolate, load_dotenv, merge, restore_secrets)
from .db import Database

log = logging.getLogger(__name__)

SETTINGS_KEY = "config"
REVISION_KEY = "config_revision"
SECRET_KEY_KEY = "secret_key"

TRUE = {"1", "true", "yes", "on", "y"}
FALSE = {"0", "false", "no", "off", "n"}


def _bool(value: str) -> bool:
    return str(value).strip().lower() in TRUE


def _csv(value: str) -> list[str]:
    return [part.strip() for part in str(value).split(",") if part.strip()]


# env var -> (dotted config path, coercion)
ENV_MAP: dict[str, tuple[str, Any]] = {
    # web / access
    "PLEXCLEANER_HOST": ("app.host", str),
    "PLEXCLEANER_PORT": ("app.port", int),
    "PLEXCLEANER_PASSWORD": ("app.password", str),
    "PLEXCLEANER_SECRET_KEY": ("app.secret_key", str),
    "PLEXCLEANER_ALLOWED_NETWORKS": ("app.allowed_networks", _csv),
    "PLEXCLEANER_TRUST_PROXY": ("app.trust_proxy", _bool),
    "PLEXCLEANER_SESSION_HOURS": ("app.session_hours", int),
    "PLEXCLEANER_DATA_DIR": ("app.data_dir", str),
    "PLEXCLEANER_LOG_LEVEL": ("app.log_level", str),
    # safety
    "PLEXCLEANER_DRY_RUN": ("safety.dry_run", _bool),
    "PLEXCLEANER_CONFIRM_PHRASE": ("safety.confirm_phrase", str),
    "PLEXCLEANER_MAX_MEDIA_PER_RUN": ("safety.max_media_deletions_per_run", int),
    "PLEXCLEANER_MAX_USERS_PER_RUN": ("safety.max_user_removals_per_run", int),
    "PLEXCLEANER_MAX_GB_PER_RUN": ("safety.max_gigabytes_per_run", int),
    # services
    "PLEX_URL": ("plex.url", str),
    "PLEX_TOKEN": ("plex.token", str),
    "PLEX_VERIFY_SSL": ("plex.verify_ssl", _bool),
    "PLEX_EXCLUDE_LIBRARIES": ("plex.exclude_libraries", _csv),
    "TAUTULLI_URL": ("tautulli.url", str),
    "TAUTULLI_API_KEY": ("tautulli.api_key", str),
    "TAUTULLI_VERIFY_SSL": ("tautulli.verify_ssl", _bool),
    "SEERR_URL": ("seerr.url", str),
    "SEERR_API_KEY": ("seerr.api_key", str),
    "SEERR_VERIFY_SSL": ("seerr.verify_ssl", _bool),
    # rules
    "PLEXCLEANER_UNWATCHED_DAYS": ("rules.media.unwatched_days", int),
    "PLEXCLEANER_NEVER_WATCHED_DAYS": ("rules.media.never_watched_after_days", int),
    "PLEXCLEANER_MIN_AGE_DAYS": ("rules.media.min_age_days", int),
    "PLEXCLEANER_INACTIVE_DAYS": ("rules.users.inactive_days", int),
    "PLEXCLEANER_USER_PLEX_ACTION": ("rules.users.plex_action", str),
    # schedule
    "PLEXCLEANER_SCAN_ENABLED": ("schedule.scan_enabled", _bool),
    "PLEXCLEANER_SCAN_HOUR": ("schedule.scan_cron_hour", int),
    "PLEXCLEANER_SCAN_MINUTE": ("schedule.scan_cron_minute", int),
}

# Multi-instance *arr support: SONARR_URL is instance 1, SONARR_2_URL is
# instance 2, and so on up to this many.
MAX_ARR_INSTANCES = 4


def _set_path(target: dict[str, Any], dotted: str, value: Any) -> None:
    node = target
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def env_overlay(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Build a config overlay from the environment."""
    environ = environ if environ is not None else dict(os.environ)
    overlay: dict[str, Any] = {}

    for name, (path, coerce) in ENV_MAP.items():
        raw = environ.get(name)
        if raw is None or raw == "":
            continue
        try:
            _set_path(overlay, path, coerce(raw))
        except (TypeError, ValueError):
            log.warning("ignoring %s=%r: not a valid value for %s", name, raw, path)

    for kind in ("SONARR", "RADARR"):
        instances = []
        for index in range(1, MAX_ARR_INSTANCES + 1):
            prefix = kind if index == 1 else f"{kind}_{index}"
            url = environ.get(f"{prefix}_URL", "")
            key = environ.get(f"{prefix}_API_KEY", "")
            if not url and not key:
                continue
            name = environ.get(f"{prefix}_NAME", "") or (
                kind.lower() if index == 1 else f"{kind.lower()}-{index}")
            entry: dict[str, Any] = {
                "name": name,
                "enabled": bool(url and key),
                "url": url,
                "api_key": key,
            }
            for field, coerce in (("DELETE_FILES", _bool), ("ADD_IMPORT_EXCLUSION", _bool),
                                  ("VERIFY_SSL", _bool)):
                raw = environ.get(f"{prefix}_{field}")
                if raw not in (None, ""):
                    entry[field.lower()] = coerce(raw)
            instances.append(entry)
        if instances:
            overlay[kind.lower()] = instances

    # A service is enabled implicitly once it has both a URL and a credential,
    # so a container only needs the two obvious variables per service.
    for svc, key_field in (("plex", "token"), ("tautulli", "api_key"), ("seerr", "api_key")):
        node = overlay.get(svc)
        if isinstance(node, dict) and node.get("url") and node.get(key_field):
            node.setdefault("enabled", True)

    return overlay


def env_paths(environ: dict[str, str] | None = None) -> set[str]:
    """Dotted paths that the environment has an opinion about (for UI badges)."""
    environ = environ if environ is not None else dict(os.environ)
    paths = {path for name, (path, _) in ENV_MAP.items() if environ.get(name)}
    for kind in ("SONARR", "RADARR"):
        for index in range(1, MAX_ARR_INSTANCES + 1):
            prefix = kind if index == 1 else f"{kind}_{index}"
            if environ.get(f"{prefix}_URL") or environ.get(f"{prefix}_API_KEY"):
                paths.add(kind.lower())
    return paths


def find_yaml(explicit: str | Path | None = None) -> Path | None:
    candidates = [Path(explicit)] if explicit else [
        Path(os.environ.get("PLEXCLEANER_CONFIG", "")),
        Path("/config/config.yaml"),
        Path("config/config.yaml"),
        Path.home() / ".config" / "plexcleaner" / "config.yaml",
    ]
    return next((c for c in candidates if c and str(c) and c.is_file()), None)


def yaml_layer(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    return interpolate(raw)


def bootstrap_paths(explicit: str | Path | None = None) -> tuple[Path, Path | None]:
    """Resolve the data directory and YAML path before the database exists.

    The database lives inside the data directory, so this has to be worked out
    from the file and environment layers alone.
    """
    yaml_path = find_yaml(explicit)
    for folder in ({yaml_path.parent, yaml_path.parent.parent} if yaml_path else set()):
        load_dotenv(folder / ".env")
    load_dotenv(Path("/config/.env"))
    load_dotenv(Path(".env"))

    layered = merge(merge(defaults_dict(), yaml_layer(yaml_path)), env_overlay())
    cfg = Config.from_dict(layered)
    cfg.source_path = str(yaml_path) if yaml_path else ""
    return cfg.data_path, yaml_path


class SettingsStore:
    """Assembles the effective config and persists UI edits."""

    def __init__(self, db: Database, *, yaml_path: Path | None = None,
                 environ: dict[str, str] | None = None):
        self.db = db
        self.yaml_path = yaml_path
        self.environ = environ if environ is not None else dict(os.environ)
        self._lock = threading.Lock()
        self._cached: Config | None = None
        self._ensure_secret_key()

    # -- layers ----------------------------------------------------------
    def _ensure_secret_key(self) -> None:
        """Persist a generated signing key so sessions survive restarts."""
        if self.environ.get("PLEXCLEANER_SECRET_KEY"):
            return
        if not self.db.get_setting(SECRET_KEY_KEY):
            self.db.set_setting(SECRET_KEY_KEY, secrets.token_hex(32))
            log.info("generated a persistent session signing key")

    def saved(self) -> dict[str, Any]:
        return self.db.get_setting(SETTINGS_KEY, {}) or {}

    def revision(self) -> int:
        return int(self.db.get_setting(REVISION_KEY, 0) or 0)

    def is_configured(self) -> bool:
        """True once the user has saved settings, or the environment provides a service."""
        if self.saved():
            return True
        cfg = Config.from_dict(merge(merge(defaults_dict(), yaml_layer(self.yaml_path)),
                                     env_overlay(self.environ)))
        return cfg.any_service_enabled()

    def safety_locked(self) -> bool:
        return _bool(self.environ.get("PLEXCLEANER_LOCK_SAFETY", ""))

    def _assemble(self) -> Config:
        layered = defaults_dict()
        layered = merge(layered, yaml_layer(self.yaml_path))
        layered = merge(layered, env_overlay(self.environ))
        layered = merge(layered, self.saved())

        # Safety lock: the file and environment layers win over UI edits.
        if self.safety_locked():
            locked = merge(defaults_dict().get("safety", {}),
                           merge(yaml_layer(self.yaml_path).get("safety", {}) or {},
                                 env_overlay(self.environ).get("safety", {}) or {}))
            layered["safety"] = locked

        cfg = Config.from_dict(layered)
        cfg.source_path = str(self.yaml_path) if self.yaml_path else ""
        cfg.revision = self.revision()
        cfg.safety_locked = self.safety_locked()
        cfg.configured = bool(self.saved()) or cfg.any_service_enabled()

        if not cfg.app.secret_key:
            cfg.app.secret_key = str(self.db.get_setting(SECRET_KEY_KEY, "") or "")
        if not cfg.app.allowed_networks:
            cfg.app.allowed_networks = list(DEFAULT_ALLOWED_NETWORKS)
        return cfg

    # -- public API ------------------------------------------------------
    def current(self) -> Config:
        """The effective config. Cached until something saves."""
        with self._lock:
            if self._cached is None:
                self._cached = self._assemble()
            return self._cached

    def reload(self) -> Config:
        with self._lock:
            self._cached = None
        return self.current()

    def save(self, patch: dict[str, Any], *, actor: str = "web",
             merge_patch: bool = True) -> Config:
        """Validate and persist a settings patch, then reload.

        Secrets arriving as the redaction sentinel are restored from what is
        already stored, so the form can round-trip without ever holding a key.
        """
        current_full = self.current().to_dict()
        patch = restore_secrets(patch, current_full)

        if self.safety_locked():
            patch.pop("safety", None)

        # Never let the UI change where data lives — the container mount owns it.
        if isinstance(patch.get("app"), dict):
            patch["app"].pop("data_dir", None)

        candidate_saved = merge(self.saved(), patch) if merge_patch else patch
        candidate_full = merge(merge(merge(defaults_dict(), yaml_layer(self.yaml_path)),
                                     env_overlay(self.environ)), candidate_saved)
        candidate = Config.from_dict(candidate_full)

        problems = candidate.validate()
        if problems:
            raise ConfigError("; ".join(problems))

        # Persist the normalised form so *arr names stay unique and stable.
        candidate_saved = merge(candidate_saved, {
            "sonarr": [dict(a.__dict__) for a in candidate.sonarr],
            "radarr": [dict(a.__dict__) for a in candidate.radarr],
        })

        with self._lock:
            self.db.set_setting(SETTINGS_KEY, candidate_saved)
            self.db.set_setting(REVISION_KEY, self.revision() + 1)
            self._cached = None

        self.db.audit(service="settings", action="save", target=f"revision:{self.revision()}",
                      actor=actor, dry_run=True, ok=True,
                      detail={"changed": sorted(_changed_paths(current_full, candidate.to_dict()))})
        log.info("settings saved (revision %s) by %s", self.revision(), actor)
        return self.current()

    def reset(self, *, actor: str = "web") -> Config:
        """Drop UI edits, falling back to the file and environment layers."""
        with self._lock:
            self.db.set_setting(SETTINGS_KEY, {})
            self.db.set_setting(REVISION_KEY, self.revision() + 1)
            self._cached = None
        self.db.audit(service="settings", action="reset", actor=actor, dry_run=True, ok=True)
        return self.current()

    def import_yaml(self, text: str, *, actor: str = "web") -> Config:
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"not valid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError("the imported file must contain a YAML mapping at the top level")
        data.pop("source_path", None)
        return self.save(data, actor=actor, merge_patch=False)

    def provenance(self) -> dict[str, str]:
        """Where each dotted path's value comes from, for the Settings page."""
        saved = self.saved()
        yaml_data = yaml_layer(self.yaml_path)
        env_data = env_overlay(self.environ)
        out: dict[str, str] = {}
        for path in _all_paths(self.current().to_dict()):
            if _has_path(saved, path):
                out[path] = "saved"
            elif _has_path(env_data, path):
                out[path] = "env"
            elif _has_path(yaml_data, path):
                out[path] = "file"
            else:
                out[path] = "default"
        return out

    def shadowed_env(self) -> list[str]:
        """Environment-provided paths that a saved value is overriding."""
        saved, env_data = self.saved(), env_overlay(self.environ)
        return sorted(p for p in env_paths(self.environ)
                      if _has_path(env_data, p) and _has_path(saved, p))


def _all_paths(node: Any, prefix: str = "") -> list[str]:
    if isinstance(node, dict):
        out = []
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            out.extend(_all_paths(value, path) or [path])
        return out
    return [prefix] if prefix else []


def _has_path(data: Any, dotted: str) -> bool:
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _changed_paths(before: dict[str, Any], after: dict[str, Any], prefix: str = "") -> set[str]:
    """Dotted paths whose value differs, with secret values never included."""
    from .config import SECRET_KEYS

    changed: set[str] = set()
    for key in set(before) | set(after):
        path = f"{prefix}.{key}" if prefix else key
        old, new = before.get(key), after.get(key)
        if isinstance(old, dict) and isinstance(new, dict):
            changed |= _changed_paths(old, new, path)
        elif old != new:
            changed.add(path if key not in SECRET_KEYS else f"{path} (secret)")
    return changed


def build_store(config_path: str | Path | None = None) -> tuple[SettingsStore, Database, Config]:
    """Standard startup path: resolve data dir, open the DB, assemble config."""
    data_path, yaml_path = bootstrap_paths(config_path)
    data_path.mkdir(parents=True, exist_ok=True)
    db = Database(data_path / "plexcleaner.db")
    store = SettingsStore(db, yaml_path=yaml_path)
    return store, db, store.current()
