#!/usr/bin/env bash

set -Eeuo pipefail

APP_DIR="${APP_DIR:-$HOME/apps/kevin}"
BRANCH="${BRANCH:-main}"
REPOSITORY="${REPOSITORY:-https://github.com/1337lean/kevin.git}"
REVISION="${REVISION:-$BRANCH}"
SERVICE_NAME="${SERVICE_NAME:-kevin.service}"

for command_name in git python3 systemctl; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        printf 'Required command is missing: %s\n' "$command_name" >&2
        exit 1
    fi
done

mkdir -p "$APP_DIR"
cd "$APP_DIR"

if [[ ! -f .env ]]; then
    printf 'Deployment refused: %s/.env does not exist.\n' "$APP_DIR" >&2
    exit 1
fi

if [[ ! -d .git ]]; then
    git init
    git remote add origin "$REPOSITORY"
else
    git remote set-url origin "$REPOSITORY"
fi

git fetch --force --prune origin "$BRANCH"
target_revision="$(git rev-parse "${REVISION}^{commit}")"

if [[ "$REVISION" != "$BRANCH" && "$target_revision" != "$REVISION" ]]; then
    printf 'Fetched commit %s does not match requested commit %s.\n' \
        "$target_revision" "$REVISION" >&2
    exit 1
fi

# Force only repository-tracked paths to the tested revision. Persistent paths such
# as .env, data/, bin/, vendor/, and .venv/ are ignored by Git and remain untouched.
git checkout --force -B "$BRANCH" "$target_revision"

mkdir -p data/backups
if [[ -f data/kevin.sqlite3 ]]; then
    backup_path="data/backups/kevin-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
    SOURCE_DATABASE="data/kevin.sqlite3" BACKUP_DATABASE="$backup_path" python3 <<'PY'
import os
import sqlite3

source = sqlite3.connect(os.environ["SOURCE_DATABASE"])
backup = sqlite3.connect(os.environ["BACKUP_DATABASE"])
try:
    source.backup(backup)
finally:
    backup.close()
    source.close()
PY

    mapfile -t old_backups < <(find data/backups -maxdepth 1 -type f \
        -name 'kevin-*.sqlite3' -printf '%T@ %p\n' | sort -rn | tail -n +11 | cut -d' ' -f2-)
    if (( ${#old_backups[@]} )); then
        rm -- "${old_backups[@]}"
    fi
fi

if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
fi

.venv/bin/python -m pip install --disable-pip-version-check --upgrade .
.venv/bin/python -m compileall -q kevin

install -d -m 700 "$HOME/.config/systemd/user"
install -m 644 deploy/kevin.service "$HOME/.config/systemd/user/kevin.service"
systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
systemctl --user restart "$SERVICE_NAME"

for _ in {1..10}; do
    if systemctl --user is-active --quiet "$SERVICE_NAME"; then
        printf 'Kevin deployed at commit %s and %s is active.\n' \
            "$target_revision" "$SERVICE_NAME"
        exit 0
    fi
    sleep 1
done

systemctl --user status "$SERVICE_NAME" --no-pager >&2 || true
journalctl --user -u "$SERVICE_NAME" -n 40 --no-pager >&2 || true
exit 1
