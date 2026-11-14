#!/usr/bin/env bash
# Source to provision the pinned official Sparkle distribution. No signing keys.
SPARKLE_VERSION=2.10.1
SPARKLE_SHA256=1a856d2a9edb8e628de88121d7be04cf0e2167f8641650b78fca00b14244cf27
SPARKLE_CACHE="${SPARKLE_CACHE:-$ROOT/output/sparkle}"
SPARKLE_ARCHIVE="$SPARKLE_CACHE/Sparkle-$SPARKLE_VERSION.tar.xz"
SPARKLE_ROOT="$SPARKLE_CACHE/$SPARKLE_VERSION"
mkdir -p "$SPARKLE_CACHE"
if [ ! -f "$SPARKLE_ARCHIVE" ]; then
  curl -fL --retry 3 "https://github.com/sparkle-project/Sparkle/releases/download/$SPARKLE_VERSION/Sparkle-$SPARKLE_VERSION.tar.xz" -o "$SPARKLE_ARCHIVE.part"
  mv "$SPARKLE_ARCHIVE.part" "$SPARKLE_ARCHIVE"
fi
[ "$(shasum -a 256 "$SPARKLE_ARCHIVE" | awk '{print $1}')" = "$SPARKLE_SHA256" ] || {
  echo 'ERROR: Sparkle archive checksum mismatch.' >&2; exit 2;
}
# Re-extract from the verified archive; never trust an edited cache directory.
# A per-invocation directory avoids races between packaging and native tests.
SPARKLE_ROOT="$(mktemp -d "$SPARKLE_CACHE/verified.XXXXXX")"
tar -xf "$SPARKLE_ARCHIVE" -C "$SPARKLE_ROOT"
