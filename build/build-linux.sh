#!/usr/bin/env bash
set -euo pipefail
cd /src
pip install -q -r requirements-build.txt
pyinstaller --noconfirm --onefile --windowed --name ImageRenamer \
    --distpath /tmp/dist --workpath /tmp/work --specpath /tmp/spec main.py
mkdir -p dist
cp /tmp/dist/ImageRenamer "dist/ImageRenamer-${APP_VERSION:-dev}-linux-x86_64"
