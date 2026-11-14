"""Public build configuration rejects insecure/malformed update sources."""
import base64
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("macos_updates", Path(__file__).resolve().parents[2] / "scripts/configure-macos-updates.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_local_build_without_owner_key_disables_update_checks():
    result = module.configure({"CFBundleIdentifier": "io.github.buaixihongshi.unity"}, {})
    assert not result["SUEnableAutomaticChecks"]
    assert result["SUVerifyUpdateBeforeExtraction"]


def test_release_requires_valid_public_key_and_https():
    info = {"CFBundleIdentifier": "io.github.buaixihongshi.unity"}
    for env in ({"REQUIRE_UPDATES": "1"}, {"SPARKLE_PUBLIC_KEY": "bad"},
                {"SPARKLE_FEED_URL": "http://example.org/appcast.xml"},
                {"SPARKLE_FEED_URL": "https://user:password@example.org/appcast.xml"},
                {"SPARKLE_FEED_URL": "http://127.0.0.1/feed", "SPARKLE_TEST_FEED": "1"}):
        with pytest.raises(ValueError):
            module.configure(dict(info), env)
    result = module.configure(dict(info), {"SPARKLE_PUBLIC_KEY": base64.b64encode(bytes(32)).decode(), "REQUIRE_UPDATES": "1"})
    assert result["SUEnableAutomaticChecks"] and result["SUAutomaticallyUpdate"]
    assert result["SUScheduledCheckInterval"] == 86400
    assert not result["SUSendProfileInfo"]


def test_loopback_test_feed_requires_an_isolated_bundle():
    result = module.configure({"CFBundleIdentifier": "unity.test"}, {
        "SPARKLE_FEED_URL": "http://127.0.0.1:1234/appcast.xml", "SPARKLE_TEST_FEED": "1"})
    assert result["SUFeedURL"].startswith("http://127.0.0.1:")
