#!/usr/bin/env python3
"""Find oversized items in Radarr or Sonarr and make them re-grab something smaller.

The alternative to re-encoding: instead of spending CPU, let the *arr replace a
bloated release with a leaner one. Costs bandwidth rather than hours.

    # What is actually big? Start here.
    ./regrab.py --radarr http://10.12.128.4:7878 --api-key KEY --min-gb 20

    # Which profiles could they move to?
    ./regrab.py --radarr http://10.12.128.4:7878 --api-key KEY --list-profiles

    # Move the biggest ten to a capped profile and search for replacements
    ./regrab.py --radarr http://10.12.128.4:7878 --api-key KEY --min-gb 20 \\
        --profile "HD-1080p" --search --max 10 --apply

Sonarr works the same way with --sonarr; sizes are per series.

How the replacement actually happens, because this trips people up:

Radarr does not delete a file for being large. It replaces a file when it grabs
something its quality profile ranks *higher*. So pointing a 90 GB 2160p remux at
a profile that tops out at 1080p is what makes Radarr consider the current file
unwanted and take the 1080p release. Changing the profile without searching does
nothing; searching without changing the profile finds nothing better.

That also means the size cap lives in the profile, not here. In Radarr, under
Settings -> Quality, each quality has a Max MB/min. Roughly 40 MB/min gives a
5 GB two-hour film. Set that first, or you may re-grab something just as large.

Nothing is written without --apply, and it is worth running on one title with
--max 1 before letting it loose.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class Service:
    kind: str           # radarr | sonarr
    base: str
    api_key: str

    @property
    def resource(self) -> str:
        return "movie" if self.kind == "radarr" else "series"

    @property
    def id_field(self) -> str:
        return "movieIds" if self.kind == "radarr" else "seriesIds"

    @property
    def search_command(self) -> str:
        return "MoviesSearch" if self.kind == "radarr" else "SeriesSearch"

    def request(self, method: str, path: str, payload: Any = None) -> Any:
        url = f"{self.base.rstrip('/')}/api/v3/{path.lstrip('/')}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "X-Api-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                body = response.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise SystemExit(f"{self.kind} {method} {path} -> HTTP {exc.code}: {detail}")
        except urllib.error.URLError as exc:
            raise SystemExit(f"Could not reach {self.kind} at {self.base}: {exc.reason}")


def size_of(item: dict[str, Any], kind: str) -> int:
    if kind == "radarr":
        return int(item.get("sizeOnDisk") or 0)
    return int((item.get("statistics") or {}).get("sizeOnDisk") or 0)


def quality_of(item: dict[str, Any]) -> str:
    """The current release quality, where the *arr reports one."""
    movie_file = item.get("movieFile") or {}
    quality = (movie_file.get("quality") or {}).get("quality") or {}
    return str(quality.get("name") or "")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--radarr", metavar="URL")
    target.add_argument("--sonarr", metavar="URL")
    p.add_argument("--api-key", required=True)

    p.add_argument("--min-gb", type=float, default=20,
                   help="only consider items at least this large (default: 20)")
    p.add_argument("--quality", default="",
                   help="only items whose current quality contains this, e.g. Remux")
    p.add_argument("--profile", default="",
                   help="move matching items to this quality profile by name")
    p.add_argument("--search", action="store_true",
                   help="trigger a search after any profile change")
    p.add_argument("--max", type=int, default=0, help="limit how many items to touch")
    p.add_argument("--list-profiles", action="store_true",
                   help="print the available quality profiles and exit")
    p.add_argument("--apply", action="store_true",
                   help="actually change profiles and search. Without this, nothing is written")
    args = p.parse_args(argv)

    svc = Service("radarr" if args.radarr else "sonarr",
                  args.radarr or args.sonarr, args.api_key)

    profiles = svc.request("GET", "qualityprofile")
    if args.list_profiles:
        print(f"{svc.kind} quality profiles:")
        for prof in profiles:
            cutoff = prof.get("cutoff")
            names = {q["quality"]["id"]: q["quality"]["name"]
                     for q in prof.get("items", []) if q.get("quality")}
            print(f"  [{prof['id']:>3}]  {prof['name']:<28} cutoff: {names.get(cutoff, cutoff)}")
        return 0

    target_profile = None
    if args.profile:
        target_profile = next((p_ for p_ in profiles
                               if p_["name"].lower() == args.profile.lower()), None)
        if target_profile is None:
            print(f"No profile named {args.profile!r}. Available: "
                  f"{', '.join(p_['name'] for p_ in profiles)}", file=sys.stderr)
            return 2

    items = svc.request("GET", svc.resource)
    floor = args.min_gb * 1_000_000_000
    matches = []
    for item in items:
        size = size_of(item, svc.kind)
        if size < floor:
            continue
        if args.quality and args.quality.lower() not in quality_of(item).lower():
            continue
        matches.append((size, item))
    matches.sort(key=lambda pair: -pair[0])

    if args.max:
        matches = matches[:args.max]

    if not matches:
        print(f"Nothing in {svc.kind} is over {args.min_gb} GB"
              f"{f' with quality matching {args.quality!r}' if args.quality else ''}.")
        return 0

    by_id = {p_["id"]: p_["name"] for p_ in profiles}
    total = sum(size for size, _ in matches)
    mode = "APPLY" if args.apply else "DRY RUN — nothing will be written"
    print(f"{svc.kind}: {len(matches)} item(s) over {args.min_gb} GB, "
          f"{total / 1e12:.2f} TB total   [{mode}]\n")

    for size, item in matches:
        current = by_id.get(item.get("qualityProfileId"), "?")
        print(f"  {size / 1e9:>7.1f} GB  {item.get('title', '?')[:48]:<48} "
              f"{quality_of(item) or '—':<16} profile: {current}")

    if not (target_profile or args.search):
        print("\nNothing asked for. Add --profile NAME and/or --search to act on these.")
        return 0

    print()
    if target_profile:
        print(f"Would move all of the above to profile {target_profile['name']!r} "
              f"(id {target_profile['id']}).")
    if args.search:
        print("Would then trigger a search for each.")

    if not args.apply:
        print("\nDry run. Re-run with --apply to do it — try --max 1 first.")
        return 0

    ids = [item["id"] for _, item in matches]

    if target_profile:
        # The editor endpoint changes many at once without having to PUT each
        # full object back, which risks clobbering fields you did not mean to.
        payload = {svc.id_field: ids, "qualityProfileId": target_profile["id"]}
        if svc.kind == "radarr":
            payload["moveFiles"] = False
        svc.request("PUT", f"{svc.resource}/editor", payload)
        print(f"Moved {len(ids)} item(s) to {target_profile['name']!r}.")

    if args.search:
        if svc.kind == "radarr":
            svc.request("POST", "command", {"name": "MoviesSearch", "movieIds": ids})
            print(f"Queued a search for {len(ids)} movie(s).")
        else:
            # Sonarr's SeriesSearch takes one series at a time.
            for series_id in ids:
                svc.request("POST", "command",
                            {"name": "SeriesSearch", "seriesId": series_id})
            print(f"Queued a search for {len(ids)} series.")

    print("\nWatch Activity -> Queue in the *arr. A file is only replaced when the "
          "new grab outranks the old one under its profile; if nothing is grabbed, "
          "check the profile's Max MB/min under Settings -> Quality.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
