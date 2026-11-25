"""Private, bounded per-attempt evidence; never used as chat output."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import time
from uuid import uuid4

MAX_STREAM_CHARS = 32_768
MAX_RECORDS = 100
_LOGGER = logging.getLogger(__name__)
_CREDENTIAL = re.compile(
    r'''(?i)(["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|context[_-]?token|authorization|password|secret)["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,}]+)'''
)


def _redact(text: str) -> str:
    text = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", text)
    text = re.sub(r'data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+', 'data:image/[REDACTED]', text)
    text = re.sub(r'("(?:data|image_data)"\s*:\s*")[A-Za-z0-9+/=]{32,}("?)', r'\1[IMAGE_REDACTED]\2', text)
    text = _CREDENTIAL.sub(r"\1[REDACTED]", text)
    return re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", text)


def write_cli_diagnostic(directory: Path, *, stdout: str, stderr: str, **metadata) -> None:
    """Best-effort logs, capped at 100 attempts; no env or MCP config copies.

    Tails can contain project text, so keep them local/private even after
    credential redaction. Diagnostic failures must not change turn outcomes.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        record = dict(metadata, recorded_at=time.time())
        for name, value in (("stdout", stdout), ("stderr", stderr)):
            # Redact before truncation so a cut cannot hide the credential key.
            clean = _redact(value)
            record[name + "_tail"] = clean[-MAX_STREAM_CHARS:]
            record[name + "_truncated"] = len(clean) > MAX_STREAM_CHARS
        path = directory / (uuid4().hex + ".json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False)
        records = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in records[MAX_RECORDS:]:
            old.unlink(missing_ok=True)
    except OSError:
        _LOGGER.warning("Could not save CLI diagnostic", exc_info=True)
