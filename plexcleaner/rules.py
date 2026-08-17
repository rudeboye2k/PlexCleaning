"""Rules engine.

Every item gets one of three verdicts:

  protected — a rule explicitly says never touch this
  candidate — stale by the configured thresholds and not protected
  keep      — recently used, or too new to judge

Protection always beats staleness. Reasons are recorded either way so the UI
can explain any verdict without re-deriving it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from .config import MediaRules, UserRules
from .models import MediaItem, UserAccount, days_since


def _lower(values: Iterable[str]) -> set[str]:
    return {str(v).strip().lower() for v in values or [] if str(v).strip()}


def evaluate_media(items: list[MediaItem], rules: MediaRules, *,
                   protected_refs: set[str] | None = None,
                   now: datetime | None = None) -> list[MediaItem]:
    now = now or datetime.now(timezone.utc)
    protected_refs = protected_refs or set()
    keep_labels = _lower(rules.protect_plex_labels)
    keep_collections = _lower(rules.protect_collections)
    keep_tags = _lower(rules.protect_arr_tags)
    include_libraries = _lower(rules.include_libraries)

    for item in items:
        item.reasons = []
        item.verdict = _evaluate_one_media(
            item, rules, now, protected_refs, keep_labels, keep_collections, keep_tags, include_libraries
        )
    return items


def _evaluate_one_media(item: MediaItem, rules: MediaRules, now: datetime,
                        protected_refs: set[str], keep_labels: set[str],
                        keep_collections: set[str], keep_tags: set[str],
                        include_libraries: set[str]) -> str:
    add = item.reasons.append

    # --- hard protections ------------------------------------------------
    if item.ref in protected_refs:
        add("On the permanent keep list")
        return "protected"

    if include_libraries and str(item.plex_library or "").lower() not in include_libraries:
        add(f"Library '{item.plex_library}' is not in the include list")
        return "protected"

    hit_labels = _lower(item.labels) & keep_labels
    if hit_labels:
        add(f"Protected Plex label: {', '.join(sorted(hit_labels))}")
        return "protected"

    hit_collections = _lower(item.collections) & keep_collections
    if hit_collections:
        add(f"In protected collection: {', '.join(sorted(hit_collections))}")
        return "protected"

    hit_tags = _lower(item.arr_tags) & keep_tags
    if hit_tags:
        add(f"Protected {item.arr_instance or 'arr'} tag: {', '.join(sorted(hit_tags))}")
        return "protected"

    if rules.protect_continuing_series and item.kind == "show":
        if str(item.arr_status or "").lower() == "continuing" and item.arr_monitored:
            add("Still airing and monitored in Sonarr")
            return "protected"

    if rules.protect_in_progress and item.in_progress:
        add("Partially watched — someone is in the middle of it")
        return "protected"

    if rules.protect_recent_seerr_requests_days > 0 and item.seerr_requested_at:
        age = days_since(item.seerr_requested_at, now)
        if age is not None and age < rules.protect_recent_seerr_requests_days:
            add(f"Requested in Seerr {age}d ago by {item.seerr_requested_by or 'a user'}")
            return "protected"

    # --- too new / too small to judge -------------------------------------
    age_days = days_since(item.added_at, now)
    if age_days is not None and age_days < rules.min_age_days:
        add(f"Added {age_days}d ago (minimum age is {rules.min_age_days}d)")
        return "keep"

    if rules.min_size_mb > 0 and item.size_bytes < rules.min_size_mb * 1_000_000:
        add(f"Smaller than the {rules.min_size_mb} MB floor — not worth reclaiming")
        return "keep"

    # --- staleness --------------------------------------------------------
    if item.last_watched_at is None:
        if age_days is None:
            add("Never watched, but the date it was added is unknown — not proposing it")
            return "keep"
        if age_days >= rules.never_watched_after_days:
            add(f"Never watched by anyone in {age_days}d since it was added")
            return "candidate"
        add(f"Never watched, but only {age_days}d old (threshold {rules.never_watched_after_days}d)")
        return "keep"

    unwatched = days_since(item.last_watched_at, now) or 0
    if unwatched >= rules.unwatched_days:
        who = f" by {item.watcher_count} user(s)" if item.watcher_count else ""
        add(f"Last watched {unwatched}d ago{who} (threshold {rules.unwatched_days}d)")
        return "candidate"

    add(f"Watched {unwatched}d ago")
    return "keep"


def evaluate_users(users: list[UserAccount], rules: UserRules, *,
                   protected_refs: set[str] | None = None,
                   now: datetime | None = None) -> list[UserAccount]:
    now = now or datetime.now(timezone.utc)
    protected_refs = protected_refs or set()
    protect_list = _lower(rules.protect_users)

    for user in users:
        user.reasons = []
        user.verdict = _evaluate_one_user(user, rules, now, protected_refs, protect_list)
    return users


def _evaluate_one_user(user: UserAccount, rules: UserRules, now: datetime,
                       protected_refs: set[str], protect_list: set[str]) -> str:
    add = user.reasons.append

    if user.ref in protected_refs:
        add("On the permanent keep list")
        return "protected"

    identities = _lower([user.username, user.email, user.title])
    hit = identities & protect_list
    if hit:
        add(f"Explicitly protected: {', '.join(sorted(hit))}")
        return "protected"

    if rules.protect_admins and user.is_admin:
        add("Server owner or admin")
        return "protected"

    if rules.protect_home_users and user.is_home:
        add("Plex Home user")
        return "protected"

    activity = user.last_activity
    if activity is None:
        # Never watched and never logged in. Give brand-new accounts a grace
        # period measured from when we first saw them.
        age = days_since(user.first_seen_at, now)
        if age is None:
            add("No activity on record and no account creation date — not proposing them")
            return "keep"
        if age >= rules.never_active_after_days:
            add(f"Never watched or logged in, account is {age}d old")
            return "candidate"
        add(f"No activity yet, but the account is only {age}d old")
        return "keep"

    inactive = days_since(activity, now) or 0
    if inactive >= rules.inactive_days:
        detail = []
        if user.last_seen_at:
            detail.append(f"last watched {days_since(user.last_seen_at, now)}d ago")
        if user.last_login_at:
            detail.append(f"last Seerr login {days_since(user.last_login_at, now)}d ago")
        add(f"Inactive for {inactive}d ({'; '.join(detail) or 'no recent activity'})")
        return "candidate"

    add(f"Active {inactive}d ago")
    return "keep"


def protection_key(kind: str, ref: str) -> str:
    """Namespaced key used by the protection table."""
    return ref if ref.startswith(("movie:", "show:", "user:")) else f"{kind}:{ref}"
