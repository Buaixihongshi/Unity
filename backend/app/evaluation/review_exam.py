"""Answer sheet format and location grading for the code-review exam.

Examinees review one PR diff and hand in a fixed JSON answer sheet. Grading
matches findings to the private answer key by file and line window only; a
location hit says nothing about whether the stated reason is right, so every
hit is also queued for blinded reason judging. Nothing here calls a model.
"""
from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Iterable

MAX_FINDINGS = 20
MAX_SUMMARY_CHARS = 500
# A finding may not cover a whole file: wide ranges would "hit" every bug.
MAX_SPAN_LINES = 30
DEFAULT_TOLERANCE = 5
VERDICTS = ("issues", "no_issues")

_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


class SheetError(ValueError):
    """The answer sheet does not follow the required format."""


@dataclass(frozen=True)
class Finding:
    file: str
    line_start: int
    line_end: int
    summary: str


@dataclass(frozen=True)
class Sheet:
    verdict: str
    findings: tuple[Finding, ...]


@dataclass(frozen=True)
class KeyBug:
    id: str
    file: str
    line_start: int
    line_end: int
    description: str = ""


@dataclass(frozen=True)
class AnswerKey:
    paper_id: str
    kind: str  # "bug" or "clean"
    bugs: tuple[KeyBug, ...]


@dataclass
class Grade:
    paper_id: str
    valid: bool
    error: str = ""
    hits: list[dict] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    false_positives: list[dict] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "paper_id": self.paper_id,
            "valid": self.valid,
            "error": self.error,
            "hits": self.hits,
            "missed": self.missed,
            "false_positives": self.false_positives,
            "duplicates": self.duplicates,
        }


def parse_sheet(text: str) -> Sheet:
    """Read the answer sheet from raw examinee output.

    Accepts either bare JSON or exactly one fenced JSON block, because CLIs
    often wrap their final answer in prose. More than one block is rejected
    rather than guessed at.
    """
    raw = text.strip()
    blocks = _FENCE.findall(raw)
    if len(blocks) > 1:
        raise SheetError("answer contains more than one JSON block")
    if blocks:
        raw = blocks[0]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SheetError(f"answer is not JSON: {exc.msg}") from None
    if not isinstance(data, dict):
        raise SheetError("answer must be a JSON object")
    unknown = set(data) - {"verdict", "findings"}
    if unknown:
        raise SheetError(f"unknown fields: {sorted(unknown)}")
    verdict = data.get("verdict")
    if verdict not in VERDICTS:
        raise SheetError(f"verdict must be one of {VERDICTS}")
    items = data.get("findings")
    if not isinstance(items, list):
        raise SheetError("findings must be a list")
    if verdict == "no_issues" and items:
        raise SheetError("no_issues must have an empty findings list")
    if verdict == "issues" and not items:
        raise SheetError("issues needs at least one finding")
    if len(items) > MAX_FINDINGS:
        raise SheetError(f"at most {MAX_FINDINGS} findings")
    return Sheet(verdict=verdict, findings=tuple(_finding(item, index) for index, item in enumerate(items)))


def load_key(data: dict) -> AnswerKey:
    kind = data.get("kind")
    if kind not in ("bug", "clean"):
        raise ValueError("key kind must be bug or clean")
    bugs = tuple(
        KeyBug(
            id=str(item["id"]),
            file=_relative_path(item["file"]),
            line_start=int(item["line_start"]),
            line_end=int(item["line_end"]),
            description=str(item.get("description", "")),
        )
        for item in data.get("bugs", [])
    )
    if (kind == "bug") != bool(bugs):
        raise ValueError("a bug paper needs bugs; a clean paper must have none")
    if len({bug.id for bug in bugs}) != len(bugs):
        raise ValueError("duplicate bug id in key")
    return AnswerKey(paper_id=str(data["paper_id"]), kind=kind, bugs=bugs)


