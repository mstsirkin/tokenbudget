#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if ! python3 -c 'import PySide6' >/dev/null 2>&1; then
    echo "PySide6 is not installed. On Fedora run: dnf install -y python3-pyside6" >&2
    exit 1
fi

export PYTHONPATH="${PYTHONPATH:-}"

exec python3 "$repo_root/desktop/tokenbudget_qt.py" "$@"
