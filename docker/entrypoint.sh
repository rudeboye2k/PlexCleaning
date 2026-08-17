#!/bin/sh
# Container entrypoint.
#
# Synology and unRAID bind mounts are owned by whatever UID/GID the NAS decided
# on, which is rarely 1000. Rather than making people chown their shares, this
# adopts the IDs given in PUID/PGID, fixes ownership of the writable volumes,
# then drops privileges.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
UMASK="${UMASK:-022}"
DATA_DIR="${PLEXCLEANER_DATA_DIR:-/data}"

umask "$UMASK"

log() { echo "[entrypoint] $*"; }

if [ "$(id -u)" = "0" ]; then
    # Re-point the app user at the requested IDs. Both may already be taken by
    # a system account, which is fine — we only need the numeric IDs to match.
    if [ "$(id -g plexcleaner 2>/dev/null)" != "$PGID" ]; then
        groupmod -o -g "$PGID" plexcleaner 2>/dev/null || true
    fi
    if [ "$(id -u plexcleaner 2>/dev/null)" != "$PUID" ]; then
        usermod -o -u "$PUID" plexcleaner 2>/dev/null || true
    fi

    mkdir -p "$DATA_DIR" /config

    # Only chown when it is actually wrong: on a large data directory with a
    # slow NAS filesystem, a blind recursive chown on every boot is painful.
    for dir in "$DATA_DIR" /config; do
        owner="$(stat -c '%u:%g' "$dir" 2>/dev/null || echo '')"
        if [ "$owner" != "$PUID:$PGID" ]; then
            log "taking ownership of $dir as $PUID:$PGID"
            chown -R "$PUID:$PGID" "$dir" 2>/dev/null || \
                log "WARNING: could not chown $dir — check the share permissions on your NAS"
        fi
    done

    log "starting as UID $PUID / GID $PGID (umask $UMASK)"
    exec gosu "$PUID:$PGID" plexcleaner "$@"
fi

# Already unprivileged (e.g. compose set `user:`), so just run.
log "starting as $(id -u):$(id -g)"
exec plexcleaner "$@"
