# PlexCleaner

Cross-references **Plex**, **Tautulli**, **Sonarr**, **Radarr** and **Seerr**
(Overseerr/Jellyseerr) to find TV shows, movies and users that nobody has
touched in a long time — then removes them from every service in one
coordinated pass.

Runs as a self-contained container with a web portal. There is no config file
to edit, no shell access needed, and no `.env` to maintain: start it, open it in
a browser, and a setup wizard walks you through connecting each service.

Nothing is ever deleted without you selecting it, reviewing the exact list of
service calls, and confirming.

![dry run](https://img.shields.io/badge/default-dry%20run-3dd68c)
![arch](https://img.shields.io/badge/arch-amd64%20%7C%20arm64-blue)
![python](https://img.shields.io/badge/python-3.11%2B-blue)

---

## Install

> **First time: pick where the image comes from.**
> This repository is **private**, so the image CI publishes to
> `ghcr.io/rudeboye2k/plexcleaning` is private too, and a NAS pulling it will
> fail with `unauthorized`. Choose one:
>
> - **Make the package public** (easiest). After the first CI run, go to your
>   GitHub profile → **Packages** → `plexcleaning` → **Package settings** →
>   *Change visibility* → **Public**. The image contains no secrets — all
>   configuration lives in your `/data` volume. Then the compose files work
>   as-is.
> - **Authenticate on the host.** Create a classic PAT with `read:packages` and
>   run `docker login ghcr.io -u your-github-username`. On Synology, do this
>   over SSH before creating the project.
> - **Build it yourself** and skip the registry entirely — see
>   [From source](#from-source) below. This is the simplest route if you would
>   rather not publish an image at all.

### Synology Container Manager

1. In **File Station**, create the folder `docker/plexcleaner/data`.
2. Over SSH, run `id your-dsm-username` and note the `uid` and `gid`. DSM's
   first user is usually `1026`, and the `users` group is `100`.
3. **Container Manager → Project → Create**
   - Project name: `plexcleaner`
   - Path: the folder from step 1
   - Source: *Create docker-compose.yml*
   - Paste the contents of [`docker-compose.synology.yml`](docker-compose.synology.yml)
   - Change `PUID`/`PGID` to the values from step 2, and set
     `PLEXCLEANER_ALLOWED_NETWORKS` to your LAN subnet
4. Build and start it, then open `http://your-nas:8585`.

`PUID`/`PGID` are the step people get wrong. If the container cannot write its
database, the log will say so on the first line — check that they match the
owner of the folder you created.

### Portainer

**Stacks → Add stack → Web editor**, paste
[`docker-compose.yml`](docker-compose.yml), deploy. Environment variables can go
in the editor or in Portainer's own environment table — either works.

### Plain Docker

```bash
docker run -d --name plexcleaner \
  -p 8585:8585 \
  -v plexcleaner-data:/data \
  -e PUID=1000 -e PGID=1000 \
  -e PLEXCLEANER_ALLOWED_NETWORKS=192.168.1.0/24 \
  --restart unless-stopped \
  ghcr.io/rudeboye2k/plexcleaning:latest
```

### From source

Builds locally, so no registry login is needed:

```bash
git clone https://github.com/rudeboye2k/PlexCleaning.git
cd PlexCleaning
docker compose -f docker-compose.build.yml up -d --build
```

On Synology you can do the same from a Container Manager project: upload the
repository to a shared folder, then point the project at
`docker-compose.build.yml` instead.

Or without Docker:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .
.venv/bin/plexcleaner serve
```

Then open `http://your-host:8585` and the wizard takes over.

---

## What you will need

The wizard asks for these, and tests each one as you enter it. Everything is
optional except Plex or Tautulli.

| Service | Where the key lives |
|---|---|
| **Plex** | Open any library item in Plex Web → **Get Info** → **View XML**, then copy `X-Plex-Token` from the address bar |
| **Tautulli** | Settings → Web Interface → API |
| **Sonarr** | Settings → General → API Key |
| **Radarr** | Settings → General → API Key |
| **Seerr** | Settings → General → API Key |

**Connect Tautulli.** Plex's own `lastViewedAt` only reflects the *server
owner's* account, so a show your friends watch every week looks untouched in
Plex. Tautulli sees every account, which is why it is treated as the authority
on watch state. Without it, the candidate list will be wrong in the most
dangerous direction.

---

## Configuring it

Everything lives on the **Settings** page and applies immediately — no restart,
no container rebuild. Saved settings are stored in the database on your `/data`
volume, so they survive image updates.

Configuration comes from four layers, lowest precedence first:

| Layer | Purpose |
|---|---|
| Built-in defaults | Sensible values for a home server |
| `config.yaml` *(optional)* | For people who prefer files or Git |
| Environment variables | How Container Manager and Portainer inject values |
| **Web UI** | What you edit in the portal — **wins** |

The web UI winning is deliberate: what you save is what applies. Environment
variables still matter — they seed a brand-new install so a container can come
up already configured, and the Settings page puts an `ENV` badge on any field
whose environment value is being shadowed by a saved one, with a one-click reset
to fall back.

Every field shows where its value came from: `ENV`, `FILE`, `SAVED`, or nothing
for a default.

### Useful environment variables

All optional — the wizard can collect everything instead.

| Variable | Meaning |
|---|---|
| `PUID` / `PGID` / `UMASK` | Match the owner of your `/data` volume |
| `PLEXCLEANER_ALLOWED_NETWORKS` | Comma-separated CIDRs allowed to reach the UI |
| `PLEXCLEANER_PASSWORD` | Web UI password |
| `PLEXCLEANER_TRUST_PROXY` | Set when a reverse proxy you control sits in front |
| `PLEXCLEANER_LOCK_SAFETY` | Stops the web UI from ever leaving dry-run mode |
| `PLEX_URL` / `PLEX_TOKEN` | Pre-seed Plex; same pattern for `TAUTULLI_`, `SONARR_`, `RADARR_`, `SEERR_` |
| `SONARR_2_URL` / `SONARR_2_API_KEY` | A second instance, e.g. a 4K setup (up to 4) |

A service switches itself on as soon as it has both a URL and a key, so two
variables per service is enough.

---

## Safety model

This tool deletes irreplaceable data, so the defaults are deliberately timid.

- **Dry run is on by default.** Every action is simulated and written to the
  audit log. Turning it off requires typing the confirmation phrase, and if
  `PLEXCLEANER_LOCK_SAFETY=true` is set, the portal cannot turn it off at all —
  useful when other people can reach the UI.
- **Two phases.** A scan only builds candidate lists. A plan turns your
  selection into an explicit list of service calls you review before executing.
- **Typed confirmation** before any live execution.
- **Per-run caps** on item count, user count and total gigabytes. A plan that
  exceeds them is refused, not truncated.
- **Snapshots.** A JSON record of every item is written to
  `/data/backups/plan-<id>/` before deletion, so you can re-add it by hand.
- **Permanent keep list.** "Never suggest again" survives every future scan.
- **Full audit log** of every call, simulated or real.
- **Protections** for Plex labels, Plex collections, \*arr tags, still-airing
  monitored series, partially-watched items, recently requested titles, admins,
  and Plex Home users.

### Keeping it internal

Three independent layers, all on by default:

1. **Network guard** — every request's source address is checked against
   `allowed_networks`. Anything outside gets a `403`, even if a port-forward or
   reverse proxy is misconfigured later. `/healthz` is the only exception, so
   container health checks work.
2. **Password + CSRF** — set a password for a login prompt. State-changing
   requests require a matching CSRF token whether or not a password is set.
3. **Bind address** — you can pin the published port to one interface
   (`"10.12.128.4:8585:8585"`) instead of all of them.

Behind Synology's reverse proxy, Nginx Proxy Manager or Traefik, enable
**Trust reverse proxy headers** — otherwise the network guard only ever sees the
proxy's own address. Only enable it when the proxy is yours; otherwise a forged
`X-Forwarded-For` header defeats the guard.

Do not expose this publicly. It holds API keys for five services and can delete
your entire library.

---

## Using it

1. **Dashboard → Test connections.** Fix anything red first.
2. **Run scan.** Reads every service and builds the candidate lists. Nothing is
   modified. A few minutes for a large library.
3. **Media tab.** Sort by size, filter by library, and read the *Why* column —
   every verdict explains itself. Use **Never suggest again** for anything you
   want permanently exempt.
4. **Build deletion plan.** Shows the exact service calls, in order.
5. **Run simulation.** Confirms every call would succeed, changes nothing.
6. When you trust it: turn off dry run in Settings, return to a plan, type the
   confirmation phrase, execute.

### Suggested first run

Start conservative and tighten later:

- Unwatched threshold: **730** days
- Never-watched grace: **365** days
- Minimum age: **90** days
- Max media per run: **5**

Leave it in dry run for a couple of weeks and read the candidate list. When it
stops surprising you, lower the thresholds.

---

## How the cross-referencing works

**Finding stale media.** Plex provides the library inventory (titles, GUIDs,
file sizes, labels, collections). Tautulli provides who watched what and when.
Sonarr and Radarr provide on-disk size, tags and whether a series is still
airing. Seerr provides who requested a title and when.

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

For users: Seerr account deleted → Plex access revoked → Tautulli entry removed,
each step individually toggleable. The Plex action defaults to **unshare**,
which revokes library access but keeps the friendship and can be undone;
**remove friend** cannot.

If a step fails, the remaining steps for that item are skipped rather than
pressed on with — a half-deleted title is worse than an untouched one.

---

## CLI

Optional. The portal can do all of this; the CLI exists for cron and scripting.

```bash
plexcleaner config                      # show the effective configuration
plexcleaner test                        # connectivity check
plexcleaner scan                        # refresh candidate lists
plexcleaner report --kind movie         # list stale movies
plexcleaner report --users              # list inactive users
plexcleaner plan --top 10               # plan from the 10 largest candidates
plexcleaner apply 3                     # simulate plan #3
plexcleaner apply 3 --live --confirm DELETE
plexcleaner reset-settings              # discard UI settings, fall back to env
```

Inside a container: `docker exec plexcleaner plexcleaner config`.

`plexcleaner config` is the fastest way to see what a container actually thinks
its settings are, including which environment variables are being shadowed.

---

## Updating

```bash
docker compose pull && docker compose up -d
```

Settings and history live in the `/data` volume, so they survive the update.
In Container Manager, use **Project → Action → Build** after pulling.

---

## Development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pip install -e .
.venv/bin/python -m pytest -q
```

163 tests cover the rules engine, cross-service matching, plan building,
execution safety rails, the settings store's layering and persistence, an
end-to-end scan against mocked HTTP for all five services, and the network,
auth and CSRF guards.

```
plexcleaner/
├── config.py          dataclasses, validation, redaction
├── settings_store.py  four-layer config with live reload
├── schema.py          field definitions that drive the whole portal UI
├── db.py              SQLite schema, audit log, protections
├── models.py          MediaItem / UserAccount / Step
├── match.py           cross-service identity matching
├── scan.py            inventory + merge + evaluate
├── rules.py           candidate / keep / protected verdicts
├── actions.py         plan building and execution
├── clients/           one module per service
└── web/               FastAPI app, templates, static assets
```

Adding a setting means adding a field to the dataclass in `config.py` and an
entry in `schema.py` — the portal form, help text, validation and provenance
badges all follow from that.

---

## Troubleshooting

**`Error response from daemon: Head "https://ghcr.io/v2/.../manifests/latest"`
— unauthorized / denied.** The image was built and published, but this
repository is private, so its GHCR package is private too and an
unauthenticated pull is rejected. Make the package public at
`https://github.com/users/<you>/packages/container/plexcleaning/settings` →
*Change visibility* → **Public** (the image holds no secrets; your repository
stays private either way), or `docker login ghcr.io` on the host with a
`read:packages` token, or build locally with `docker-compose.build.yml`.

**Container will not start / cannot write the database.** `PUID`/`PGID` do not
match the owner of your data folder. Run `id your-username` on the NAS and set
them accordingly. The entrypoint logs a warning naming the directory it could
not take ownership of.

**403 when opening the web UI.** Your client's IP is outside
`PLEXCLEANER_ALLOWED_NETWORKS`. The 403 page names the address it saw, so add
that subnet. Behind a reverse proxy, enable *Trust reverse proxy headers*.

**Everything shows as never watched.** Tautulli is not connected. Plex alone
only knows about the owner's playback.

**Changing an environment variable does nothing.** A saved setting is shadowing
it. The Settings page lists which ones and offers a reset.

**Plex deletion returns 401.** Enable *Allow media deletion* in Plex → Settings
→ Library. Not needed if Sonarr/Radarr manage every title.

**A title reappears after deletion.** *Add import exclusion* was off, and an
import list re-added it.

**Items show 0 GB.** Plex has not scanned the file sizes; the \*arr value is
used as a fallback, so check the item is matched to Sonarr/Radarr (the Services
column).

---

## License

MIT
