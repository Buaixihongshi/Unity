"""Observable regression for the audit gate exemption mechanism (#186).

The payload shapes below are copied from the real `npm --prefix frontend audit
--json` output measured on 2026-11-18: only picomatch carries the advisory,
every other finding reaches it through `via` package chains. A gate change
that flips one of these labels must do it on purpose.
"""
from __future__ import annotations

import json

import pytest

from scripts import audit_frontend as gate

ADVISORY = "GHSA-fc5q-39xc-vjpf"


def npm_audit_shape(advisories: dict[str, tuple[str, str]], via: dict[str, list[str]] | None = None) -> dict:
    vulnerabilities: dict[str, dict] = {}
    for name, (advisory, title) in advisories.items():
        vulnerabilities[name] = {"severity": "high", "isDirect": False,
                                 "via": [{"title": title, "severity": "high",
                                          "url": f"https://github.com/advisories/{advisory}",
                                          "range": "<=4.0.7"}]}
    for name, deps in (via or {}).items():
        vulnerabilities.setdefault(name, {"severity": "high", "isDirect": False, "via": []})
        vulnerabilities[name]["via"].extend(deps)
    return {"vulnerabilities": vulnerabilities,
            "metadata": {"vulnerabilities": {"high": len(vulnerabilities), "total": len(vulnerabilities)}}}


# The exact #186 finding: 6 packages, one root advisory, five transitive chains.
PICOMATCH_186 = npm_audit_shape(
    advisories={"picomatch": (ADVISORY, "picomatch vulnerable to ReDoS through single-branch repeated extglobs")},
    via={
        "@vitejs/plugin-react": ["vite"],
        "fdir": ["picomatch"],
        "tinyglobby": ["fdir", "picomatch"],
        "vite": ["picomatch", "tinyglobby"],
        "vitest": ["picomatch", "tinyglobby"],
    },
)


def exemption(advisory: str = ADVISORY, **extra) -> list[dict]:
    entry = {"advisory": advisory, "reason": "dev 工具链内、不进运行时产物", "issue": 186,
             "withdraw_when": "上游修复并吸收进锁文件后删除"}
    entry.update(extra)
    return [entry]


def test_exempting_the_root_advisory_covers_the_whole_chain():
    verdict = gate.evaluate(PICOMATCH_186, exemption())
    assert verdict["remaining"] == []
    assert verdict["stale"] == []
    assert sorted(verdict["exempted"]) == ["@vitejs/plugin-react", "fdir", "picomatch", "tinyglobby", "vite", "vitest"]


def test_without_exemptions_every_chain_member_stays_a_finding():
    verdict = gate.evaluate(PICOMATCH_186, [])
    assert sorted(f["package"] for f in verdict["remaining"]) == [
        "@vitejs/plugin-react", "fdir", "picomatch", "tinyglobby", "vite", "vitest"]
    picomatch = next(f for f in verdict["remaining"] if f["package"] == "picomatch")
    assert ADVISORY in picomatch["advisories"][0]


def test_exemption_for_another_advisory_neither_covers_nor_applies():
    verdict = gate.evaluate(PICOMATCH_186, exemption("GHSA-0000-0000-0000"))
    assert len(verdict["remaining"]) == 6
    assert verdict["stale"] == ["GHSA-0000-0000-0000"]


def test_own_advisory_on_a_chain_member_blocks_it_and_its_dependents():
    audit = npm_audit_shape(
        advisories={"picomatch": (ADVISORY, "picomatch ReDoS"),
                    "vite": ("GHSA-vite-own", "vite own advisory")},
        via={"@vitejs/plugin-react": ["vite"], "vitest": ["picomatch"]},
    )
    verdict = gate.evaluate(audit, exemption())
    assert sorted(f["package"] for f in verdict["remaining"]) == ["@vitejs/plugin-react", "vite"]
    assert ADVISORY in verdict["exempted"]["vitest"][0]


def test_upstream_fix_leaves_the_exemption_stale_and_failing():
    verdict = gate.evaluate(npm_audit_shape({}, {}), exemption())
    assert verdict["remaining"] == []
    assert verdict["stale"] == [ADVISORY]


def test_via_reference_to_a_missing_package_stays_a_finding():
    audit = npm_audit_shape({}, {"vite": ["ghost-package"]})
    verdict = gate.evaluate(audit, exemption())
    assert [f["package"] for f in verdict["remaining"]] == ["vite"]
    assert gate.UNRESOLVED in verdict["remaining"][0]["advisories"][0]


def write_exemptions(tmp_path, entries) -> str:
    path = tmp_path / "audit-exemptions.json"
    path.write_text(json.dumps({"exemptions": entries}, ensure_ascii=False))
    return str(path)


def test_exemption_file_with_missing_field_is_rejected(tmp_path):
    path = tmp_path / "audit-exemptions.json"
    path.write_text(json.dumps({"exemptions": [{"advisory": ADVISORY, "reason": "有理由", "issue": 186}]},
                               ensure_ascii=False))
    with pytest.raises(gate.AuditGateError, match="withdraw_when"):
        gate.load_exemptions(path)


def test_duplicate_advisory_entries_are_rejected(tmp_path):
    path = tmp_path / "audit-exemptions.json"
    path.write_text(json.dumps({"exemptions": exemption() + exemption()}, ensure_ascii=False))
    with pytest.raises(gate.AuditGateError, match="duplicates"):
        gate.load_exemptions(path)


def test_main_passes_on_exempted_186_chain(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gate, "run_npm_audit", lambda: PICOMATCH_186)
    assert gate.main(["--exemptions", write_exemptions(tmp_path, exemption())]) == 0
    out = capsys.readouterr().out
    assert ADVISORY in out and "PASS" in out


def test_main_fails_on_stale_exemption(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gate, "run_npm_audit", lambda: npm_audit_shape({}, {}))
    assert gate.main(["--exemptions", write_exemptions(tmp_path, exemption())]) == 1
    assert "stale exemption" in capsys.readouterr().out


def test_main_fails_when_npm_reports_an_error(tmp_path, monkeypatch, capsys):
    def broken_npm():
        raise gate.AuditGateError("npm audit error: ENOTFOUND")
    monkeypatch.setattr(gate, "run_npm_audit", broken_npm)
    assert gate.main(["--exemptions", write_exemptions(tmp_path, exemption())]) == 1
    assert "ENOTFOUND" in capsys.readouterr().out
