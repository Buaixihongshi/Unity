#!/usr/bin/env bash
# Build Unity.app and a drag-to-install DMG for the host architecture.
#
#   ./scripts/package-macos.sh
#   SIGN_IDENTITY="Developer ID Application: …" NOTARY_PROFILE=unity ./scripts/package-macos.sh
#
# Without SIGN_IDENTITY the bundle is ad-hoc signed: fine on this Mac, but other
# Macs need the quarantine flag removed. NOTARY_PROFILE is a `notarytool
# store-credentials` keychain profile and requires SIGN_IDENTITY.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${OUT:-$ROOT/output/macos}"
PYTHON_VERSION="${PYTHON_VERSION:-$(cat "$ROOT/.python-version")}"
BUNDLE_ID="${BUNDLE_ID:-io.github.buaixihongshi.unity}"
SIGN_IDENTITY="${SIGN_IDENTITY:-}"
NOTARY_PROFILE="${NOTARY_PROFILE:-}"
ARCH="$(uname -m)"

[ "$(uname -s)" = "Darwin" ] || { echo "ERROR: macOS only." >&2; exit 2; }
[ -z "$NOTARY_PROFILE" ] || [ -n "$SIGN_IDENTITY" ] || { echo "ERROR: NOTARY_PROFILE needs SIGN_IDENTITY." >&2; exit 2; }
for tool in uv npm swiftc hdiutil codesign; do
  command -v "$tool" >/dev/null || { echo "ERROR: missing $tool" >&2; exit 2; }
done

# Hermetic bytecode env (#60): interpreter-auto-written .pyc embed absolute
# build paths, and compileall then skips them as fresh (mtime match), leaking
# build-machine paths into the bundle. An inherited PYTHONPYCACHEPREFIX would
# instead redirect even compileall's output out of the bundle entirely.
export PYTHONDONTWRITEBYTECODE=1
unset PYTHONPYCACHEPREFIX

SHA="$(git -C "$ROOT" rev-parse HEAD)"
VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$ROOT/frontend/package.json")"
BUILD="$(git -C "$ROOT" rev-list --count HEAD)"
if [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=normal)" ]; then
  echo "WARNING: uncommitted changes; the build is labelled dirty and is not a release candidate." >&2
fi

BUILD_DIR="$OUT/build"
APP="$BUILD_DIR/Unity.app"
RES="$APP/Contents/Resources"
DMG="$OUT/Unity-$VERSION-$ARCH.dmg"
rm -rf "$BUILD_DIR" "$DMG"
mkdir -p "$APP/Contents/MacOS" "$RES/app"

echo "==> Frontend"
# Build-time identity must match what the packaged backend reports.
(cd "$ROOT/frontend" && YIREN_ENVIRONMENT=desktop npm run build)
PY_DEV="$ROOT/.venv/bin/python"; [ -x "$PY_DEV" ] || PY_DEV=python3
(cd "$ROOT" && YIREN_ENVIRONMENT=desktop "$PY_DEV" scripts/environment_status.py identity) > "$RES/app/build-identity.json"

echo "==> Relocatable Python $PYTHON_VERSION"
export UV_PYTHON_INSTALL_DIR="$OUT/python-cache"
# --no-bin: never put the build cache's interpreter on the user's PATH (~/.local/bin).
uv python install --no-bin "$PYTHON_VERSION" >/dev/null
PY_SRC="$(uv python find --managed-python "$PYTHON_VERSION")"
PREFIX="$("$PY_SRC" -c 'import sys; print(sys.prefix)')"
ditto "$PREFIX" "$RES/python"
# uv rewrites sysconfig with its install path; restore the upstream placeholder.
grep -rIl --fixed-strings "$PREFIX" "$RES/python" | while IFS= read -r file; do
  PREFIX="$PREFIX" perl -pi -e 's/\Q$ENV{PREFIX}\E/\/install/g' "$file"