def grade(text: str, key: AnswerKey, tolerance: int = DEFAULT_TOLERANCE) -> Grade:
    """Grade one raw answer against its key.

    An unreadable sheet is graded as invalid with every bug missed, never as a
    silent "no issues". Each bug can be hit once; further findings on an
    already-hit bug are duplicates, not false positives. Findings that match no
    bug are false positives (on a clean paper, every finding is one).
    """
    try:
        sheet = parse_sheet(text)
    except SheetError as exc:
        return Grade(paper_id=key.paper_id, valid=False, error=str(exc), missed=[bug.id for bug in key.bugs])
    result = Grade(paper_id=key.paper_id, valid=True)
    hit_ids: set[str] = set()
    for index, finding in enumerate(sheet.findings):
        matches = [bug for bug in key.bugs if _overlaps(finding, bug, tolerance)]
        fresh = [bug for bug in matches if bug.id not in hit_ids]
        record = {"finding": index, "file": finding.file, "line_start": finding.line_start,
                  "line_end": finding.line_end, "summary": finding.summary}
        if fresh:
            bug = fresh[0]
            hit_ids.add(bug.id)
            result.hits.append({**record, "bug": bug.id})
        elif matches:
            result.duplicates.append({**record, "bug": matches[0].id})
        else:
            result.false_positives.append(record)
    result.missed = [bug.id for bug in key.bugs if bug.id not in hit_ids]
    return result


def judge_queue(entries: Iterable[tuple[str, Grade, AnswerKey]], seed: int = 0) -> tuple[list[dict], dict]:
    """Build a blinded reason-judging queue from location hits.

    ``entries`` are (arm, grade, key). The returned items carry only an
    anonymous id, the finding and the key description; the arm mapping is
    returned separately so it can stay with the private answer material.
    """
    items = []
    for arm, result, key in entries:
        bugs = {bug.id: bug for bug in key.bugs}
        for hit in result.hits:
            items.append((arm, result.paper_id, hit, bugs[hit["bug"]]))
    random.Random(seed).shuffle(items)
    queue, mapping = [], {}
    for number, (arm, paper_id, hit, bug) in enumerate(items, start=1):
        anonymous = f"j{number:03d}"
        mapping[anonymous] = {"arm": arm, "paper_id": paper_id, "finding": hit["finding"]}
        queue.append({
            "id": anonymous,
            "location": f"{hit['file']}:{hit['line_start']}-{hit['line_end']}",
            "examinee_reason": hit["summary"],
            "known_bug": bug.description,
            "question": "考生说的原因是否就是这颗已知 bug？只答 same / different / unclear。",
        })
    return queue, mapping


def _finding(item: object, index: int) -> Finding:
    if not isinstance(item, dict):
        raise SheetError(f"finding {index} must be an object")
    unknown = set(item) - {"file", "line_start", "line_end", "summary"}
    if unknown:
        raise SheetError(f"finding {index} has unknown fields: {sorted(unknown)}")
    start, end = item.get("line_start"), item.get("line_end")
    if not (_positive_int(start) and _positive_int(end)) or start > end:
        raise SheetError(f"finding {index} needs positive line_start <= line_end")
    if end - start + 1 > MAX_SPAN_LINES:
        raise SheetError(f"finding {index} spans more than {MAX_SPAN_LINES} lines")
    summary = item.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise SheetError(f"finding {index} needs a summary")
    if len(summary) > MAX_SUMMARY_CHARS:
        raise SheetError(f"finding {index} summary exceeds {MAX_SUMMARY_CHARS} characters")
    try:
        path = _relative_path(item.get("file"))
    except ValueError as exc:
        raise SheetError(f"finding {index}: {exc}") from None
    return Finding(file=path, line_start=start, line_end=end, summary=summary.strip())


def _relative_path(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("file must be a repository-relative path")
    text = value.strip().removeprefix("./")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or "\\" in text:
        raise ValueError("file must be a repository-relative path")
    return str(path)


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _overlaps(finding: Finding, bug: KeyBug, tolerance: int) -> bool:
    return (finding.file == bug.file
            and finding.line_start <= bug.line_end + tolerance
            and finding.line_end >= bug.line_start - tolerance)
