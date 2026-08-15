#!/bin/bash
# Linux launcher. SPEC §22.4.
#
#   ./start-passbook.sh
#
# Most Linux desktops will run this on double-click once it is executable and
# the file manager is set to ask; from a terminal it always works. Everything
# else is scripts/launch.py, shared with the macOS and Windows launchers.

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
    echo "    Debian/Ubuntu   sudo apt install python3"
    echo "    Fedora          sudo dnf install python3"
    echo "    Arch            sudo pacman -S python"
    echo
    exit 1
fi

exec "$PY" scripts/launch.py
