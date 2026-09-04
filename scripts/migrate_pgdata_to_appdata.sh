#!/usr/bin/env bash
#
# Migrate iPedro's Postgres data out of the old named Docker volume
# (e.g. `docker_ipedro_pgdata`) into a host-path bind mount under appdata,
# so the Unraid "Appdata Backup" plugin can actually back it up.
#
# Named volumes live in /var/lib/docker/volumes/ and are invisible to the
# Appdata Backup plugin — which is why it logs:
#   'docker_ipedro_pgdata' does NOT exist! Please check your mappings!
#
# This script COPIES (never moves) the data, read-only at the source, so
# your old volume stays intact for rollback until you choose to delete it.
#
# Usage:
#   scripts/migrate_pgdata_to_appdata.sh [old_volume_name]
#
# If old_volume_name is omitted, it auto-detects a *ipedro_pgdata volume.
# Override the destination with PGDATA_HOST_PATH (must match docker-compose).

set -euo pipefail

DEST="${PGDATA_HOST_PATH:-/mnt/user/appdata/ipedro/pgdata}"
OLD_VOLUME="${1:-}"

if [[ -z "$OLD_VOLUME" ]]; then
  OLD_VOLUME="$(docker volume ls --format '{{.Name}}' \
    | grep -E 'ipedro_pgdata$' | head -n1 || true)"
fi

if [[ -z "$OLD_VOLUME" ]]; then
  echo "No existing *ipedro_pgdata Docker volume found." >&2
  echo "Pass it explicitly:  $0 <old_volume_name>" >&2
  echo "List candidates:     docker volume ls | grep pgdata" >&2
  exit 1
fi

if ! docker volume inspect "$OLD_VOLUME" >/dev/null 2>&1; then
  echo "Volume '$OLD_VOLUME' does not exist." >&2
  exit 1
fi

echo "Source volume : $OLD_VOLUME"
echo "Destination   : $DEST"
echo
echo "This will: stop the compose stack, then copy the data to the host path."
read -r -p "Proceed? [y/N] " ans
[[ "$ans" == "y" || "$ans" == "Y" ]] || { echo "Aborted."; exit 1; }

# Stop anything using the volume. Best-effort: works when run from the
# compose directory; harmless otherwise.
docker compose down 2>/dev/null || true

if [[ -e "$DEST" && -n "$(ls -A "$DEST" 2>/dev/null || true)" ]]; then
  echo "Destination '$DEST' already exists and is non-empty." >&2
  echo "Refusing to overwrite. Move it aside or pick a fresh PGDATA_HOST_PATH." >&2
  exit 1
fi

mkdir -p "$DEST"

# Alpine one-shot: cp -a preserves ownership/permissions Postgres needs.
docker run --rm \
  -v "$OLD_VOLUME":/from:ro \
  -v "$DEST":/to \
  alpine sh -c 'cp -a /from/. /to/ && echo "Copied $(du -sh /to | cut -f1) to /to"'

echo
echo "Migration copy complete."
echo "Next:"
echo "  1. Bring the stack up with the updated compose:  docker compose up -d"
echo "  2. Confirm the bot connects and your data is present (ducks, quotes, memory)."
echo "  3. Point the Appdata Backup plugin at:  $DEST"
echo "  4. ONLY after verifying, reclaim the old volume:  docker volume rm $OLD_VOLUME"
