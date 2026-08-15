#!/bin/bash
# macOS launcher. Double-click this file in Finder. SPEC §22.4.
#
# Finder runs a .command from the user's home directory, not from the file's own
# folder, so the first thing it does is cd to itself. Everything else is
# scripts/launch.py, which is shared with the Windows and Linux launchers.
#
# On the very first run macOS may refuse to open it — right-click -> Open, or
# `chmod +x start-passbook.command` in Terminal. Gatekeeper does not sign
# anything downloaded from GitHub, so this is expected and not a fault.

cd "$(dirname "$0")" || exit 1

PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    echo
    echo "  Python 3 is not installed."
    echo
    echo "  Install it from https://www.python.org/downloads/ and run this again."
    echo "  (macOS also ships one with the Xcode command line tools:"
    echo "   xcode-select --install)"
    echo
    read -r -p "  Press Enter to close." _
    exit 1
fi

"$PY" scripts/launch.py
status=$?

echo
read -r -p "Press Enter to close this window." _
exit $status
