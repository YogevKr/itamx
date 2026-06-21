#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$ROOT/dist"
NAME="itamx-elal-award-inspector"
VERSION="$(python3 -c 'import json, pathlib; print(json.loads(pathlib.Path("'"$ROOT"'/manifest.json").read_text())["version"])')"
OUT="$OUT_DIR/$NAME-$VERSION.xpi"

mkdir -p "$OUT_DIR"
rm -f "$OUT"

cd "$ROOT"
zip -qr "$OUT" manifest.json content.js page-hook.js styles.css README.md
echo "$OUT"
