#!/usr/bin/env python3
"""Re-encode oversized media to HEVC, safely, on a schedule.

Finds candidates (from a PlexCleaner scan, or by walking a directory), encodes
them with QuickSync or x265, verifies the result, and only then replaces the
original.

    # See what it would do — this is the default, nothing is written
    ./reencode.py --source /mnt/video --min-gb 20

    # Pull the worklist from PlexCleaner instead of walking a directory
    ./reencode.py --plexcleaner http://10.12.128.4:8585 --min-gb 20

    # Actually do it, for six hours, keeping originals
    ./reencode.py --source /mnt/video --min-gb 20 --apply \\
        --scratch /var/tmp/reencode --until 06:00 --keep-originals

Nightly, via cron on the encoding box:

    0 22 * * *  /opt/plexcleaner/scripts/reencode.py --source /mnt/video \\
                  --min-gb 20 --apply --scratch /var/tmp/reencode --until 06:00 \\
                  >> /var/log/reencode.log 2>&1

Why this is careful, and what it will not do:

* Dry run is the default. `--apply` is required to write anything.
* Encodes to scratch, never in place. A crash or a full disk cannot leave you
  with a truncated file where a movie used to be.
* Verifies before replacing: the output must probe cleanly, run to within a few
  seconds of the original's duration, keep every stream, and actually be
  smaller. Any failure and the original is left alone.
* Skips Dolby Vision by default. The enhancement layer does not survive this
  and some players then render it badly. `--allow-dolby-vision` if you accept
  losing it.
* Skips anything already HEVC or AV1 — re-encoding those is generation loss for
  almost no gain.
* Skips files modified in the last hour, which is the cheap way to avoid a
  still-copying download.
* Remembers what it has done, so a nightly run resumes rather than restarts.

It deliberately does NOT talk to Plex, Sonarr or Radarr. It swaps a file for a
smaller one with the same name; your library keeps working. Trigger a Plex scan
afterwards if you want the new bitrate reflected right away.
"""
from __future__ import annotations

import argparse
import json
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Iterable

VIDEO_SUFFIXES = {".mkv", ".mp4", ".m4v", ".avi", ".ts", ".mov", ".wmv", ".m2ts"}
EFFICIENT_CODECS = {"hevc", "h265", "av1"}
STATE_VERSION = 1

_stop = False


def _handle_signal(signum, _frame):
    global _stop
    _stop = True
    print(f"\n[signal {signum}] finishing the current file, then stopping.", flush=True)


@dataclass
class Candidate:
    path: Path
    size: int
    codec: str = ""
    title: str = ""

    @property
    def gb(self) -> float:
        return self.size / 1_000_000_000


@dataclass
class Stats:
    considered: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    encoded: int = 0
    failed: int = 0
    bytes_before: int = 0
    bytes_after: int = 0

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    @property
    def saved(self) -> int:
        return max(self.bytes_before - self.bytes_after, 0)


# ---------------------------------------------------------------------------
# ffprobe / ffmpeg
# ---------------------------------------------------------------------------

