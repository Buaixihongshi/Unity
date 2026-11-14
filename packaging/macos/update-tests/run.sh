#!/bin/bash
# Real framework, WKWebView and bundled backend; private feed/key/data only.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
exec "$ROOT/.venv/bin/python" "$ROOT/packaging/macos/update-tests/run.py" "$@"
