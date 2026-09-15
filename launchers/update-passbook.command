#!/usr/bin/env bash
# Update passbook. Double-click on macOS (see update-passbook.command) or run
# this from a terminal on Linux.
#
# The app itself cannot do this: applying an update rebuilds a container image,
# which needs the Docker socket, and the one process that listens on a port and
# parses uploaded files is the last place that socket belongs. So the Status
# page tells you an update exists and this runs it.
set -euo pipefail
cd "$(dirname "$0")/.."

echo
echo "  Updating passbook. This backs up first, then pulls, rebuilds and"
echo "  checks the ledger."
echo
if make update; then
    echo
    echo "  Done. Open http://localhost:8081"
else
    echo
    echo "  Update stopped. Nothing was half-applied — the version you had is"
    echo "  still running, and your backup is in backups/."
    exit 1
fi
