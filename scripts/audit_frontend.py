"""Frontend dependency audit gate with an explicit, reviewable exemption list.

Replaces the bare `npm --prefix frontend audit` call (#186): findings whose
every root advisory is listed in config/audit-exemptions.json pass, anything
else fails, and an exemption that no longer matches a finding also fails so
the list cannot rot once upstream ships a fix.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
EXEMPTIONS_PATH = ROOT / "config" / "audit-exemptions.json"
REQUIRED_FIELDS = ("advisory", "reason", "issue", "withdraw_when")
UNRESOLVED = "unresolved"


class AuditGateError(Exception):
    """Unusable gate input (broken npm output or broken exemption file)."""


def load_exemptions(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditGateError(f"{path} is not readable JSON: {exc}") from exc
    entries = data.get("exemptions") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise AuditGateError(f"{path} must be a JSON object with an 'exemptions' list")
    known: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise AuditGateError(f"exemption #{index} in {path.name} is not an object")
        for field in REQUIRED_FIELDS:
            if not str(entry.get(field, "")).strip():
                raise AuditGateError(f"exemption #{index} in {path.name} is missing required field '{field}'")
        advisory = str(entry["advisory"]).strip()
        if advisory in known:
            raise AuditGateError(f"exemption #{index} in {path.name} duplicates advisory {advisory}")
        known.add(advisory)
    return entries


def run_npm_audit(root: Path = ROOT) -> dict:
    proc = subprocess.run(["npm", "--prefix", "frontend", "audit", "--json"],
                          cwd=root, capture_output=True, text=True)
    detail = " | ".join((proc.stderr or proc.stdout or "").strip().splitlines()[-3:])
    # npm audit exits 1 when it reports vulnerabilities; any other exit code
    # is an operational failure (network, broken install) and must fail the
    # gate instead of silently passing as "no findings" (#194).
    if proc.returncode not in (0, 1):
        raise AuditGateError(f"npm audit exited {proc.returncode}, expected 0 or 1: {detail}")
    try:
        audit = json.loads(proc.stdout or "")
    except json.JSONDecodeError as exc:
        raise AuditGateError(f"npm audit produced no JSON (exit {proc.returncode}): {detail}") from exc
    if not isinstance(audit, dict):
        raise AuditGateError(f"npm audit JSON is not an object: {detail}")
    if isinstance(audit.get("error"), dict):
        raise AuditGateError(f"npm audit error: {audit['error'].get('summary') or audit['error']}")
    for key in ("vulnerabilities", "metadata"):
        if not isinstance(audit.get(key), dict):
            raise AuditGateError(f"npm audit output lacks a '{key}' object; "
                                 f"refusing to pass on unusable audit data: {detail}")
    return audit


def _advisory_id(via: dict) -> str:
    url = str(via.get("url") or "")
    return url.rstrip("/").rsplit("/", 1)[-1] or UNRESOLVED


def evaluate(audit: dict, exemptions: list[dict]) -> dict:
    """Split audit findings into exempted / remaining / stale exemptions.

    A package finding is exempted only when every root advisory reachable
    through its `via` chains is explicitly exempted; any non-exempted root
    advisory keeps the package (and everything depending on it) a finding.
    A finding that resolves to no root advisory at all (empty via, or a via
    cycle without advisories) is kept as unresolved and fails the gate.
    Malformed findings or via entries raise AuditGateError instead of pass.
    """
    vulns = audit.get("vulnerabilities") or {}
    direct: dict[str, list[tuple[str, str]]] = {}
    edges: dict[str, list[str]] = {}
    for name, finding in vulns.items():
        if not isinstance(finding, dict) or not isinstance(finding.get("via"), list):
            raise AuditGateError(f"finding for {name} is malformed: expected an object with a 'via' list")
        advisories: list[tuple[str, str]] = []
        via_packages: list[str] = []
        for via in finding["via"]:
            if isinstance(via, dict):
                advisories.append((_advisory_id(via), str(via.get("title") or "")))
            elif isinstance(via, str) and via in vulns:
                via_packages.append(via)
            elif isinstance(via, str):
                advisories.append((UNRESOLVED, f"via {via} (package missing from audit output)"))
            else:
                raise AuditGateError(f"finding for {name} has a non-string/dict via entry: {via!r}")
        direct[name] = advisories
        edges[name] = via_packages

    exempt_ids = {str(entry["advisory"]).strip() for entry in exemptions}
    roots_cache: dict[str, set[tuple[str, str]]] = {}

    def roots(package: str, stack: tuple[str, ...] = ()) -> set[tuple[str, str]]:
        if package in roots_cache:
            return roots_cache[package]
        acc = set(direct[package])
        for nxt in edges[package]:
            if nxt in stack:
                continue
            acc |= roots(nxt, stack + (package,))
        roots_cache[package] = acc
        return acc

    remaining: list[dict] = []
    exempted: dict[str, list[str]] = {}
    used: set[str] = set()
    for name in vulns:
        package_roots = roots(name)
        if not package_roots:
            # Empty via, or a via cycle carrying no advisory anywhere: nothing
            # was actually verified for this package, so it fails closed (#194).
            remaining.append({"package": name, "advisories": [
                f"{UNRESOLVED} (no root advisory resolved from its via chain)"]})
            continue
        covered = sorted({ident for ident, _ in package_roots if ident in exempt_ids})
        used.update(covered)
        if covered:
            exempted[name] = covered
        blocked = sorted(f"{ident} {title}".strip() for ident, title in package_roots if ident not in exempt_ids)
        if blocked:
            remaining.append({"package": name, "advisories": blocked})

    return {"remaining": remaining, "exempted": exempted, "stale": sorted(exempt_ids - used)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exemptions", type=Path, default=EXEMPTIONS_PATH,
                        help="exemption list to apply (default: config/audit-exemptions.json)")
    args = parser.parse_args(argv)
    try:
        exemptions = load_exemptions(args.exemptions)
        audit = run_npm_audit()
    except AuditGateError as exc:
        print(f"audit: FAIL: {exc}")
        return 1
    verdict = evaluate(audit, exemptions)
    for name, ids in sorted(verdict["exempted"].items()):
        print(f"audit: exempted {', '.join(ids)} reachable from {name}")
    for finding in verdict["remaining"]:
        for advisory in finding["advisories"]:
            print(f"audit: NOT exempted: {finding['package']}: {advisory}")
    for ident in verdict["stale"]:
        print(f"audit: stale exemption {ident} matches no current finding; remove it per its withdraw_when note")
    if verdict["remaining"] or verdict["stale"]:
        print(f"audit: FAIL: {len(verdict['remaining'])} package finding(s) uncovered, "
              f"{len(verdict['stale'])} stale exemption(s)")
        return 1
    total = ((audit.get("metadata") or {}).get("vulnerabilities") or {}).get("total", 0)
    print(f"audit: PASS: {total} reported finding(s) fully covered by reviewed exemptions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
