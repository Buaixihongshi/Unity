"""Observable regression for the audit gate exemption mechanism (#186).

The payload shapes below are copied from the real `npm --prefix frontend audit
--json` output measured on 2026-11-18: only picomatch carries the advisory,
every other finding reaches it through `via` package chains. A gate change
that flips one of these labels must do it on purpose.
"""
from __future__ import annotations

import json
import subprocess

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
    # The six-key stats block as measured from real `npm audit --json`.
    stats = {level: 0 for level in gate.SEVERITY_LEVELS}
    stats["high"] = len(vulnerabilities)
    stats["total"] = len(vulnerabilities)
    return {"vulnerabilities": vulnerabilities, "metadata": {"vulnerabilities": stats}}


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


# --- fail-closed regressions for #194: unusable audit data must never pass ---


def fake_npm(stdout: str, returncode: int, stderr: str = ""):
    """CompletedProcess stand-in; tests below mock subprocess.run itself (#194)."""
    return subprocess.CompletedProcess(args=["npm", "audit"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def test_main_fails_on_operational_npm_exit_code(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gate.subprocess, "run",
                        lambda *a, **k: fake_npm("{}", 2, "npm failed"))
    assert gate.main(["--exemptions", write_exemptions(tmp_path, [])]) == 1
    out = capsys.readouterr().out
    assert "exited 2" in out and "npm failed" in out


def test_main_fails_on_json_without_audit_schema(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gate.subprocess, "run",
                        lambda *a, **k: fake_npm("{}", 0))
    assert gate.main(["--exemptions", write_exemptions(tmp_path, exemption())]) == 1
    assert "vulnerabilities" in capsys.readouterr().out


def test_main_fails_on_rootless_finding_under_real_default_exemptions(monkeypatch, capsys):
    # Reproduction of #194 case 1: the repo's real exemption list stays in
    # place, yet a critical finding with an empty via chain must fail.
    audit = json.loads(json.dumps(PICOMATCH_186))
    audit["vulnerabilities"]["unknown"] = {"severity": "critical", "via": []}
    audit["metadata"]["vulnerabilities"]["total"] += 1
    monkeypatch.setattr(gate, "run_npm_audit", lambda: audit)
    assert gate.main([]) == 1
    out = capsys.readouterr().out
    assert "unknown" in out and gate.UNRESOLVED in out


def test_via_cycle_without_advisories_fails_closed(tmp_path, monkeypatch, capsys):
    audit = npm_audit_shape(advisories={}, via={"a": ["b"], "b": ["a"]})
    monkeypatch.setattr(gate, "run_npm_audit", lambda: audit)
    assert gate.main(["--exemptions", write_exemptions(tmp_path, exemption())]) == 1
    out = capsys.readouterr().out
    assert "a" in out and "b" in out and gate.UNRESOLVED in out


def test_evaluate_keeps_rootless_and_cyclic_findings_uncovered():
    audit = npm_audit_shape(advisories={}, via={"a": ["b"], "b": ["a"]})
    audit["vulnerabilities"]["lonely"] = {"severity": "critical", "via": []}
    verdict = gate.evaluate(audit, exemption())
    assert sorted(f["package"] for f in verdict["remaining"]) == ["a", "b", "lonely"]
    assert all(gate.UNRESOLVED in advisory
               for finding in verdict["remaining"] for advisory in finding["advisories"])


def test_malformed_finding_shape_is_rejected():
    audit = npm_audit_shape({}, {})
    audit["vulnerabilities"]["weird"] = {"severity": "high", "via": "not-a-list"}
    with pytest.raises(gate.AuditGateError, match="weird"):
        gate.evaluate(audit, [])


def test_non_string_via_entry_is_rejected():
    audit = npm_audit_shape({}, {})
    audit["vulnerabilities"]["weird"] = {"severity": "high", "via": [42]}
    with pytest.raises(gate.AuditGateError, match="weird"):
        gate.evaluate(audit, [])


# --- fail-closed regressions for #194 r2: summary/details disagreement must
# --- never pass: a finding only the metadata counts was never exemption-checked.


def with_stats(audit: dict, **overrides) -> str:
    """Serialized audit with metadata.vulnerabilities patched, for subprocess mocks."""
    payload = json.loads(json.dumps(audit))
    payload["metadata"]["vulnerabilities"].update(overrides)
    return json.dumps(payload)


def test_main_fails_when_summary_counts_findings_the_details_hide(tmp_path, monkeypatch, capsys):
    # Review reproduction 1: six exempted findings unchanged, only the summary
    # gains critical=1/total=7. The hidden seventh finding has no advisory to
    # check against any exemption, so the gate must fail on the contradiction.
    monkeypatch.setattr(gate.subprocess, "run",
                        lambda *a, **k: fake_npm(with_stats(PICOMATCH_186, critical=1, total=7), 1))
    assert gate.main([]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "critical=1 contradicts 0 finding(s)" in out


def test_main_fails_when_metadata_lacks_the_stats_object(tmp_path, monkeypatch, capsys):
    # Review reproduction 2: exit 1 with {vulnerabilities:{}, metadata:{}} used
    # to pass as "0 findings, 0 stale".
    monkeypatch.setattr(gate.subprocess, "run",
                        lambda *a, **k: fake_npm(json.dumps({"vulnerabilities": {}, "metadata": {}}), 1))
    assert gate.main(["--exemptions", write_exemptions(tmp_path, [])]) == 1
    assert "stats object" in capsys.readouterr().out


def test_main_fails_when_exit1_contradicts_zero_findings(tmp_path, monkeypatch, capsys):
    clean = json.dumps({"vulnerabilities": {},
                        "metadata": {"vulnerabilities": {"info": 0, "low": 0, "moderate": 0,
                                                         "high": 0, "critical": 0, "total": 0}}})
    monkeypatch.setattr(gate.subprocess, "run", lambda *a, **k: fake_npm(clean, 1))
    assert gate.main(["--exemptions", write_exemptions(tmp_path, [])]) == 1
    assert "untrustworthy" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["6", None, True, -1])
def test_main_fails_on_mistyped_stat_values(tmp_path, monkeypatch, capsys, value):
    monkeypatch.setattr(gate.subprocess, "run",
                        lambda *a, **k: fake_npm(with_stats(PICOMATCH_186, total=value), 1))
    assert gate.main([]) == 1
    assert "total" in capsys.readouterr().out


def test_main_fails_when_severity_counts_disagree_with_details(tmp_path, monkeypatch, capsys):
    # high=5 + critical=1 keeps sum(5+1)=6=total=len(details) self-consistent;
    # only the per-severity detail check catches the phantom critical.
    monkeypatch.setattr(gate.subprocess, "run",
                        lambda *a, **k: fake_npm(with_stats(PICOMATCH_186, high=5, critical=1), 1))
    assert gate.main([]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "high=5 contradicts 6 finding(s)" in out


def test_main_fails_on_unknown_finding_severity(tmp_path, monkeypatch, capsys):
    audit = json.loads(json.dumps(PICOMATCH_186))
    audit["vulnerabilities"]["fdir"]["severity"] = "extreme"
    monkeypatch.setattr(gate.subprocess, "run",
                        lambda *a, **k: fake_npm(json.dumps(audit), 1))
    assert gate.main([]) == 1
    assert "unknown severity" in capsys.readouterr().out


def test_main_passes_on_clean_audit(tmp_path, monkeypatch, capsys):
    clean = json.dumps({"vulnerabilities": {},
                        "metadata": {"vulnerabilities": {"info": 0, "low": 0, "moderate": 0,
                                                         "high": 0, "critical": 0, "total": 0}}})
    monkeypatch.setattr(gate.subprocess, "run", lambda *a, **k: fake_npm(clean, 0))
    assert gate.main(["--exemptions", write_exemptions(tmp_path, [])]) == 0
    out = capsys.readouterr().out
    assert "PASS: 0" in out
