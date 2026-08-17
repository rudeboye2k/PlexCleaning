"""Plan building and execution.

Deletion is split into two phases on purpose:

  build_plan()   turns selected candidates into an explicit, reviewable list of
                 service calls. It touches nothing.
  execute_plan() runs those calls, in dependency order, honouring dry-run,
                 per-run caps and a typed confirmation phrase.

Step ordering matters. Sonarr and Radarr own the files on disk, so they go
first; Seerr's entry is cleared next so the title becomes requestable again;
Plex is last and only deletes directly when no *arr instance owned the item.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from .clients import ClientError
from .config import Config
from .db import Database, utcnow
from .models import MediaItem, Step, UserAccount
from .scan import Services

log = logging.getLogger(__name__)


class PlanError(Exception):
    """A plan cannot be built or is refused by a safety rule."""


@dataclass
class PlanItem:
    kind: str                 # media | user
    title: str
    ref: str
    size_bytes: int
    steps: list[Step]
    snapshot: dict[str, Any]


# --------------------------------------------------------------------------
# Plan building
# --------------------------------------------------------------------------

def build_media_steps(item: MediaItem, cfg: Config) -> list[Step]:
    steps: list[Step] = []
    handled_files_by_arr = False

    if item.arr_instance and item.arr_id:
        arr_cfg = next((c for c in cfg.sonarr + cfg.radarr if c.name == item.arr_instance), None)
        if arr_cfg and arr_cfg.enabled:
            service = "sonarr" if item.kind == "show" else "radarr"
            handled_files_by_arr = arr_cfg.delete_files
            steps.append(Step(
                service=item.arr_instance,
                action=f"delete_{'series' if item.kind == 'show' else 'movie'}",
                target=str(item.arr_id),
                params={
                    "delete_files": arr_cfg.delete_files,
                    "add_exclusion": arr_cfg.add_import_exclusion,
                    "service_kind": service,
                },
                description=(
                    f"Remove from {item.arr_instance}"
                    f"{' and delete files' if arr_cfg.delete_files else ' (keep files)'}"
                    f"{', add import exclusion' if arr_cfg.add_import_exclusion else ''}"
                ),
            ))

    if cfg.seerr.enabled and item.seerr_media_id:
        if cfg.seerr.delete_file_via_seerr and not handled_files_by_arr:
            steps.append(Step(
                service="seerr",
                action="delete_media_file",
                target=str(item.seerr_media_id),
                description="Delete the file through Seerr",
            ))
        if cfg.seerr.remove_media_entry:
            steps.append(Step(
                service="seerr",
                action="delete_media",
                target=str(item.seerr_media_id),
                description="Clear the Seerr entry so it becomes requestable again",
            ))

    if cfg.plex.enabled and item.plex_rating_key:
        if handled_files_by_arr:
            # The files are gone; just make Plex notice.
            steps.append(Step(
                service="plex",
                action="refresh_section",
                target=str(item.plex_section_id or ""),
                description=f"Refresh Plex library '{item.plex_library}' so the empty entry drops out",
            ))
        else:
            steps.append(Step(
                service="plex",
                action="delete_item",
                target=item.plex_rating_key,
                description="Delete from Plex, including files on disk",
            ))

    if cfg.tautulli.enabled and cfg.tautulli.purge_history_on_delete and item.plex_rating_key:
        steps.append(Step(
            service="tautulli",
            action="purge_media_history",
            target=item.plex_rating_key,
            description="Purge Tautulli watch history for this item",
        ))

    return steps


def build_user_steps(user: UserAccount, cfg: Config) -> list[Step]:
    steps: list[Step] = []
    rules = cfg.rules.users

    if cfg.seerr.enabled and rules.remove_from_seerr and user.seerr_user_id:
        steps.append(Step(
            service="seerr",
            action="delete_user",
            target=str(user.seerr_user_id),
            description="Delete the Seerr account and its request history",
        ))

    if cfg.plex.enabled and rules.plex_action != "none" and user.username:
        if rules.plex_action == "unshare":
            if user.has_plex_share:
                steps.append(Step(
                    service="plex",
                    action="unshare",
                    target=user.username,
                    description="Revoke library access, keep the friendship (reversible)",
                ))
        elif rules.plex_action == "remove_friend":
            steps.append(Step(
                service="plex",
                action="remove_friend",
                target=user.username,
                description="Remove the sharing relationship entirely",
            ))

    if cfg.tautulli.enabled and rules.remove_from_tautulli and user.tautulli_user_id:
        steps.append(Step(
            service="tautulli",
            action="delete_user",
            target=user.tautulli_user_id,
            description="Delete the user and their history from Tautulli (destroys stats)",
        ))

    return steps


def _row_to_media(row) -> MediaItem:
    from .models import parse_ts
    detail = json.loads(row["detail"] or "{}")
    return MediaItem(
        kind=row["kind"],
        title=row["title"],
        year=row["year"],
        plex_rating_key=row["plex_rating_key"],
        plex_section_id=row["plex_section_id"],
        plex_library=row["plex_library"],
        tmdb_id=row["tmdb_id"],
        tvdb_id=row["tvdb_id"],
        imdb_id=row["imdb_id"],
        added_at=parse_ts(row["added_at"]),
        last_watched_at=parse_ts(row["last_watched_at"]),
        play_count=row["play_count"],
        watcher_count=row["watcher_count"],
        size_bytes=row["size_bytes"],
        episode_count=row["episode_count"],
        arr_instance=row["arr_instance"],
        arr_id=row["arr_id"],
        arr_status=row["arr_status"],
        arr_monitored=bool(row["arr_monitored"]),
        arr_tags=json.loads(row["arr_tags"] or "[]"),
        arr_path=detail.get("arr_path"),
        seerr_media_id=row["seerr_media_id"],
        seerr_requested_by=row["seerr_requested_by"],
        seerr_requested_at=parse_ts(row["seerr_requested_at"]),
        labels=json.loads(row["labels"] or "[]"),
        collections=json.loads(row["collections"] or "[]"),
        verdict=row["verdict"],
        reasons=json.loads(row["reasons"] or "[]"),
        detail=detail,
    )


def _row_to_user(row) -> UserAccount:
    from .models import parse_ts
    detail = json.loads(row["detail"] or "{}")
    return UserAccount(
        username=row["username"],
        email=row["email"],
        title=row["title"],
        plex_user_id=row["plex_user_id"],
        plex_share_id=row["plex_share_id"],
        tautulli_user_id=row["tautulli_user_id"],
        seerr_user_id=row["seerr_user_id"],
        is_admin=bool(row["is_admin"]),
        is_home=bool(row["is_home"]),
        has_plex_share=bool(detail.get("has_plex_share", True)),
        last_seen_at=parse_ts(row["last_seen_at"]),
        last_login_at=parse_ts(row["last_login_at"]),
        plays=row["plays"],
        request_count=row["request_count"],
        verdict=row["verdict"],
        reasons=json.loads(row["reasons"] or "[]"),
        detail=detail,
    )


def build_plan(db: Database, cfg: Config, *, scan_id: int, media_ids: list[int],
               user_ids: list[int], created_by: str = "web") -> int:
    """Turn selected candidate rows into a stored, reviewable plan."""
    if not media_ids and not user_ids:
        raise PlanError("Nothing selected — pick at least one item or user.")

    items: list[PlanItem] = []

    if media_ids:
        marks = ",".join("?" for _ in media_ids)
        rows = db.query(
            f"SELECT * FROM media_item WHERE scan_id = ? AND id IN ({marks})",
            (scan_id, *media_ids),
        )
        protected = db.protected_refs("media")
        for row in rows:
            item = _row_to_media(row)
            if item.verdict == "protected" or item.ref in protected:
                log.warning("refusing to plan protected item %s", item.ref)
                continue
            steps = build_media_steps(item, cfg)
            if not steps:
                log.warning("no actionable steps for %s — skipping", item.ref)
                continue
            items.append(PlanItem("media", f"{item.title} ({item.year or '?'})",
                                  item.ref, item.size_bytes, steps, item.snapshot()))

    if user_ids:
        marks = ",".join("?" for _ in user_ids)
        rows = db.query(
            f"SELECT * FROM user_account WHERE scan_id = ? AND id IN ({marks})",
            (scan_id, *user_ids),
        )
        protected = db.protected_refs("user")
        for row in rows:
            user = _row_to_user(row)
            if user.verdict == "protected" or user.ref in protected:
                log.warning("refusing to plan protected user %s", user.ref)
                continue
            steps = build_user_steps(user, cfg)
            if not steps:
                log.warning("no actionable steps for user %s — skipping", user.ref)
                continue
            items.append(PlanItem("user", user.username, user.ref, 0, steps, user.snapshot()))

    if not items:
        raise PlanError(
            "Nothing actionable. The selected rows are either protected or have no "
            "matching entry in any enabled service."
        )

    summary = {
        "media_count": sum(1 for i in items if i.kind == "media"),
        "user_count": sum(1 for i in items if i.kind == "user"),
        "total_bytes": sum(i.size_bytes for i in items),
        "step_count": sum(len(i.steps) for i in items),
    }
    plan_id = db.insert("plan", {
        "scan_id": scan_id,
        "created_at": utcnow(),
        "status": "draft",
        "dry_run": int(cfg.safety.dry_run),
        "created_by": created_by,
        "summary": json.dumps(summary),
    })
    db.insert_many("plan_item", [{
        "plan_id": plan_id,
        "kind": i.kind,
        "title": i.title,
        "ref": i.ref,
        "size_bytes": i.size_bytes,
        "steps": json.dumps([s.to_dict() for s in i.steps]),
        "status": "pending",
        "result": "{}",
        "snapshot": json.dumps(i.snapshot, default=str),
    } for i in items])

    db.audit(service="plan", action="plan_created", target=f"plan:{plan_id}", actor=created_by,
             dry_run=cfg.safety.dry_run, ok=True, plan_id=plan_id, detail=summary)
    log.info("plan %s built: %s", plan_id, summary)
    return plan_id


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

class Executor:
    def __init__(self, cfg: Config, db: Database, services: Services | None = None):
        self.cfg = cfg
        self.db = db
        self.services = services or Services(cfg)

    def preflight(self, plan_id: int, confirm: str, *, force_live: bool = False) -> tuple[bool, list[str]]:
        """Check every safety rule. Returns (dry_run, problems)."""
        problems: list[str] = []
        plan = self.db.query_one("SELECT * FROM plan WHERE id = ?", (plan_id,))
        if not plan:
            return True, [f"Plan {plan_id} does not exist"]
        if plan["status"] not in ("draft",):
            problems.append(f"Plan {plan_id} is '{plan['status']}', not a draft")

        dry_run = self.cfg.safety.dry_run or not force_live
        if not dry_run and confirm.strip() != self.cfg.safety.confirm_phrase:
            problems.append(f"Confirmation phrase must be exactly '{self.cfg.safety.confirm_phrase}'")

        rows = self.db.query("SELECT kind, size_bytes FROM plan_item WHERE plan_id = ?", (plan_id,))
        media = [r for r in rows if r["kind"] == "media"]
        users = [r for r in rows if r["kind"] == "user"]
        safety = self.cfg.safety

        if len(media) > safety.max_media_deletions_per_run:
            problems.append(
                f"{len(media)} media items exceeds the cap of {safety.max_media_deletions_per_run} per run"
            )
        if len(users) > safety.max_user_removals_per_run:
            problems.append(
                f"{len(users)} users exceeds the cap of {safety.max_user_removals_per_run} per run"
            )
        if safety.max_gigabytes_per_run > 0:
            gb = sum(r["size_bytes"] for r in rows) / 1_000_000_000
            if gb > safety.max_gigabytes_per_run:
                problems.append(
                    f"{gb:.1f} GB exceeds the cap of {safety.max_gigabytes_per_run} GB per run"
                )
        return dry_run, problems

    def execute(self, plan_id: int, *, confirm: str = "", force_live: bool = False,
                actor: str = "web") -> dict[str, Any]:
        dry_run, problems = self.preflight(plan_id, confirm, force_live=force_live)
        if problems:
            raise PlanError("; ".join(problems))

        self.db.execute("UPDATE plan SET status = 'executing', dry_run = ? WHERE id = ?",
                        (int(dry_run), plan_id))
        self.db.audit(service="plan", action="execute_start", target=f"plan:{plan_id}", actor=actor,
                      dry_run=dry_run, ok=True, plan_id=plan_id)

        items = self.db.query("SELECT * FROM plan_item WHERE plan_id = ? ORDER BY kind, id", (plan_id,))
        results = {"done": 0, "failed": 0, "skipped": 0, "bytes_freed": 0, "dry_run": dry_run}
        consecutive_failures = 0

        for row in items:
            if consecutive_failures >= self.cfg.safety.abort_after_failures:
                self.db.execute("UPDATE plan_item SET status = 'skipped', result = ? WHERE id = ?",
                                (json.dumps({"reason": "aborted after repeated failures"}), row["id"]))
                results["skipped"] += 1
                continue

            snapshot = json.loads(row["snapshot"] or "{}")
            if self.cfg.safety.snapshot_before_delete and not dry_run:
                self._write_snapshot(plan_id, row["ref"], snapshot)

            steps = [Step(**s) for s in json.loads(row["steps"] or "[]")]
            step_results: list[dict[str, Any]] = []
            item_ok = True

            for step in steps:
                outcome = self._run_step(step, dry_run=dry_run, plan_id=plan_id, actor=actor)
                step_results.append(outcome)
                if not outcome["ok"]:
                    item_ok = False
                    # A failed *arr deletion means the files are still there, so
                    # do not go on to clear the Seerr entry or the Plex row —
                    # that would leave the library inconsistent.
                    if step.service not in ("tautulli",):
                        break

            status = "done" if item_ok else "failed"
            self.db.execute(
                "UPDATE plan_item SET status = ?, result = ? WHERE id = ?",
                (status, json.dumps({"steps": step_results}, default=str), row["id"]),
            )
            if item_ok:
                results["done"] += 1
                results["bytes_freed"] += row["size_bytes"]
                consecutive_failures = 0
            else:
                results["failed"] += 1
                consecutive_failures += 1

        final = "done" if results["failed"] == 0 else "failed"
        # Merge rather than replace: the build-time counts (how much was planned)
        # stay meaningful alongside the run's outcome.
        plan_row = self.db.query_one("SELECT summary FROM plan WHERE id = ?", (plan_id,))
        summary = json.loads(plan_row["summary"] or "{}") if plan_row else {}
        summary["execution"] = results
        self.db.execute("UPDATE plan SET status = ?, executed_at = ?, summary = ? WHERE id = ?",
                        (final, utcnow(), json.dumps(summary), plan_id))
        self.db.audit(service="plan", action="execute_finish", target=f"plan:{plan_id}", actor=actor,
                      dry_run=dry_run, ok=results["failed"] == 0, plan_id=plan_id, detail=results)
        log.info("plan %s finished: %s", plan_id, results)
        return results

    def _write_snapshot(self, plan_id: int, ref: str, snapshot: dict[str, Any]) -> None:
        try:
            folder = self.cfg.backup_path / f"plan-{plan_id}"
            folder.mkdir(parents=True, exist_ok=True)
            safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in ref)[:120]
            (folder / f"{safe}.json").write_text(
                json.dumps(snapshot, indent=2, default=str), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("could not write snapshot for %s: %s", ref, exc)

    def _run_step(self, step: Step, *, dry_run: bool, plan_id: int, actor: str) -> dict[str, Any]:
        label = f"{step.service}.{step.action}({step.target})"
        if dry_run:
            self.db.audit(service=step.service, action=step.action, target=step.target, actor=actor,
                          dry_run=True, ok=True, plan_id=plan_id,
                          detail={"simulated": True, "description": step.description, "params": step.params})
            return {"step": label, "ok": True, "dry_run": True, "detail": "simulated"}

        try:
            detail = self._dispatch(step)
            self.db.audit(service=step.service, action=step.action, target=step.target, actor=actor,
                          dry_run=False, ok=True, plan_id=plan_id, detail=detail)
            return {"step": label, "ok": True, "dry_run": False, "detail": detail}
        except (ClientError, PlanError, ValueError) as exc:
            log.error("step failed: %s -> %s", label, exc)
            self.db.audit(service=step.service, action=step.action, target=step.target, actor=actor,
                          dry_run=False, ok=False, plan_id=plan_id, detail={"error": str(exc)})
            return {"step": label, "ok": False, "dry_run": False, "error": str(exc)}

    def _dispatch(self, step: Step) -> dict[str, Any]:
        """Route one step to the client that performs it."""
        svc = self.services

        if step.action in ("delete_series", "delete_movie"):
            client = svc.arr_client(step.service)
            if client is None:
                raise PlanError(f"{step.service} is not enabled — cannot run {step.action}")
            return client.delete_item(
                int(step.target),
                delete_files=bool(step.params.get("delete_files", True)),
                add_exclusion=bool(step.params.get("add_exclusion", True)),
            )

        if step.service == "seerr":
            if svc.seerr is None:
                raise PlanError("seerr is not enabled")
            if step.action == "delete_media":
                return svc.seerr.delete_media(int(step.target))
            if step.action == "delete_media_file":
                return svc.seerr.delete_media_file(int(step.target))
            if step.action == "delete_user":
                return svc.seerr.delete_user(int(step.target))

        if step.service == "plex":
            if svc.plex is None:
                raise PlanError("plex is not enabled")
            if step.action == "delete_item":
                return svc.plex.delete_item(step.target)
            if step.action == "refresh_section":
                return svc.plex.refresh_section(step.target)
            if step.action == "unshare":
                return svc.plex.unshare(step.target)
            if step.action == "remove_friend":
                return svc.plex.remove_friend(step.target)

        if step.service == "tautulli":
            if svc.tautulli is None:
                raise PlanError("tautulli is not enabled")
            if step.action == "delete_user":
                svc.tautulli.delete_user_history(step.target)
                return svc.tautulli.delete_user(step.target)
            if step.action == "purge_media_history":
                return {"note": "per-item history purge is not supported by the Tautulli API"}

        raise PlanError(f"unknown step: {step.service}.{step.action}")


def cancel_plan(db: Database, plan_id: int, actor: str = "web") -> None:
    db.execute("UPDATE plan SET status = 'cancelled' WHERE id = ? AND status = 'draft'", (plan_id,))
    db.audit(service="plan", action="plan_cancelled", target=f"plan:{plan_id}", actor=actor,
             dry_run=True, ok=True, plan_id=plan_id)
