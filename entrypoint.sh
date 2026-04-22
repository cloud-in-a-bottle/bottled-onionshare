#!/bin/bash
# OpenHost OnionShare entrypoint.
#
# Unlike the openhost-hidden-service app, we do NOT run a long-lived
# torrc here. Each share creates its own onion via onionshare-cli, which
# in turn spawns and controls a tor process.  Our job is just to make
# sure the data/state directories exist and then hand off to server.py.

set -euo pipefail

APP_DATA="${OPENHOST_APP_DATA_DIR:-/data/app_data/onionshare}"
APP_TEMP="${OPENHOST_APP_TEMP_DIR:-/data/app_temp_data/onionshare}"

# Persistent: uploaded files, persistent share settings (so .onion
# addresses survive restarts for shares the user marks persistent),
# and any ad-hoc state we write.
SHARES_DIR="$APP_DATA/shares"              # uploaded files live here
PERSIST_DIR="$APP_DATA/persistent_sessions" # onionshare --persistent files
RECEIVE_DIR="$APP_DATA/received"           # files uploaded TO us by recipients

# Ephemeral: per-process scratch used by onionshare/tor. Not backed up.
RUN_DIR="$APP_TEMP/run"

mkdir -p "$SHARES_DIR" "$PERSIST_DIR" "$RECEIVE_DIR" "$RUN_DIR"

echo "[entrypoint] APP_DATA=$APP_DATA"
echo "[entrypoint] APP_TEMP=$APP_TEMP"
echo "[entrypoint] Starting admin server on :8080"

exec python3 /app/server.py