def probe(path: Path) -> dict[str, Any] | None:
    """Full stream and format info, or None if the file is unreadable."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=120, check=True,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def video_stream(info: dict[str, Any]) -> dict[str, Any]:
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "video":
            return stream
    return {}


def duration_of(info: dict[str, Any]) -> float:
    try:
        return float(info.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


def has_dolby_vision(info: dict[str, Any]) -> bool:
    """Dolby Vision shows up as a side-data block on the video stream."""
    stream = video_stream(info)
    for side in stream.get("side_data_list", []) or []:
        kind = str(side.get("side_data_type", "")).lower()
        if "dovi" in kind or "dolby vision" in kind:
            return True
    return False


def encoder_available(name: str) -> bool:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=30).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return any(line.split()[1:2] == [name] for line in out.splitlines() if line.strip())


def build_command(src: Path, dst: Path, args) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y"]

    if args.encoder == "hevc_qsv":
        cmd += ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"]
    elif args.encoder == "hevc_vaapi":
        cmd += ["-hwaccel", "vaapi", "-hwaccel_device", args.vaapi_device,
                "-hwaccel_output_format", "vaapi"]

    cmd += ["-i", str(src), "-map", "0"]

    if args.encoder == "hevc_qsv":
        cmd += ["-c:v", "hevc_qsv", "-preset", args.preset,
                "-global_quality", str(args.quality)]
    elif args.encoder == "hevc_vaapi":
        cmd += ["-c:v", "hevc_vaapi", "-qp", str(args.quality)]
    else:
        cmd += ["-c:v", "libx265", "-preset", args.preset, "-crf", str(args.quality)]

    # Audio and subtitles pass through untouched: this is about video bitrate,
    # and re-encoding TrueHD or Atmos to AAC is how people quietly ruin a library.
    cmd += ["-c:a", "copy", "-c:s", "copy", "-max_muxing_queue_size", "4096", str(dst)]
    return cmd


# ---------------------------------------------------------------------------
# Finding candidates
# ---------------------------------------------------------------------------

def walk_source(roots: list[Path], args) -> Iterable[Candidate]:
    for root in roots:
        if not root.exists():
            print(f"  ! source does not exist: {root}", file=sys.stderr)
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix.lower() not in VIDEO_SUFFIXES or not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size < args.min_gb * 1_000_000_000:
                continue
            yield Candidate(path=path, size=size, title=path.stem)


def from_plexcleaner(base: str, args) -> Iterable[Candidate]:
    """Use a PlexCleaner scan as the worklist.

    PlexCleaner stores Plex's view of each item, which does not include the
    file path, so this still needs --source to locate the file by name. It is
    most useful for its codec and size filters.
    """
    query = (f"{base.rstrip('/')}/api/media?verdict=all&codec=not-hevc"
             f"&min_gb={args.min_gb}&limit=2000&sort=size&dir=desc")
    try:
        with urllib.request.urlopen(query, timeout=30) as response:
            payload = json.load(response)
    except Exception as exc:                        # noqa: BLE001 - report and move on
        print(f"  ! could not read PlexCleaner at {base}: {exc}", file=sys.stderr)
        return

    names = {item["title"]: item for item in payload.get("items", [])}
    print(f"  PlexCleaner offered {len(names)} candidate(s) over {args.min_gb} GB")

    for root in args.source:
        for path in sorted(Path(root).rglob("*")):
            if path.suffix.lower() not in VIDEO_SUFFIXES or not path.is_file():
                continue
            match = next((n for n in names if n and n.lower() in path.stem.lower()), None)
            if not match:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            yield Candidate(path=path, size=size,
                            codec=(names[match].get("video_codec") or ""), title=match)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": STATE_VERSION, "done": {}, "failed": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": STATE_VERSION, "done": {}, "failed": {}}
    data.setdefault("done", {})
    data.setdefault("failed", {})
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        print(f"  ! could not write state file: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# The work
# ---------------------------------------------------------------------------

def within_window(until: dtime | None) -> bool:
    if until is None:
        return True
    now = datetime.now().time()
    # A window ending at 06:00 having started the previous evening means "stop
    # once we are past 06:00 but still in the morning".
    return not (until <= now < dtime(12, 0))


def verify(src_info: dict[str, Any], dst: Path, src_size: int, args) -> str:
    """Return an empty string if the output is good, else why it is not."""
    if not dst.exists() or dst.stat().st_size == 0:
        return "output missing or empty"

    dst_info = probe(dst)
    if dst_info is None:
        return "output does not probe cleanly"

    src_dur, dst_dur = duration_of(src_info), duration_of(dst_info)
    if src_dur and abs(src_dur - dst_dur) > args.duration_tolerance:
        return f"duration differs by {abs(src_dur - dst_dur):.1f}s (truncated?)"

    if len(dst_info.get("streams", [])) < len(src_info.get("streams", [])):
        return (f"lost streams: {len(src_info.get('streams', []))} in, "
                f"{len(dst_info.get('streams', []))} out")

    saved = src_size - dst.stat().st_size
    if saved <= 0:
        return "output is not smaller"
    if saved / src_size * 100 < args.min_saving_percent:
        return f"only {saved / src_size * 100:.1f}% smaller (floor is {args.min_saving_percent}%)"
    return ""


def process(cand: Candidate, args, state: dict[str, Any], stats: Stats) -> None:
    key = str(cand.path)
    stats.considered += 1

    if key in state["done"]:
        stats.skip("already done")
        return
    if state["failed"].get(key, {}).get("attempts", 0) >= args.max_attempts:
        stats.skip("failed too many times")
        return

    try:
        age = time.time() - cand.path.stat().st_mtime
    except OSError:
        stats.skip("unreadable")
        return
    if age < args.min_age_minutes * 60:
        stats.skip("modified too recently (still downloading?)")
        return

    info = probe(cand.path)
    if info is None:
        stats.skip("could not probe")
        return

    codec = str(video_stream(info).get("codec_name", "")).lower()
    if codec in EFFICIENT_CODECS:
        stats.skip(f"already {codec}")
        return
    if has_dolby_vision(info) and not args.allow_dolby_vision:
        stats.skip("Dolby Vision (use --allow-dolby-vision to accept losing it)")
        return

    dst = Path(args.scratch) / f"{cand.path.stem}.reencoded{cand.path.suffix}"
    cmd = build_command(cand.path, dst, args)

    print(f"\n  {cand.title or cand.path.name}")
    print(f"    {cand.gb:.1f} GB  {codec or 'unknown'}  ->  {args.encoder} q{args.quality}")

    if not args.apply:
        print(f"    DRY RUN: {' '.join(cmd)}")
        stats.skip("dry run")
        return

    Path(args.scratch).mkdir(parents=True, exist_ok=True)
    started = time.time()
    try:
        subprocess.run(cmd, check=True, timeout=args.timeout_hours * 3600)
    except subprocess.CalledProcessError as exc:
        print(f"    FAILED: ffmpeg exited {exc.returncode}")
        _record_failure(state, key, f"ffmpeg exit {exc.returncode}", args)
        stats.failed += 1
        dst.unlink(missing_ok=True)
        return
    except subprocess.TimeoutExpired:
        print(f"    FAILED: exceeded {args.timeout_hours}h")
        _record_failure(state, key, "timeout", args)
        stats.failed += 1
        dst.unlink(missing_ok=True)
        return
    except KeyboardInterrupt:
        dst.unlink(missing_ok=True)
        raise

    problem = verify(info, dst, cand.size, args)
    if problem:
        print(f"    REJECTED: {problem} — original untouched")
        _record_failure(state, key, problem, args)
        stats.failed += 1
        dst.unlink(missing_ok=True)
        return

    new_size = dst.stat().st_size
    elapsed = time.time() - started

    # Replace. Move the encode next to the original first so the swap is on one
    # filesystem, then remove or park the original.
    final = cand.path.with_suffix(dst.suffix)
    try:
        staged = cand.path.parent / f".{dst.name}.incoming"
        shutil.move(str(dst), str(staged))
        if args.keep_originals:
            cand.path.rename(cand.path.with_suffix(cand.path.suffix + ".original"))
        else:
            cand.path.unlink()
        staged.rename(final)
    except OSError as exc:
        print(f"    FAILED to swap files: {exc} — original untouched")
        _record_failure(state, key, f"swap failed: {exc}", args)
        stats.failed += 1
        return

    saved = cand.size - new_size
    print(f"    OK  {cand.gb:.1f} -> {new_size / 1e9:.1f} GB "
          f"(saved {saved / 1e9:.1f} GB, {saved / cand.size * 100:.0f}%) in {elapsed / 60:.0f} min")

    state["done"][key] = {"at": datetime.now().isoformat(timespec="seconds"),
                          "before": cand.size, "after": new_size}
    state["failed"].pop(key, None)
    stats.encoded += 1
    stats.bytes_before += cand.size
    stats.bytes_after += new_size


def _record_failure(state: dict[str, Any], key: str, reason: str, args) -> None:
    entry = state["failed"].setdefault(key, {"attempts": 0})
    entry["attempts"] += 1
    entry["reason"] = reason
    entry["at"] = datetime.now().isoformat(timespec="seconds")
    save_state(Path(args.state), state)


# ---------------------------------------------------------------------------

def parse_time(value: str) -> dtime:
    hour, _, minute = value.partition(":")
    return dtime(int(hour), int(minute or 0))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    src = p.add_argument_group("what to encode")
    src.add_argument("--source", action="append", default=[], metavar="DIR",
                     help="directory to walk; repeatable")
    src.add_argument("--plexcleaner", metavar="URL",
                     help="take the worklist from a PlexCleaner scan (still needs --source "
                          "to locate the files on disk)")
    src.add_argument("--min-gb", type=float, default=20,
                     help="only consider files at least this large (default: 20)")

    enc = p.add_argument_group("encoding")
    enc.add_argument("--encoder", default="hevc_qsv",
                     choices=["hevc_qsv", "hevc_vaapi", "libx265"],
                     help="hevc_qsv for Intel QuickSync (default), libx265 for CPU")
    enc.add_argument("--quality", type=int, default=24,
                     help="lower is better: QSV global_quality or x265 CRF (default: 24)")
    enc.add_argument("--preset", default="slow")
    enc.add_argument("--vaapi-device", default="/dev/dri/renderD128")
    enc.add_argument("--scratch", default="/var/tmp/reencode",
                     help="working directory; must be on a disk with room (default: /var/tmp/reencode)")

    safe = p.add_argument_group("safety")
    safe.add_argument("--apply", action="store_true",
                      help="actually encode and replace. Without this, nothing is written")
    safe.add_argument("--keep-originals", action="store_true",
                      help="rename the original to .original instead of deleting it")
    safe.add_argument("--allow-dolby-vision", action="store_true",
                      help="encode Dolby Vision titles anyway, losing the DV layer")
    safe.add_argument("--min-saving-percent", type=float, default=15,
                      help="reject an encode that saves less than this (default: 15)")
    safe.add_argument("--duration-tolerance", type=float, default=5.0,
                      help="seconds of duration drift to allow (default: 5)")
    safe.add_argument("--min-age-minutes", type=int, default=60,
                      help="skip files touched more recently than this (default: 60)")
    safe.add_argument("--max-attempts", type=int, default=2)

    run = p.add_argument_group("run limits")
    run.add_argument("--max-items", type=int, default=0, help="0 means no limit")
    run.add_argument("--until", type=parse_time, metavar="HH:MM",
                     help="stop starting new files after this time, e.g. 06:00")
    run.add_argument("--timeout-hours", type=float, default=8.0)
    run.add_argument("--state", default="~/.cache/reencode-state.json")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.state = str(Path(args.state).expanduser())
    args.source = [Path(s).expanduser() for s in args.source]

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if not args.source:
        print("Nothing to do: pass --source DIR at least once.", file=sys.stderr)
        return 2
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print("ffmpeg and ffprobe must be on PATH.", file=sys.stderr)
        return 2
    if args.apply and not encoder_available(args.encoder):
        print(f"ffmpeg has no {args.encoder} encoder. "
              f"Try --encoder libx265, or check your QuickSync setup.", file=sys.stderr)
        return 2

    mode = "APPLY — files will be replaced" if args.apply else "DRY RUN — nothing will be written"
    print(f"reencode.py  [{mode}]")
    print(f"  encoder {args.encoder} q{args.quality}, floor {args.min_gb} GB, "
          f"scratch {args.scratch}")
    if args.until:
        print(f"  will stop starting new files after {args.until.strftime('%H:%M')}")

    state = load_state(Path(args.state))
    stats = Stats()

    source = (from_plexcleaner(args.plexcleaner, args) if args.plexcleaner
              else walk_source(args.source, args))

    for cand in source:
        if _stop:
            print("  stopping as asked.")
            break
        if not within_window(args.until):
            print(f"  past {args.until.strftime('%H:%M')} — stopping for today.")
            break
        if args.max_items and stats.encoded >= args.max_items:
            print(f"  reached --max-items {args.max_items}.")
            break
        try:
            process(cand, args, state, stats)
        except KeyboardInterrupt:
            print("\n  interrupted; original left untouched.")
            break
        if args.apply:
            save_state(Path(args.state), state)

    save_state(Path(args.state), state)

    print(f"\nConsidered {stats.considered}, encoded {stats.encoded}, failed {stats.failed}.")
    for reason, count in sorted(stats.skipped.items(), key=lambda kv: -kv[1]):
        print(f"  skipped {count:>4}  {reason}")
    if stats.encoded:
        print(f"Reclaimed {stats.saved / 1e9:.1f} GB "
              f"({stats.bytes_before / 1e9:.1f} -> {stats.bytes_after / 1e9:.1f} GB).")
    elif not args.apply:
        print("Dry run: re-run with --apply once the list above looks right.")
    return 1 if stats.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
