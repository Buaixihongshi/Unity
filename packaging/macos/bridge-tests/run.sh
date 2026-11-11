#!/bin/bash
# Regression suite for the desktop unityBridge (#168/#169). macOS only: it
# drives a real WKWebView, so it cannot run on the linux CI runners — same
# situation as package-macos.sh. Verifies, in order:
#   1. UnityApp.swift still compiles as the app (package script parameters);
#   2. the bridge tests compile and pass against the shipped handler code.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
ARCH="$(uname -m)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "== compile UnityApp.swift as the app (package parameters)"
swiftc -O -target "${ARCH}-apple-macos12.0" -o "$TMP/Unity-app" \
  "$ROOT/packaging/macos/UnityApp.swift" "$ROOT/packaging/macos/main.swift"

echo "== compile + run bridge regression suite"
swiftc -O -target "${ARCH}-apple-macos12.0" -o "$TMP/bridge-tests" \
  "$ROOT/packaging/macos/UnityApp.swift" "$HERE/main.swift"
"$TMP/bridge-tests"
