#!/usr/bin/env bash
set -euo pipefail
cd /src
python3 -m venv --system-site-packages /tmp/venv
/tmp/venv/bin/pip install -q -r requirements-build.txt
/tmp/venv/bin/python -c "import tkinter; print('tkinter', tkinter.TkVersion)"
/tmp/venv/bin/pyinstaller --noconfirm --onefile --windowed --hidden-import PIL._tkinter_finder --name ImageRenamer \
    --distpath /tmp/dist --workpath /tmp/work --specpath /tmp/spec main.py
mkdir -p dist
cp /tmp/dist/ImageRenamer "dist/ImageRenamer-${APP_VERSION:-dev}-linux-x86_64"

# hand the output back to the host user (containers run as root)
chown -R "${HOST_UID:-0}:${HOST_GID:-0}" /src
