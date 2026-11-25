"""Connection metadata and versioned, local-only credentials.

New credentials are written before metadata is committed. A failed settings
save therefore cannot overwrite a credential used by the committed settings.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from backend.app.config.providers import load_cli_providers
from backend.app.storage import hub_settings


PROTOCOLS = {"openai_responses", "openai_chat", "anthropic"}


def validate_endpoint(url: str, allow_private: bool = False, *, resolve: bool = False) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError("invalid endpoint") from None
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or any(ord(c) <= 32 for c in url)):
        raise ValueError("endpoint must have a host and no credentials, query or fragment")
    if parsed.scheme != "https" and not (allow_private and parsed.scheme == "http"):
        raise ValueError("HTTPS required; HTTP requires explicit local/private permission")
    host = parsed.hostname
    addresses = []
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        if host.lower() == "localhost" or host.lower().endswith(".localhost"):
            if not allow_private:
                raise ValueError("private endpoint requires explicit permission")
        if resolve:
            try:
                addresses = [ipaddress.ip_address(item[4][0]) for item in
                             socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)]
            except OSError:
                raise ValueError("endpoint DNS resolution failed") from None
    if not allow_private and any(not address.is_global for address in addresses):
        raise ValueError("private endpoint requires explicit permission")
    return url.rstrip("/")


def credential_dir(settings) -> Path:
    return settings.database_path.parent / "connection-secrets"


def write_key(settings, value: str) -> str:
    directory = credential_dir(settings)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    reference = uuid.uuid4().hex
    fd = os.open(directory / reference, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        (directory / reference).unlink(missing_ok=True)
        raise
    return reference


def read_key(settings, connection: dict) -> str | None:
    reference = connection.get("key_ref", "")
    if not re.fullmatch(r"[0-9a-f]{32}", reference):
        return None
    try:
        return (credential_dir(settings) / reference).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def retire_keys(settings, previous: dict, current: dict) -> None:
    """Remove superseded credentials only after settings commit succeeds."""
    retained = {item.get("key_ref") for item in current.values()}
    for item in previous.values():
        reference = item.get("key_ref", "")
        if reference not in retained and re.fullmatch(r"[0-9a-f]{32}", reference):
            (credential_dir(settings) / reference).unlink(missing_ok=True)


def connection_views(settings, data: dict | None = None) -> list[dict]:
    data = data if data is not None else hub_settings.load(hub_settings.default_path(settings))
    views = []
    for provider in load_cli_providers(settings.config_dir):
        enabled = data.get("providers", {}).get(provider.id, {}).get("enabled", provider.enabled)
        views.append(dict(id=provider.id, name=provider.label, kind="cli", protocol=None,
                          base_url=None, enabled=enabled, has_key=False,
                          timeout=settings.cli_timeout, allow_private=False))
    for cid, connection in data.get("connections", {}).items():
        views.append(dict(id=cid, kind="api", image_input=connection.get("image_input", "native"), has_key=bool(read_key(settings, connection)),
                          **{key: connection[key] for key in
                             ("name", "protocol", "base_url", "enabled", "timeout", "allow_private")}))
    return views


def prepare_connections(existing: dict, patches: dict) -> tuple[dict, dict[str, str]]:
    """Validate all metadata before writing anything; return pending key values."""
    result = {cid: dict(value) for cid, value in existing.items()}
    keys = {}
    for cid, patch in patches.items():
        if not re.fullmatch(r"api-[A-Za-z0-9_-]{1,100}", cid):
            raise ValueError("API connection ID must start with api-")
        allowed = {"name", "protocol", "base_url", "enabled", "timeout", "allow_private",
                   "key_action", "api_key", "delete", "image_input"}
        if set(patch) - allowed:
            raise ValueError("unknown connection field")
        if patch.get("delete") is True:
            result.pop(cid, None)
            continue
        previous = result.get(cid, {})
        item = dict(name=cid, protocol="openai_responses", base_url="", enabled=True,
                    timeout=120, allow_private=False, image_input="native")
        item.update(previous)
        item.update({key: value for key, value in patch.items() if key in
                     {"name", "protocol", "base_url", "enabled", "timeout", "allow_private", "image_input"}})
        if not isinstance(item["name"], str) or not 1 <= len(item["name"].strip()) <= 100:
            raise ValueError("invalid connection name")
        if item["image_input"] not in {"native", "none"}:
            raise ValueError("unsupported image input capability")
        if item["protocol"] not in PROTOCOLS:
            raise ValueError("unsupported connection protocol")
        if type(item["enabled"]) is not bool or type(item["allow_private"]) is not bool:
            raise ValueError("connection flags must be boolean")
        if type(item["timeout"]) is not int or not 1 <= item["timeout"] <= 600:
            raise ValueError("connection timeout must be between 1 and 600 seconds")
        if not isinstance(item["base_url"], str):
            raise ValueError("invalid endpoint")
        item["base_url"] = validate_endpoint(item["base_url"], item["allow_private"])
        action = patch.get("key_action", "keep")
        if action not in {"keep", "replace", "delete"}:
            raise ValueError("invalid credential action")
        if action != "replace" and "api_key" in patch:
            raise ValueError("credential value requires replace action")
        if previous.get("key_ref") and any(item[k] != previous.get(k) for k in ("base_url", "protocol")) and action == "keep":
            raise ValueError("changing endpoint or protocol requires replacing or deleting its credential")
        if action == "replace":
            value = patch.get("api_key")
            if not isinstance(value, str) or not value.strip() or len(value) > 8192 or any(ord(c) < 32 for c in value):
                raise ValueError("invalid credential")
            keys[cid] = value
        elif action == "delete":
            item.pop("key_ref", None)
        result[cid] = item
    return result, keys
