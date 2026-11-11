#!/usr/bin/env bash
set -euo pipefail
cd /src
PY="wine /opt/winpython/python.exe"
$PY -m pip install -q -r requirements-build.txt
$PY -m PyInstaller --noconfirm --onefile --windowed --name ImageRenamer \
    --distpath 'Z:\tmp\dist' --workpath 'Z:\tmp\work' --specpath 'Z:\tmp\spec' main.py
wineserver -w
mkdir -p dist
cp /tmp/dist/ImageRenamer.exe "dist/ImageRenamer-${APP_VERSION:-dev}-windows-x86_64.exe"

# hand the output back to the host user (containers run as root)
chown -R "${HOST_UID:-0}:${HOST_GID:-0}" /src
