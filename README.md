# PlexCleaner

Cross-references **Plex**, **Tautulli**, **Sonarr**, **Radarr** and **Seerr**
(Overseerr/Jellyseerr) to find TV shows, movies and users that nobody has
touched in a long time — then removes them from every service in one
coordinated pass.

Runs as an internal-only web app on your server. Nothing is ever deleted
without you selecting it, reviewing the exact list of service calls, and
confirming.

![dry run badge](https://img.shields.io/badge/default-dry%20run-3dd68c) ![python](https://img.shields.io/badge/python-3.11%2B-blue)

---

## What it actually does

**Finding stale media.** Plex provides the library inventory (titles, GUIDs,
file sizes, labels, collections). Tautulli provides the truth about who watched
what and when — this matters, because Plex's own `lastViewedAt` only reflects
the *owner's* account, so a show your friends watch weekly can look untouched.
Sonarr and Radarr supply on-disk size, tags and whether a series is still
airing. Seerr supplies who requested a title and when.

Items are matched across services by TMDB / TVDB / IMDb id first, then Plex
rating key, then a normalised title+year — and an ambiguous title match is
*refused* rather than guessed, so "The Thing (1982)" can never delete the wrong
one.

**Finding stale users.** Plex says who has access, Tautulli says when they last
watched, Seerr says when they last logged in. Someone who requests a lot but
streams through another client still counts as active.

**Removing things.** Deletion order matters and is handled for you:

| Step | Service | Why it is in this position |
|---|---|---|
| 1 | Sonarr / Radarr | Owns the files on disk. Deletes them and adds an import-list exclusion so automation cannot re-add the title. |
| 2 | Seerr | Clears the media entry so the title shows as requestable again. |
| 3 | Plex | Only *refreshes* the library if an \*arr already deleted the files. Deletes directly when no \*arr owned the item. |
| 4 | Tautulli | Optional history purge. |

For users: Seerr account deleted → Plex access revoked → Tautulli entry removed
(each step individually toggleable).

If a step fails, the remaining steps for that item are skipped rather than
pressed on with — a half-deleted title is worse than an untouched one.

---

## Safety model

This tool deletes irreplaceable data, so the defaults are deliberately timid.

- **Dry run is on by default.** Every action is simulated and written to the
  audit log. You must edit `safety.dry_run: false` in the config file *and*
  restart before anything real happens. The web UI cannot flip this — a
  compromised browser session cannot start deleting.
- **Two phases.** A scan only builds candidate lists. A plan turns your
  selection into an explicit list of service calls that you review before
  executing.
- **Typed confirmation.** Live execution requires typing the exact
  `safety.confirm_phrase`.
- **Per-run caps** on item count, user count and total gigabytes. A plan that
  exceeds them is refused, not truncated.
- **Snapshots.** A JSON record of every item is written to
  `data/backups/plan-<id>/` before deletion, so you can re-add it by hand.
- **Permanent keep list.** "Never suggest again" survives every future scan.
- **Full audit log** of every call, simulated or real.
- **Protections** for: Plex labels, Plex collections, \*arr tags, still-airing
  monitored series, partially-watched items, recently requested titles, admins,
  and Plex Home users.

---

## Requirements

- Python 3.11+ (or Docker)
- Plex Media Server with **Settings → Library → Allow media deletion** enabled
  *only if* you want Plex to delete items it owns directly. If every title is
  managed by Sonarr/Radarr, you can leave this off.
- Tautulli, Sonarr, Radarr, Seerr — all optional, but Plex or Tautulli must be
  enabled so watch state can be determined.

---

## Setup

```bash
git clone https://github.com/rudeboye2k/PlexCleaning.git
cd PlexCleaning

cp config/config.example.yaml config/config.yaml
cp .env.example .env
```

Put your secrets in `.env` (it is gitignored); `config.yaml` refers to them as
`${VAR}` so no keys are ever committed:

```bash
openssl rand -hex 32          # use for PLEXCLEANER_SECRET_KEY
```

| Variable | Where to find it |
|---|---|
| `PLEX_TOKEN` | Any Plex web player → library item → **Get Info** → **View XML** → copy `X-Plex-Token` from the URL |
| `TAUTULLI_API_KEY` | Tautulli → Settings → Web Interface → API |
| `SONARR_API_KEY` | Sonarr → Settings → General |
| `RADARR_API_KEY` | Radarr → Settings → General |
| `SEERR_API_KEY` | Seerr → Settings → General |

Then edit `config/config.yaml` — at minimum set the service URLs, and set
`app.host` to your internal IP (`10.12.128.4`).

### Run with Docker (recommended)

```bash
docker compose up -d --build
```

The compose file publishes on `10.12.128.4:8585` only. That IP prefix matters:
without it Docker binds `0.0.0.0` and writes an iptables rule that bypasses a
host firewall.

### Run directly

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .

plexcleaner test          # verify every service is reachable
plexcleaner serve
```

A systemd unit:

```ini
[Unit]
Description=PlexCleaner
After=network-online.target

[Service]
User=plex
WorkingDirectory=/opt/PlexCleaning
Environment=PLEXCLEANER_CONFIG=/opt/PlexCleaning/config/config.yaml
ExecStart=/opt/PlexCleaning/.venv/bin/plexcleaner serve
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

---

## Keeping it internal

Three independent layers, all on by default:

1. **Bind address** — `app.host: "10.12.128.4"` means the socket only listens
   on your LAN interface. Nothing on a WAN interface can reach it.
2. **Network guard** — every request's source address is checked against
   `app.allowed_networks`. Anything outside gets a `403`, even if a reverse
   proxy or port-forward is misconfigured later. `/healthz` is the only
   exception, so container health checks work.
3. **Password + CSRF** — set `PLEXCLEANER_PASSWORD` for a login prompt.
   State-changing requests additionally require a matching CSRF token.

Do not put this behind a public reverse proxy or Cloudflare tunnel. It holds
API keys for five services and can delete your entire library.

---

## Using it

1. **Dashboard → Test connections.** Fix anything red before going further.
2. **Run scan.** Reads every service and builds the candidate lists. Nothing is
   modified. A few minutes for a large library.
3. **Media tab.** Sort by size, filter by library, read the *Why* column — every
   verdict explains itself. Use **Never suggest again** on anything you want
   permanently exempt.
4. **Build deletion plan.** Shows the exact service calls, in order.
5. **Run simulation.** Confirms every call would succeed, changes nothing.
6. When you trust it: set `safety.dry_run: false`, restart, return to a plan,
   type the confirm phrase, execute.

### Suggested first run

Start conservative and tighten later:

```yaml
rules:
  media:
    unwatched_days: 730          # two years
    never_watched_after_days: 365
    min_age_days: 90
safety:
  max_media_deletions_per_run: 5
```

Run in dry-run for a couple of weeks and read the candidate list. When it stops
surprising you, lower the thresholds.

---

## CLI

Useful for cron and for verifying a config before trusting the UI with it.

```bash
plexcleaner test                        # connectivity check
plexcleaner scan                        # refresh candidate lists
plexcleaner report --kind movie         # list stale movies
plexcleaner report --users              # list inactive users
plexcleaner plan --top 10               # plan from the 10 largest candidates
plexcleaner apply 3                     # simulate plan #3
plexcleaner apply 3 --live --confirm DELETE
plexcleaner prune --days 365            # trim audit log and old scans
```

A nightly scan (actions still stay manual):

```cron
30 4 * * * /opt/PlexCleaning/.venv/bin/plexcleaner scan >> /var/log/plexcleaner.log 2>&1
```

Or leave `schedule.scan_enabled: true` and the server does it internally.

---

## Configuration reference

Every option is documented inline in
[`config/config.example.yaml`](config/config.example.yaml). The ones worth
understanding before your first live run:

| Key | Meaning |
|---|---|
| `safety.dry_run` | Master switch. `true` means nothing is ever deleted. |
| `rules.media.unwatched_days` | Days since anyone last watched before an item is a candidate. |
| `rules.media.never_watched_after_days` | Grace period for items nobody has ever watched, measured from when they were added. |
| `rules.media.min_age_days` | Absolute floor — nothing newer is ever proposed. |
| `rules.media.protect_continuing_series` | Keep shows Sonarr still lists as airing and monitored. |
| `rules.media.protect_in_progress` | Keep anything someone is partway through. |
| `rules.media.protect_recent_seerr_requests_days` | Keep recently requested titles. |
| `rules.users.inactive_days` | Days with no playback *and* no Seerr login. |
| `rules.users.plex_action` | `unshare` (reversible), `remove_friend`, or `none`. |
| `sonarr[].delete_files` | Whether removing a series also deletes files. `false` unmonitors only. |
| `sonarr[].add_import_exclusion` | Stop automation re-adding what you just removed. |

Multiple Sonarr/Radarr instances (e.g. an HD and a 4K instance) are supported —
add another entry to the list with a unique `name`.

---

## Development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pip install -e .
.venv/bin/python -m pytest -q
```

105 tests cover the rules engine, cross-service matching, plan building,
execution safety rails, an end-to-end scan against mocked HTTP for all five
services, and the network/auth guards.

```
plexcleaner/
├── config.py         YAML + ${ENV} config, validation
├── db.py             SQLite schema, audit log, protections
├── models.py         MediaItem / UserAccount / Step
├── match.py          cross-service identity matching
├── scan.py           inventory + merge + evaluate
├── rules.py          candidate / keep / protected verdicts
├── actions.py        plan building and execution
├── clients/          one module per service
└── web/              FastAPI app, templates, static assets
```

---

## Troubleshooting

**Everything shows as never watched.** Tautulli is not enabled or not
reachable. Plex alone only knows about the owner's playback.

**Plex deletion returns 401.** Enable *Allow media deletion* in Plex → Settings
→ Library.

**A title reappears after deletion.** `add_import_exclusion` was off, and an
import list re-added it.

**Items show 0 GB.** Plex has not scanned the file sizes; the \*arr value is
used as a fallback, so check the item is matched to Sonarr/Radarr (the Services
column).

**403 from the web UI.** Your client IP is outside `app.allowed_networks`.

---

## License

MIT
