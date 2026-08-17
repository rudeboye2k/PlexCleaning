"""Configuration dataclasses plus dict/YAML conversion.

The runtime *source* of configuration is `settings_store.SettingsStore`, which
layers built-in defaults, an optional YAML file, environment variables and
web-UI edits stored in SQLite. This module only defines the shape and knows how
to turn a nested dict into typed objects and back again.
"""
from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Sensible for a NAS or home server: reachable from the LAN, never from a WAN
# interface. Narrow this on the Settings page if you want tighter control.
DEFAULT_ALLOWED_NETWORKS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"]

PLEX_ACTIONS = ("unshare", "remove_friend", "none")


class ConfigError(Exception):
    pass


def load_dotenv(path: Path) -> None:
    """Populate os.environ from a .env file without clobbering real env vars."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def interpolate(node: Any) -> Any:
    """Replace ${VAR} with the environment value, recursively."""
    if isinstance(node, str):
        return ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), node)
    if isinstance(node, dict):
        return {k: interpolate(v) for k, v in node.items()}
    if isinstance(node, list):
        return [interpolate(v) for v in node]
    return node


def _build(cls: type, data: Any):
    """Instantiate a flat dataclass from a dict, ignoring unknown keys."""
    if not isinstance(data, dict):
        return cls()
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known and v is not None})


@dataclass
class AppConfig:
    host: str = "0.0.0.0"
    port: int = 8585
    allowed_networks: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_NETWORKS))
    # Set when running behind Synology's reverse proxy, Nginx Proxy Manager,
    # Traefik and friends: without it the network guard only ever sees the
    # proxy's own address.
    trust_proxy: bool = False
    secret_key: str = ""
    password: str = ""
    session_hours: int = 12
    data_dir: str = "/data"
    log_level: str = "INFO"


@dataclass
class SafetyConfig:
    dry_run: bool = True
    confirm_phrase: str = "DELETE"
    max_media_deletions_per_run: int = 25
    max_user_removals_per_run: int = 5
    max_gigabytes_per_run: int = 2000
    snapshot_before_delete: bool = True
    abort_after_failures: int = 5


@dataclass
class PlexConfig:
    enabled: bool = False
    url: str = ""
    token: str = ""
    verify_ssl: bool = False
    timeout: int = 60
    libraries: list[str] = field(default_factory=list)
    exclude_libraries: list[str] = field(default_factory=list)


@dataclass
class TautulliConfig:
    enabled: bool = False
    url: str = ""
    api_key: str = ""
    verify_ssl: bool = False
    timeout: int = 60
    purge_history_on_delete: bool = False


@dataclass
class ArrConfig:
    name: str = "arr"
    enabled: bool = False
    url: str = ""
    api_key: str = ""
    verify_ssl: bool = False
    timeout: int = 60
    delete_files: bool = True
    add_import_exclusion: bool = True


@dataclass
class SeerrConfig:
    enabled: bool = False
    url: str = ""
    api_key: str = ""
    verify_ssl: bool = False
    timeout: int = 60
    remove_media_entry: bool = True
    delete_file_via_seerr: bool = False


@dataclass
class MediaRules:
    unwatched_days: int = 365
    never_watched_after_days: int = 180
    min_age_days: int = 30
    protect_continuing_series: bool = True
    protect_plex_labels: list[str] = field(default_factory=lambda: ["keep", "favorite"])
    protect_collections: list[str] = field(default_factory=list)
    protect_arr_tags: list[str] = field(default_factory=lambda: ["keep"])
    protect_in_progress: bool = True
    protect_recent_seerr_requests_days: int = 90
    include_libraries: list[str] = field(default_factory=list)
    min_size_mb: int = 0


@dataclass
class UserRules:
    inactive_days: int = 180
    never_active_after_days: int = 60
    protect_users: list[str] = field(default_factory=list)
    protect_admins: bool = True
    protect_home_users: bool = True
    plex_action: str = "unshare"
    remove_from_seerr: bool = True
    remove_from_tautulli: bool = False


@dataclass
class RulesConfig:
    media: MediaRules = field(default_factory=MediaRules)
    users: UserRules = field(default_factory=UserRules)


@dataclass
class ScheduleConfig:
    scan_enabled: bool = True
    scan_cron_hour: int = 4
    scan_cron_minute: int = 30
    retention_days: int = 365


@dataclass
class Config:
    app: AppConfig = field(default_factory=AppConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    plex: PlexConfig = field(default_factory=PlexConfig)
    tautulli: TautulliConfig = field(default_factory=TautulliConfig)
    sonarr: list[ArrConfig] = field(default_factory=list)
    radarr: list[ArrConfig] = field(default_factory=list)
    seerr: SeerrConfig = field(default_factory=SeerrConfig)
    rules: RulesConfig = field(default_factory=RulesConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)

    # Runtime metadata, not user-editable settings.
    source_path: str = ""
    revision: int = 0
    configured: bool = False
    safety_locked: bool = False

    # -- paths -----------------------------------------------------------
    @property
    def data_path(self) -> Path:
        p = Path(self.app.data_dir).expanduser()
        if not p.is_absolute():
            base = Path(self.source_path).parent.parent if self.source_path else Path.cwd()
            p = (base / p).resolve()
        return p

    @property
    def db_path(self) -> Path:
        return self.data_path / "plexcleaner.db"

    @property
    def backup_path(self) -> Path:
        return self.data_path / "backups"

    # -- helpers ---------------------------------------------------------
    def allowed_cidrs(self) -> list[Any]:
        nets: list[Any] = [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")]
        for cidr in self.app.allowed_networks:
            try:
                nets.append(ipaddress.ip_network(str(cidr).strip(), strict=False))
            except ValueError:
                continue
        try:
            host_ip = ipaddress.ip_address(self.app.host)
            nets.append(ipaddress.ip_network(
                f"{host_ip}/{'32' if host_ip.version == 4 else '128'}"))
        except ValueError:
            pass
        return nets

    def all_arrs(self) -> list[ArrConfig]:
        return list(self.sonarr) + list(self.radarr)

    def arr_by_name(self, name: str) -> ArrConfig | None:
        return next((a for a in self.all_arrs() if a.name == name), None)

    def any_service_enabled(self) -> bool:
        return bool(self.plex.enabled or self.tautulli.enabled or self.seerr.enabled
                    or any(a.enabled for a in self.all_arrs()))

    # -- serialisation ---------------------------------------------------
    def to_dict(self, *, redact: bool = False) -> dict[str, Any]:
        data = {
            "app": asdict(self.app),
            "safety": asdict(self.safety),
            "plex": asdict(self.plex),
            "tautulli": asdict(self.tautulli),
            "sonarr": [asdict(a) for a in self.sonarr],
            "radarr": [asdict(a) for a in self.radarr],
            "seerr": asdict(self.seerr),
            "rules": {"media": asdict(self.rules.media), "users": asdict(self.rules.users)},
            "schedule": asdict(self.schedule),
        }
        if redact:
            data = redact_secrets(data)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        rules = data.get("rules") or {}
        cfg = cls(
            app=_build(AppConfig, data.get("app", {})),
            safety=_build(SafetyConfig, data.get("safety", {})),
            plex=_build(PlexConfig, data.get("plex", {})),
            tautulli=_build(TautulliConfig, data.get("tautulli", {})),
            sonarr=[_build(ArrConfig, i) for i in (data.get("sonarr") or [])],
            radarr=[_build(ArrConfig, i) for i in (data.get("radarr") or [])],
            seerr=_build(SeerrConfig, data.get("seerr", {})),
            rules=RulesConfig(media=_build(MediaRules, rules.get("media", {})),
                              users=_build(UserRules, rules.get("users", {}))),
            schedule=_build(ScheduleConfig, data.get("schedule", {})),
        )
        cfg.normalise_names()
        return cfg

    def normalise_names(self) -> None:
        """Give every *arr instance a unique, non-empty name."""
        used: set[str] = set()
        for kind, instances in (("sonarr", self.sonarr), ("radarr", self.radarr)):
            for i, arr in enumerate(instances):
                name = (arr.name or "").strip()
                if not name or name == "arr":
                    name = kind if i == 0 else f"{kind}-{i + 1}"
                original = name
                suffix = 2
                while name in used:
                    name = f"{original}-{suffix}"
                    suffix += 1
                arr.name = name
                used.add(name)

    def to_yaml(self, *, redact: bool = False) -> str:
        return yaml.safe_dump(self.to_dict(redact=redact), sort_keys=False, default_flow_style=False)

    # -- validation ------------------------------------------------------
    def validate(self) -> list[str]:
        """Structural problems that make a configuration incoherent.

        Only these block a save, so the setup wizard can store one step at a
        time without being told off for not having configured Plex yet. Things
        that merely stop a scan from running live in `readiness()`.
        """
        problems: list[str] = []

        def check(svc, label: str, key_field: str) -> None:
            if not getattr(svc, "enabled", False):
                return
            if not getattr(svc, "url", ""):
                problems.append(f"{label} is enabled but has no URL")
            elif not str(svc.url).startswith(("http://", "https://")):
                problems.append(f"{label} URL must start with http:// or https://")
            if not getattr(svc, key_field, ""):
                problems.append(f"{label} is enabled but has no API key/token")

        check(self.plex, "Plex", "token")
        check(self.tautulli, "Tautulli", "api_key")
        check(self.seerr, "Seerr", "api_key")
        for arr in self.all_arrs():
            check(arr, arr.name, "api_key")

        if self.rules.users.plex_action not in PLEX_ACTIONS:
            problems.append(f"User Plex action must be one of: {', '.join(PLEX_ACTIONS)}")
        if not self.app.allowed_networks:
            problems.append("No allowed networks are set, so only localhost can reach the web UI")
        for cidr in self.app.allowed_networks:
            try:
                ipaddress.ip_network(str(cidr).strip(), strict=False)
            except ValueError:
                problems.append(f"'{cidr}' is not a valid network (expected something like 192.168.1.0/24)")
        if self.safety.max_media_deletions_per_run < 0 or self.safety.max_user_removals_per_run < 0:
            problems.append("Per-run caps cannot be negative")
        if self.rules.media.min_age_days < 0 or self.rules.media.unwatched_days < 1:
            problems.append("Media rule thresholds must be positive")
        return problems

    def readiness(self) -> list[str]:
        """Hard blockers between this configuration and a usable scan."""
        if not self.plex.enabled and not self.tautulli.enabled:
            return ["Enable Plex or Tautulli — one of them is needed to determine watch state"]
        return []

    def problems(self) -> list[str]:
        """Everything that should be fixed before scanning — for the UI and CLI."""
        return self.validate() + self.readiness()

    def can_scan(self) -> bool:
        return not self.validate() and (self.plex.enabled or self.tautulli.enabled)

    def warnings(self) -> list[str]:
        """Non-blocking things worth telling the user about."""
        out: list[str] = []
        if self.plex.enabled and not self.tautulli.enabled:
            out.append("Tautulli is not connected. Plex only reports the server owner's playback, "
                       "so titles your other users watch regularly will look unwatched. "
                       "Connecting Tautulli is strongly recommended before deleting anything.")
        if not self.safety.dry_run:
            out.append("Live mode is on — executing a plan permanently deletes media and revokes access.")
        if not self.app.password:
            out.append("No web UI password is set. Anyone on an allowed network can use this tool.")
        if self.app.trust_proxy:
            out.append("Proxy headers are trusted. Only enable this when a reverse proxy you control "
                       "sits in front, or the network guard can be spoofed with an X-Forwarded-For header.")
        if "0.0.0.0/0" in [str(n).strip() for n in self.app.allowed_networks]:
            out.append("Allowed networks includes 0.0.0.0/0, which permits every source address.")
        return out


SECRET_KEYS = {"token", "api_key", "password", "secret_key"}
REDACTED = "__unchanged__"


def redact_secrets(data: Any) -> Any:
    """Replace secret values with a sentinel so they never reach the browser."""
    if isinstance(data, dict):
        return {k: (REDACTED if k in SECRET_KEYS and v else redact_secrets(v)) for k, v in data.items()}
    if isinstance(data, list):
        return [redact_secrets(v) for v in data]
    return data


def merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge overlay into base. Lists replace wholesale rather than append."""
    out = dict(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


def restore_secrets(incoming: Any, current: Any) -> Any:
    """Put back any secret the browser sent as the redaction sentinel.

    The settings form never receives real secrets, so a save must not wipe them
    just because the field round-tripped as a placeholder.
    """
    if isinstance(incoming, dict):
        out = {}
        for key, value in incoming.items():
            base = current.get(key) if isinstance(current, dict) else None
            if key in SECRET_KEYS and value == REDACTED:
                out[key] = base or ""
            else:
                out[key] = restore_secrets(value, base)
        return out
    if isinstance(incoming, list):
        # Match list entries positionally — good enough for *arr instance lists.
        base_list = current if isinstance(current, list) else []
        return [restore_secrets(v, base_list[i] if i < len(base_list) else None)
                for i, v in enumerate(incoming)]
    return incoming


def defaults_dict() -> dict[str, Any]:
    return Config().to_dict()