done
PY="$RES/python/bin/python3"
# uv also stamps its install path into libpython's id; the interpreter loads it via @rpath.
for dylib in "$RES/python/lib"/libpython*.dylib; do install_name_tool -id "@rpath/$(basename "$dylib")" "$dylib" 2>/dev/null; done
STDLIB="$RES/python/lib/python$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
rm -rf "$STDLIB"/{test,idlelib,tkinter,turtledemo,ensurepip} "$RES/python/share" \
  "$RES/python/lib"/{tcl*,tk*,itcl*,thread*,libtcl*,libtk*} "$STDLIB"/lib-dynload/_tkinter*.so

echo "==> Runtime dependencies"
grep -vE '^(pytest|httpx)==' "$ROOT/requirements.txt" > "$BUILD_DIR/requirements-runtime.txt"
uv pip install --python "$PY" --break-system-packages --link-mode copy -q -r "$BUILD_DIR/requirements-runtime.txt"
# Console scripts carry absolute build-machine shebangs; nothing needs them.
for script in "$RES/python/bin"/*; do
  [ -L "$script" ] || [ "$(head -c 2 "$script")" != "#!" ] || rm -f "$script"
done
find "$RES/python/bin" -type l ! -exec test -e {} \; -delete
SITE="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
# Lets any bundled interpreter (MCP servers, watchdogs) import `backend` from any cwd.
echo 'import os, sys; sys.path.append(os.path.join(os.path.dirname(sys.prefix), "app"))' > "$SITE/unity-app.pth"
# pip is only a build tool here (and vendors a CA bundle the scan would flag).
rm -rf "$SITE"/pip "$SITE"/pip-*.dist-info

echo "==> Application source"
rsync -a --exclude '__pycache__/' --exclude 'tests/' "$ROOT/backend/" "$RES/app/backend/"
rsync -a "$ROOT/config/" "$RES/app/config/"
rsync -a "$ROOT/frontend/dist/" "$RES/app/frontend/dist/"
# Workflow task environments import these at run time (worktree locks, previews).
mkdir -p "$RES/app/scripts"
cp "$ROOT"/scripts/environment_*.py "$RES/app/scripts/"
cp "$ROOT/LICENSE" "$RES/LICENSE"

echo "==> Bytecode"
# Ship complete bytecode so no interpreter (including -I children that ignore
# PYTHONPYCACHEPREFIX) ever writes into the signed bundle. unchecked-hash .pyc
# stay valid after Finder copies; -s/-p keep the build path out of them.
find "$RES/python" "$RES/app" -name '__pycache__' -type d -prune -exec rm -rf {} +
"$PY" -m compileall -q -j 0 --invalidation-mode unchecked-hash -s "$RES" -p "Unity.app/Contents/Resources" \
  "$RES/python/lib" "$RES/app" >/dev/null || echo "WARNING: some modules did not compile (see compileall)." >&2
# The backend imports these lazily, so a missing one would only surface in a
# user's Workflow; import each with the bundled interpreter from a neutral cwd.
(cd "$BUILD_DIR" && "$PY" -c '
import importlib, pathlib, sys
for module in sorted(pathlib.Path(sys.argv[1]).glob("environment_*.py")):
    importlib.import_module("scripts." + module.stem)
importlib.import_module("backend.app.extensions.tasks.adaptive")' "$RES/app/scripts") \
  || { echo "ERROR: bundled Workflow modules do not import." >&2; exit 1; }

echo "==> Native shell"
swiftc -O -target "$ARCH-apple-macos12.0" -o "$APP/Contents/MacOS/Unity" "$ROOT/packaging/macos/UnityApp.swift" "$ROOT/packaging/macos/main.swift"
sed -e "s/@BUNDLE_ID@/$BUNDLE_ID/" -e "s/@VERSION@/$VERSION/" -e "s/@BUILD@/$BUILD/" -e "s/@SHA@/$SHA/" \
  "$ROOT/packaging/macos/Info.plist" > "$APP/Contents/Info.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"

ICONSET="$BUILD_DIR/Unity.iconset"
# NSImage keeps the SVG's transparent corners (qlmanage thumbnails fill them white).
if swiftc -O -o "$BUILD_DIR/render-svg" "$ROOT/packaging/macos/render-svg.swift" 2>/dev/null; then
  mkdir -p "$ICONSET"
  for size in 16 32 128 256 512; do
    "$BUILD_DIR/render-svg" "$ROOT/packaging/macos/AppIcon.svg" "$ICONSET/icon_${size}x${size}.png" $size
    "$BUILD_DIR/render-svg" "$ROOT/packaging/macos/AppIcon.svg" "$ICONSET/icon_${size}x${size}@2x.png" $((size * 2))
  done
  iconutil -c icns "$ICONSET" -o "$RES/Unity.icns"
else
  echo "WARNING: icon rendering failed; using the default app icon." >&2
fi

echo "==> Privacy scan"
leaks="$(find "$APP" \( -name '*.sqlite3' -o -name '.env' -o -name '.env.*' -o -name 'hub_settings.json' \
  -o -name 'connection-secrets' -o -name '*.pem' -o -name '*.key' -o -name '*.p12' \) -print)"
leaks+="${leaks:+$'\n'}$(grep -rl --fixed-strings "$HOME" "$APP" || true)"
if [ -n "$leaks" ]; then
  echo "ERROR: bundle contains private files or local paths:" >&2
  echo "$leaks" | sed "s#$BUILD_DIR/##" >&2
  exit 1
fi

echo "==> Signing (${SIGN_IDENTITY:-ad-hoc})"
sign_args=(--force --timestamp=none --sign -)
if [ -n "$SIGN_IDENTITY" ]; then
  sign_args=(--force --timestamp --options runtime --sign "$SIGN_IDENTITY")
fi
ENTITLEMENTS="$ROOT/packaging/macos/entitlements.plist"
# Inside-out: libraries, then executables that host Python code, then the bundle.
find "$RES" -type f \( -name '*.so' -o -name '*.dylib' -o -perm -u+x \) -print0 \
  | xargs -0 file | grep 'Mach-O' | grep -v '(for architecture' | cut -d: -f1 > "$BUILD_DIR/macho.txt"
while IFS= read -r binary; do
  # Some wheels are universal2; keep only the slice this DMG targets.
  if [ "$(lipo -archs "$binary" | wc -w)" -gt 1 ]; then lipo "$binary" -thin "$ARCH" -output "$binary"; fi
  case "$binary" in
    *.so|*.dylib) codesign "${sign_args[@]}" "$binary" 2>/dev/null ;;
    *) codesign "${sign_args[@]}" --entitlements "$ENTITLEMENTS" "$binary" 2>/dev/null ;;
  esac
done < "$BUILD_DIR/macho.txt"
codesign "${sign_args[@]}" --entitlements "$ENTITLEMENTS" "$APP"
codesign --verify --deep --strict "$APP"

echo "==> DMG"
STAGE="$BUILD_DIR/dmg"
mkdir -p "$STAGE"
ditto "$APP" "$STAGE/Unity.app"
ln -s /Applications "$STAGE/Applications"
hdiutil create -quiet -volname "Unity" -srcfolder "$STAGE" -fs HFS+ -format UDZO -ov "$DMG"
if [ -n "$SIGN_IDENTITY" ]; then
  codesign --force --timestamp --sign "$SIGN_IDENTITY" "$DMG"
fi
if [ -n "$NOTARY_PROFILE" ]; then
  xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG"
fi

echo "PASS: $DMG"
echo "  source $SHA · app $(du -sh "$APP" | cut -f1) · dmg $(du -h "$DMG" | cut -f1)"
