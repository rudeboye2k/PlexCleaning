"""Configuration loading: YAML + ${ENV} interpolation, validated into dataclasses."""
from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    pass


def _load_dotenv(path: Path) -> None:
    """Populate os.environ from a .env file without clobbering real env vars."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _interpolate(node: Any) -> Any:
    """Replace ${VAR} with the environment value, recursively."""
    if isinstance(node, str):
        def sub(match: re.Match[str]) -> str:
            return os.environ.get(match.group(1), "")
        return ENV_PATTERN.sub(sub, node)
    if isinstance(node, dict):
        return {k: _interpolate(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_interpolate(v) for v in node]
    return node


def _build(cls: type, data: Any):
    """Instantiate a flat dataclass from a dict, ignoring unknown keys.

    Nested sections are assembled explicitly in load_config rather than here,
    because `from __future__ import annotations` turns field types into strings.
    """
    if not isinstance(data, dict):
        return cls()
    known = {f.name for f in fields(cls)}
    kwargs = {k: v for k, v in data.items() if k in known and v is not None}
    return cls(**kwargs)


@dataclass
class AppConfig:
    host: str = "127.0.0.1"
    port: int = 8585
    allowed_networks: list[str] = field(default_factory=list)
    secret_key: str = ""
    password: str = ""
    session_hours: int = 12
    data_dir: str = "./data"
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
    protect_plex_labels: list[str] = field(default_factory=list)
    protect_collections: list[str] = field(default_factory=list)
    protect_arr_tags: list[str] = field(default_factory=list)
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
    source_path: str = ""

    @property
    def data_path(self) -> Path:
        p = Path(self.app.data_dir).expanduser()
        if not p.is_absolute() and self.source_path:
            p = (Path(self.source_path).parent.parent / p).resolve()
        return p

    @property
    def db_path(self) -> Path:
        return self.data_path / "plexcleaner.db"

    @property
    def backup_path(self) -> Path:
        return self.data_path / "backups"

    def allowed_cidrs(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        nets: list[Any] = [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")]
        for cidr in self.app.allowed_networks:
            try:
                nets.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                raise ConfigError(f"app.allowed_networks: {cidr!r} is not a valid CIDR")
        try:
            host_ip = ipaddress.ip_address(self.app.host)
            nets.append(ipaddress.ip_network(f"{host_ip}/32" if host_ip.version == 4 else f"{host_ip}/128"))
        except ValueError:
            pass
        return nets

    def enabled_arrs(self) -> list[ArrConfig]:
        return [a for a in (self.sonarr + self.radarr) if a.enabled]

    def validate(self) -> list[str]:
        """Return a list of human-readable problems. Empty means good to go."""
        problems: list[str] = []
        if self.plex.enabled and not (self.plex.url and self.plex.token):
            problems.append("plex is enabled but url or token is missing")
        if self.tautulli.enabled and not (self.tautulli.url and self.tautulli.api_key):
            problems.append("tautulli is enabled but url or api_key is missing")
        if self.seerr.enabled and not (self.seerr.url and self.seerr.api_key):
            problems.append("seerr is enabled but url or api_key is missing")
        for arr in self.sonarr + self.radarr:
            if arr.enabled and not (arr.url and arr.api_key):
                problems.append(f"{arr.name} is enabled but url or api_key is missing")
        names = [a.name for a in self.sonarr + self.radarr]
        if len(names) != len(set(names)):
            problems.append("sonarr/radarr instance names must be unique")
        if self.app.password and not self.app.secret_key:
            problems.append("app.password is set but app.secret_key is empty — sessions cannot be signed")
        if self.rules.users.plex_action not in {"unshare", "remove_friend", "none"}:
            problems.append("rules.users.plex_action must be one of: unshare, remove_friend, none")
        if not self.plex.enabled and not self.tautulli.enabled:
            problems.append("at least one of plex or tautulli must be enabled to determine watch state")
        return problems


def load_config(path: str | Path | None = None) -> Config:
    candidates = [Path(path)] if path else [
        Path(os.environ.get("PLEXCLEANER_CONFIG", "")),
        Path("config/config.yaml"),
        Path("/config/config.yaml"),
        Path.home() / ".config" / "plexcleaner" / "config.yaml",
    ]
    chosen = next((c for c in candidates if c and str(c) and c.is_file()), None)
    if chosen is None:
        raise ConfigError(
            "No config file found. Copy config/config.example.yaml to config/config.yaml, "
            "or set PLEXCLEANER_CONFIG to its path."
        )

    _load_dotenv(chosen.parent / ".env")
    _load_dotenv(chosen.parent.parent / ".env")

    raw = yaml.safe_load(chosen.read_text(encoding="utf-8")) or {}
    raw = _interpolate(raw)

    cfg = Config(
        app=_build(AppConfig, raw.get("app", {})),
        safety=_build(SafetyConfig, raw.get("safety", {})),
        plex=_build(PlexConfig, raw.get("plex", {})),
        tautulli=_build(TautulliConfig, raw.get("tautulli", {})),
        sonarr=[_build(ArrConfig, i) for i in (raw.get("sonarr") or [])],
        radarr=[_build(ArrConfig, i) for i in (raw.get("radarr") or [])],
        seerr=_build(SeerrConfig, raw.get("seerr", {})),
        rules=RulesConfig(
            media=_build(MediaRules, (raw.get("rules") or {}).get("media", {})),
            users=_build(UserRules, (raw.get("rules") or {}).get("users", {})),
        ),
        schedule=_build(ScheduleConfig, raw.get("schedule", {})),
        source_path=str(chosen.resolve()),
    )
    # Radarr instances default to a distinct name so a single-instance config
    # that omits `name` on both does not collide.
    for i, arr in enumerate(cfg.radarr):
        if arr.name == "arr":
            arr.name = f"radarr-{i + 1}" if i else "radarr"
    for i, arr in enumerate(cfg.sonarr):
        if arr.name == "arr":
            arr.name = f"sonarr-{i + 1}" if i else "sonarr"
    return cfg
