"""Command line interface.

Everything here is optional — the web portal can do all of it. This exists for
cron jobs, for scripting, and for checking a container's configuration without
opening a browser.

    plexcleaner serve                    start the web UI (the container default)
    plexcleaner config                   show the effective configuration
    plexcleaner test                     check connectivity to every service
    plexcleaner scan                     refresh the candidate lists
    plexcleaner report --kind movie      list current candidates
    plexcleaner plan --top 10            build a plan from the biggest candidates
    plexcleaner apply <id> --live        execute a plan
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from . import __version__
from .actions import Executor, PlanError, build_plan
from .config import Config, ConfigError
from .db import Database
from .scan import Scanner, Services
from .settings_store import SettingsStore, build_store


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _gb(byte_count: Any) -> str:
    return f"{(byte_count or 0) / 1_000_000_000:.1f} GB"


def cmd_config(cfg: Config, db: Database, store: SettingsStore, args) -> int:
    if args.yaml:
        print(cfg.to_yaml(redact=not args.show_secrets))
        return 0

    print(f"PlexCleaner {__version__}")
    print(f"  data directory : {cfg.data_path}")
    print(f"  config file    : {cfg.source_path or '(none — using env vars and saved settings)'}")
    print(f"  settings rev   : {cfg.revision}")
    print(f"  configured     : {'yes' if cfg.configured else 'no — open the web UI to run setup'}")
    print(f"  mode           : {'DRY RUN' if cfg.safety.dry_run else 'LIVE'}"
          f"{' (locked by PLEXCLEANER_LOCK_SAFETY)' if cfg.safety_locked else ''}")
    print(f"  web UI         : http://{cfg.app.host}:{cfg.app.port}"
          f"  password {'set' if cfg.app.password else 'NOT set'}")
    print(f"  allowed nets   : {', '.join(cfg.app.allowed_networks) or 'localhost only'}")

    print("\n  Services:")
    for label, svc, key in (("plex", cfg.plex, "token"), ("tautulli", cfg.tautulli, "api_key"),
                            ("seerr", cfg.seerr, "api_key")):
        state = "enabled" if svc.enabled else "disabled"
        secret = "key set" if getattr(svc, key, "") else "NO KEY"
        print(f"    {label:<12} {state:<9} {svc.url or '(no url)':<34} {secret}")
    for arr in cfg.all_arrs():
        state = "enabled" if arr.enabled else "disabled"
        secret = "key set" if arr.api_key else "NO KEY"
        print(f"    {arr.name:<12} {state:<9} {arr.url or '(no url)':<34} {secret}")

    shadowed = store.shadowed_env()
    if shadowed:
        print("\n  Saved settings are overriding these environment variables:")
        for path in shadowed:
            print(f"    - {path}")
        print("  Use 'plexcleaner reset-settings' to fall back to the environment.")

    problems, warnings = cfg.problems(), cfg.warnings()
    if problems:
        print("\n  Problems:")
        for p in problems:
            print(f"    ! {p}")
    if warnings:
        print("\n  Warnings:")
        for w in warnings:
            print(f"    - {w}")
    if not problems and not warnings:
        print("\n  No problems found.")
    return 1 if problems else 0


def cmd_reset_settings(cfg: Config, db: Database, store: SettingsStore, args) -> int:
    store.reset(actor="cli")
    print("Saved settings cleared. Configuration now comes from the config file and "
          "environment variables.")
    return 0


def cmd_test(cfg: Config, db: Database, store: SettingsStore, args) -> int:
    problems = cfg.problems()
    if problems:
        print("Configuration problems:")
        for p in problems:
            print(f"  ! {p}")
        print()

    services = Services(cfg)
    try:
        results = services.test_all()
    finally:
        services.close()

    if not results:
        print("No services are enabled yet. Open the web UI to run setup.")
        return 1

    failed = 0
    for r in results:
        mark = "ok  " if r["ok"] else "FAIL"
        version = f" v{r['version']}" if r["version"] else ""
        print(f"[{mark}] {r['name']}{version}")
        if not r["ok"]:
            failed += 1
            print(f"        {r['detail']}")
    print(f"\n{len(results) - failed}/{len(results)} services reachable.")
    return 1 if (failed or problems) else 0


def cmd_scan(cfg: Config, db: Database, store: SettingsStore, args) -> int:
    problems = cfg.problems()
    if problems:
        print("Refusing to scan — fix the configuration first:")
        for p in problems:
            print(f"  ! {p}")
        return 1

    services = Services(cfg)
    try:
        scan_id = Scanner(cfg, db, services).run(args.trigger)
    finally:
        services.close()

    scan = db.query_one("SELECT * FROM scan WHERE id = ?", (scan_id,))
    stats = json.loads(scan["stats"] or "{}")
    errors = json.loads(scan["errors"] or "[]")
    print(f"Scan #{scan_id} complete.")
    print(f"  {stats.get('media_total', 0)} items tracked "
          f"({stats.get('movies_total', 0)} movies, {stats.get('shows_total', 0)} shows)")
    print(f"  {stats.get('media_candidates', 0)} stale, {_gb(stats.get('reclaimable_bytes'))} reclaimable")
    print(f"  {stats.get('users_total', 0)} users, {stats.get('user_candidates', 0)} inactive")
    if errors:
        print("  Non-fatal errors:")
        for e in errors:
            print(f"    - {e}")
    return 0


def cmd_report(cfg: Config, db: Database, store: SettingsStore, args) -> int:
    scan = db.latest_scan()
    if not scan:
        print("No completed scan yet. Run: plexcleaner scan")
        return 1

    if args.users:
        rows = db.query("SELECT * FROM user_account WHERE scan_id = ? AND verdict = ? "
                        "ORDER BY last_seen_at ASC", (scan["id"], args.verdict))
        if args.json:
            print(json.dumps([dict(r) for r in rows], indent=2))
            return 0
        print(f"{len(rows)} user(s) with verdict '{args.verdict}' from scan #{scan['id']}:\n")
        for r in rows:
            reasons = json.loads(r["reasons"] or "[]")
            print(f"  [{r['id']:>5}] {r['username']:<26} last seen "
                  f"{(r['last_seen_at'] or 'never'):<22} {reasons[0] if reasons else ''}")
        return 0

    where = ["scan_id = ?", "verdict = ?"]
    params: list[Any] = [scan["id"], args.verdict]
    if args.kind:
        where.append("kind = ?")
        params.append(args.kind)
    rows = db.query(f"SELECT * FROM media_item WHERE {' AND '.join(where)} "
                    "ORDER BY size_bytes DESC LIMIT ?", (*params, args.limit))
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2))
        return 0

    print(f"{len(rows)} item(s) with verdict '{args.verdict}' from scan #{scan['id']} "
          f"({_gb(sum(r['size_bytes'] for r in rows))}):\n")
    for r in rows:
        reasons = json.loads(r["reasons"] or "[]")
        print(f"  [{r['id']:>6}] {_gb(r['size_bytes']):>9}  {r['title'][:44]:<44} "
              f"{(r['plex_library'] or ''):<14} {reasons[0] if reasons else ''}")
    return 0


def cmd_plan(cfg: Config, db: Database, store: SettingsStore, args) -> int:
    scan = db.latest_scan()
    if not scan:
        print("No completed scan yet. Run: plexcleaner scan")
        return 1

    media_ids = list(args.media_id or [])
    user_ids = list(args.user_id or [])
    if args.top and not media_ids:
        media_ids = [r["id"] for r in db.query(
            "SELECT id FROM media_item WHERE scan_id = ? AND verdict = 'candidate' "
            "ORDER BY size_bytes DESC LIMIT ?", (scan["id"], args.top))]
    if args.top_users and not user_ids:
        user_ids = [r["id"] for r in db.query(
            "SELECT id FROM user_account WHERE scan_id = ? AND verdict = 'candidate' "
            "ORDER BY last_seen_at ASC LIMIT ?", (scan["id"], args.top_users))]

    try:
        plan_id = build_plan(db, cfg, scan_id=scan["id"], media_ids=media_ids,
                             user_ids=user_ids, created_by="cli")
    except PlanError as exc:
        print(f"Could not build a plan: {exc}")
        return 1

    plan = db.query_one("SELECT * FROM plan WHERE id = ?", (plan_id,))
    summary = json.loads(plan["summary"] or "{}")
    print(f"Plan #{plan_id}: {summary.get('media_count', 0)} media, "
          f"{summary.get('user_count', 0)} users, {_gb(summary.get('total_bytes'))}, "
          f"{summary.get('step_count', 0)} service calls.")
    for row in db.query("SELECT * FROM plan_item WHERE plan_id = ? ORDER BY kind, id", (plan_id,)):
        print(f"\n  {row['title']}  [{row['ref']}]")
        for step in json.loads(row["steps"] or "[]"):
            print(f"      - {step['service']}.{step['action']}: {step['description']}")
    print(f"\nReview it at /plans/{plan_id}, or run: plexcleaner apply {plan_id}")
    return 0


def cmd_apply(cfg: Config, db: Database, store: SettingsStore, args) -> int:
    executor = Executor(cfg, db)
    try:
        results = executor.execute(args.plan_id, confirm=args.confirm,
                                   force_live=args.live, actor="cli")
    except PlanError as exc:
        print(f"Refused: {exc}")
        return 1
    finally:
        executor.services.close()

    mode = "Simulated" if results["dry_run"] else "Executed"
    print(f"{mode} plan #{args.plan_id}: {results['done']} succeeded, "
          f"{results['failed']} failed, {results['skipped']} skipped, "
          f"{_gb(results['bytes_freed'])} {'would be ' if results['dry_run'] else ''}freed.")
    return 1 if results["failed"] else 0


def cmd_prune(cfg: Config, db: Database, store: SettingsStore, args) -> int:
    removed = db.prune(args.days or cfg.schedule.retention_days)
    print(f"Removed {removed['audit_rows']} audit rows and {removed['scans']} old scans.")
    return 0


def cmd_serve(cfg: Config, db: Database, store: SettingsStore, args) -> int:
    import uvicorn

    from .scheduler import start_scheduler
    from .web.app import create_app

    app = create_app(store, db)
    start_scheduler(store, db, app.state.runner)

    host = args.host or cfg.app.host
    port = args.port or cfg.app.port

    print(f"PlexCleaner {__version__} — {'DRY RUN' if cfg.safety.dry_run else 'LIVE MODE'}")
    print(f"  listening on http://{host}:{port}")
    print(f"  data directory {cfg.data_path}")
    if not cfg.configured:
        print("  not configured yet — open the web UI and the setup wizard will guide you")
    if not cfg.app.password:
        print("  no password set: anyone on an allowed network can use this")
    for warning in cfg.warnings():
        print(f"  warning: {warning}")

    uvicorn.run(app, host=host, port=port, log_level=str(cfg.app.log_level).lower(),
                proxy_headers=cfg.app.trust_proxy,
                forwarded_allow_ips="*" if cfg.app.trust_proxy else None)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plexcleaner", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-c", "--config", help="path to an optional config.yaml")
    parser.add_argument("--log-level", default="")
    parser.add_argument("--version", action="version", version=f"plexcleaner {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the web UI")
    p.add_argument("--host", default="")
    p.add_argument("--port", type=int, default=0)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("config", help="show the effective configuration")
    p.add_argument("--yaml", action="store_true", help="print as YAML")
    p.add_argument("--show-secrets", action="store_true", help="do not redact keys")
    p.set_defaults(fn=cmd_config)

    sub.add_parser("reset-settings", help="discard web-UI settings and fall back to env/file"
                   ).set_defaults(fn=cmd_reset_settings)

    sub.add_parser("test", help="check connectivity to every configured service"
                   ).set_defaults(fn=cmd_test)

    p = sub.add_parser("scan", help="inventory every service and refresh the candidate lists")
    p.add_argument("--trigger", default="cli")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("report", help="print the current candidates")
    p.add_argument("--kind", choices=["movie", "show"], default="")
    p.add_argument("--verdict", choices=["candidate", "keep", "protected"], default="candidate")
    p.add_argument("--users", action="store_true", help="report on users instead of media")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("plan", help="build a deletion plan without executing it")
    p.add_argument("--media-id", type=int, action="append", help="repeatable media_item id")
    p.add_argument("--user-id", type=int, action="append", help="repeatable user_account id")
    p.add_argument("--top", type=int, default=0, help="take the N largest media candidates")
    p.add_argument("--top-users", type=int, default=0, help="take the N most inactive users")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("apply", help="execute a plan (simulates unless --live)")
    p.add_argument("plan_id", type=int)
    p.add_argument("--live", action="store_true", help="actually delete — requires --confirm")
    p.add_argument("--confirm", default="", help="the safety confirmation phrase")
    p.set_defaults(fn=cmd_apply)

    p = sub.add_parser("prune", help="drop old audit rows and scans")
    p.add_argument("--days", type=int, default=0)
    p.set_defaults(fn=cmd_prune)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        store, db, cfg = build_store(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    setup_logging(args.log_level or cfg.app.log_level)
    try:
        return args.fn(cfg, db, store, args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
