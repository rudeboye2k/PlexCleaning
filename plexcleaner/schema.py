"""Field schema for the settings portal.

One declaration drives the setup wizard, the Settings page, the help text and
the provenance badges — so adding a setting means adding it here and to the
dataclass in config.py, not writing another block of HTML.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .config import PLEX_ACTIONS


@dataclass
class Field:
    path: str                       # dotted config path, relative to the section root
    label: str
    type: str = "text"              # text | password | url | number | bool | select | list
    help: str = ""
    placeholder: str = ""
    options: list[str] = field(default_factory=list)
    min: int | None = None
    max: int | None = None
    advanced: bool = False
    unit: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "", [], False)} | {
            "path": self.path, "label": self.label, "type": self.type,
        }


@dataclass
class Section:
    key: str
    title: str
    description: str = ""
    fields: list[Field] = field(default_factory=list)
    # Repeating sections (Sonarr/Radarr instances) render N copies of `fields`.
    repeatable: bool = False
    list_path: str = ""
    service: str = ""               # enables the "Test connection" button
    icon: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "title": self.title, "description": self.description,
            "repeatable": self.repeatable, "list_path": self.list_path,
            "service": self.service, "icon": self.icon,
            "fields": [f.to_dict() for f in self.fields],
        }


TIMEOUT = Field("timeout", "Timeout", "number", "Seconds to wait for a response.",
                min=5, max=600, advanced=True, unit="s")
VERIFY_SSL = Field("verify_ssl", "Verify SSL certificate", "bool",
                   "Turn off for self-signed certificates on your LAN.", advanced=True)

ARR_FIELDS = [
    Field("name", "Instance name", "text", "Must be unique. Shown throughout the UI.",
          placeholder="sonarr-4k"),
    Field("enabled", "Enabled", "bool"),
    Field("url", "URL", "url", placeholder="http://192.168.1.10:8989"),
    Field("api_key", "API key", "password", "Settings → General → API Key."),
    Field("delete_files", "Delete files on disk", "bool",
          "Off means the entry is removed but the files stay where they are."),
    Field("add_import_exclusion", "Add import exclusion", "bool",
          "Stops import lists re-adding what you just removed."),
    VERIFY_SSL, TIMEOUT,
]

SECTIONS: list[Section] = [
    Section(
        key="plex", title="Plex", service="plex", icon="🎬",
        description="Supplies the library inventory: titles, database ids, file sizes, "
                    "labels and collections.",
        fields=[
            Field("enabled", "Enabled", "bool"),
            Field("url", "Server URL", "url", placeholder="http://192.168.1.10:32400"),
            Field("token", "X-Plex-Token", "password",
                  "Open any library item in Plex Web → Get Info → View XML, then copy "
                  "X-Plex-Token from the address bar."),
            Field("exclude_libraries", "Never touch these libraries", "list",
                  "Names exactly as they appear in Plex.", placeholder="Home Videos"),
            Field("libraries", "Only scan these libraries", "list",
                  "Leave empty to scan every movie and show library.", advanced=True),
            VERIFY_SSL, TIMEOUT,
        ],
    ),
    Section(
        key="tautulli", title="Tautulli", service="tautulli", icon="📊",
        description="The authority on who watched what and when. Without it, only the "
                    "server owner's playback is visible, so titles your friends watch "
                    "regularly will look untouched.",
        fields=[
            Field("enabled", "Enabled", "bool"),
            Field("url", "URL", "url", placeholder="http://192.168.1.10:8181"),
            Field("api_key", "API key", "password", "Settings → Web Interface → API."),
            Field("purge_history_on_delete", "Purge history when deleting", "bool",
                  "Also clears watch statistics for removed items and users.", advanced=True),
            VERIFY_SSL, TIMEOUT,
        ],
    ),
    Section(
        key="sonarr", title="Sonarr", repeatable=True, list_path="sonarr", icon="📺",
        description="Owns TV files on disk. Add more than one instance if you run a "
                    "separate 4K setup.",
        fields=ARR_FIELDS,
    ),
    Section(
        key="radarr", title="Radarr", repeatable=True, list_path="radarr", icon="🍿",
        description="Owns movie files on disk.",
        fields=ARR_FIELDS,
    ),
    Section(
        key="seerr", title="Seerr", service="seerr", icon="🎟️",
        description="Overseerr, Jellyseerr and Seerr all work here — they share the same API.",
        fields=[
            Field("enabled", "Enabled", "bool"),
            Field("url", "URL", "url", placeholder="http://192.168.1.10:5055"),
            Field("api_key", "API key", "password", "Settings → General → API Key."),
            Field("remove_media_entry", "Clear the media entry on delete", "bool",
                  "Makes the title requestable again."),
            Field("delete_file_via_seerr", "Delete files through Seerr", "bool",
                  "Leave off when Sonarr and Radarr are connected — they already handle "
                  "file deletion, and doing both is redundant.", advanced=True),
            VERIFY_SSL, TIMEOUT,
        ],
    ),
    Section(
        key="rules.media", title="Media rules", icon="📐",
        description="When a movie or show becomes a deletion candidate.",
        fields=[
            Field("unwatched_days", "Unwatched threshold", "number",
                  "Days since anyone last watched it.", min=1, max=10000, unit="days"),
            Field("never_watched_after_days", "Never-watched grace period", "number",
                  "How long something nobody has ever watched is left alone, counted "
                  "from when it was added.", min=1, max=10000, unit="days"),
            Field("min_age_days", "Minimum age", "number",
                  "Nothing newer than this is ever proposed, watched or not.",
                  min=0, max=10000, unit="days"),
            Field("protect_continuing_series", "Keep shows that are still airing", "bool",
                  "Protects anything Sonarr lists as continuing and monitored."),
            Field("protect_in_progress", "Keep partly-watched items", "bool",
                  "Protects anything somebody is in the middle of."),
            Field("protect_recent_seerr_requests_days", "Keep recent Seerr requests", "number",
                  "Protects titles requested within this many days. 0 disables the check.",
                  min=0, max=10000, unit="days"),
            Field("protect_plex_labels", "Protected Plex labels", "list",
                  "Anything carrying one of these labels is never proposed.",
                  placeholder="keep"),
            Field("protect_collections", "Protected Plex collections", "list",
                  placeholder="Christmas"),
            Field("protect_arr_tags", "Protected Sonarr/Radarr tags", "list",
                  placeholder="permanent"),
            Field("min_size_mb", "Minimum size to bother with", "number",
                  "Skip items smaller than this. 0 means no floor.",
                  min=0, max=1000000, advanced=True, unit="MB"),
            Field("include_libraries", "Restrict to these libraries", "list",
                  "Leave empty to consider every scanned library.", advanced=True),
        ],
    ),
    Section(
        key="rules.users", title="User rules", icon="👥",
        description="When a user becomes a removal candidate, and what removal means.",
        fields=[
            Field("inactive_days", "Inactive threshold", "number",
                  "Days with no playback and no Seerr login.", min=1, max=10000, unit="days"),
            Field("never_active_after_days", "Never-active grace period", "number",
                  "How long a brand-new account with no activity is left alone.",
                  min=1, max=10000, unit="days"),
            Field("plex_action", "What to do in Plex", "select",
                  "Unsharing revokes library access but keeps the friendship, and can be "
                  "undone. Removing a friend cannot.", options=list(PLEX_ACTIONS)),
            Field("remove_from_seerr", "Delete their Seerr account", "bool"),
            Field("remove_from_tautulli", "Delete them from Tautulli", "bool",
                  "This destroys their watch statistics permanently."),
            Field("protect_users", "Never remove these people", "list",
                  "Usernames or email addresses.", placeholder="you@example.com"),
            Field("protect_admins", "Never remove admins", "bool"),
            Field("protect_home_users", "Never remove Plex Home users", "bool"),
        ],
    ),
    Section(
        key="safety", title="Safety", icon="🛟",
        description="The guard rails. Worth leaving strict until you have watched a few "
                    "simulations behave the way you expect.",
        fields=[
            Field("dry_run", "Dry run", "bool",
                  "While on, nothing is ever deleted — every action is simulated and "
                  "logged. This is the master switch."),
            Field("confirm_phrase", "Confirmation phrase", "text",
                  "Must be typed exactly before a live execution runs."),
            Field("max_media_deletions_per_run", "Max media items per run", "number",
                  min=0, max=100000),
            Field("max_user_removals_per_run", "Max users per run", "number", min=0, max=10000),
            Field("max_gigabytes_per_run", "Max gigabytes per run", "number",
                  "0 means no limit.", min=0, max=1000000, unit="GB"),
            Field("snapshot_before_delete", "Snapshot before deleting", "bool",
                  "Writes a JSON record of each item so you can re-add it later."),
            Field("abort_after_failures", "Abort after consecutive failures", "number",
                  min=1, max=100, advanced=True),
        ],
    ),
    Section(
        key="schedule", title="Schedule", icon="🕒",
        description="Automatic scans refresh the candidate lists. Deletions are never "
                    "automatic — they always need you to review and confirm a plan.",
        fields=[
            Field("scan_enabled", "Scan automatically", "bool"),
            Field("scan_cron_hour", "Hour (UTC)", "number", min=0, max=23),
            Field("scan_cron_minute", "Minute", "number", min=0, max=59),
            Field("retention_days", "Keep history for", "number",
                  "Audit log entries and old scans older than this are pruned.",
                  min=7, max=10000, unit="days"),
        ],
    ),
    Section(
        key="app", title="Access and security", icon="🔐",
        description="Who can reach this web UI. It holds API keys for five services and "
                    "can delete your library, so keep it on your LAN.",
        fields=[
            Field("password", "Web UI password", "password",
                  "Leave empty for no login prompt. Recommended even on a trusted LAN."),
            Field("allowed_networks", "Allowed networks", "list",
                  "Requests from outside these CIDR ranges get a 403. Localhost is always "
                  "allowed.", placeholder="192.168.1.0/24"),
            Field("trust_proxy", "Trust reverse proxy headers", "bool",
                  "Enable only when a reverse proxy you control sits in front. Otherwise "
                  "the network guard can be bypassed with a forged X-Forwarded-For header."),
            Field("session_hours", "Session length", "number", min=1, max=720,
                  advanced=True, unit="hours"),
            Field("log_level", "Log level", "select",
                  options=["DEBUG", "INFO", "WARNING", "ERROR"], advanced=True),
        ],
    ),
]

# The wizard walks a subset, in this order.
WIZARD_STEPS = [
    {"key": "welcome", "title": "Welcome", "sections": []},
    {"key": "access", "title": "Access", "sections": ["app"]},
    {"key": "core", "title": "Plex & Tautulli", "sections": ["plex", "tautulli"]},
    {"key": "arr", "title": "Sonarr & Radarr", "sections": ["sonarr", "radarr"]},
    {"key": "seerr", "title": "Seerr", "sections": ["seerr"]},
    {"key": "rules", "title": "Rules", "sections": ["rules.media", "rules.users"]},
    {"key": "review", "title": "Review", "sections": ["safety"]},
]


def schema_json() -> dict[str, Any]:
    return {
        "sections": [s.to_dict() for s in SECTIONS],
        "wizard": WIZARD_STEPS,
        "arr_template": {f.path: None for f in ARR_FIELDS},
    }


def section_by_key(key: str) -> Section | None:
    return next((s for s in SECTIONS if s.key == key), None)
