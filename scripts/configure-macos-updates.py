"""Render public update settings; never reads a private key or Keychain."""
import base64
import os
from pathlib import Path
import plistlib
import sys
from urllib.parse import urlparse


def configure(info, env):
    key = env.get("SPARKLE_PUBLIC_KEY", "").strip()
    feed = env.get("SPARKLE_FEED_URL", "https://raw.githubusercontent.com/Buaixihongshi/Unity/main/packaging/macos/appcast.xml")
    url = urlparse(feed)
    test_feed = (env.get("SPARKLE_TEST_FEED") == "1"
                 and info["CFBundleIdentifier"] != "io.github.buaixihongshi.unity"
                 and url.scheme == "http" and url.hostname == "127.0.0.1")
    if url.scheme != "https" and not test_feed:
        raise ValueError("Update feed requires HTTPS (loopback allowed only for an isolated test bundle)")
    if not url.hostname or url.username or url.password or url.fragment:
        raise ValueError("Invalid public update feed URL")
    if key and len(base64.b64decode(key, validate=True)) != 32:
        raise ValueError("SPARKLE_PUBLIC_KEY must be a base64 Ed25519 public key (32 bytes)")
    if not key and env.get("REQUIRE_UPDATES") == "1":
        raise ValueError("Release requires SPARKLE_PUBLIC_KEY from the owner's signing key")
    info.update(SUFeedURL=feed, SUPublicEDKey=key,
                SUEnableAutomaticChecks=bool(key), SUAutomaticallyUpdate=True,
                SUScheduledCheckInterval=86400, SUSendProfileInfo=False,
                SUVerifyUpdateBeforeExtraction=True)
    return info


if __name__ == "__main__":
    path = Path(sys.argv[1])
    info = configure(plistlib.loads(path.read_bytes()), os.environ)
    path.write_bytes(plistlib.dumps(info))
    if not info["SUPublicEDKey"]:
        print("WARNING: no public update key; updater disabled (local build only).", file=sys.stderr)
