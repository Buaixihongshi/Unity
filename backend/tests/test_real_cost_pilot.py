import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomllib
import sys

from backend.app.domain.models import OrchestrationMode
from backend.app.evaluation.pilot_mcp_server import allowed, call_tool, list_tools
from backend.app.evaluation.real_pilot import BudgetedAdapter, IsolatedCLI, Ledger, PACK, ROOT, audit_transport_reads, model_args, run_pilot
from backend.app.adapters.base import TurnOutcome
from backend.app.domain.models import TurnStatus


def protocol():
    return json.loads((PACK / "protocol.json").read_text())


def test_ledger_charges_observed_usage_and_stops_after_missing_usage(tmp_path):
    ledger = Ledger(tmp_path, protocol()["models"], 1.25)
    ledger.before("strong")
    ledger.record({"model_key": "strong"}, [{"input_tokens": 1000, "output_tokens": 100, "cached_input_tokens": 900}])
    assert ledger.spent == Decimal("0.003")  # no unverified cache discount
    ledger.record({"model_key": "economy"}, [])
    assert ledger.calls[-1]["api_equivalent_usd"] is None
    with pytest.raises(RuntimeError, match="用量"):
        ledger.before("strong")


def test_ledger_checks_headroom_before_starting_another_call(tmp_path):
    ledger = Ledger(tmp_path, protocol()["models"], 0.1)
    with pytest.raises(RuntimeError, match="预留"):
        ledger.before("strong")
    assert ledger.calls == []


@pytest.mark.parametrize("mode", [OrchestrationMode.SINGLE, OrchestrationMode.REFLECTION, OrchestrationMode.FIXED, OrchestrationMode.DYNAMIC])
def test_pilot_mcp_hides_and_denies_nonallowed_tools(mode):
    mission = SimpleNamespace(orchestration=SimpleNamespace(mode=mode), room_mode="task")
    events = []
    ctx = SimpleNamespace(mission_id="m", turn_id="t", agent_id="chulan", tools=SimpleNamespace(
        repository=SimpleNamespace(get_mission=lambda _: mission, add_event=lambda *args: events.append(args))))
    names = {tool["name"] for tool in list_tools(ctx, {})["tools"]}
    assert names == allowed(ctx)
    assert events[0][1] == "evaluation.tools_list"
    assert {tool["name"] for tool in events[0][2]["tools"]} == names
    assert ("invoke_agent" in names) == (mode == OrchestrationMode.DYNAMIC)
    assert call_tool(ctx, {"name": "write_artifact"})["isError"] is True
    if mode != OrchestrationMode.DYNAMIC:
        assert call_tool(ctx, {"name": "invoke_agent"})["isError"] is True


def test_isolated_cli_runs_in_empty_directory_and_restores_cwd(monkeypatch, tmp_path):
    from backend.app.adapters.mcp_subprocess import MCPSubprocessAdapter
    original = Path.cwd()
    observed = []
    def fake_turn(self, *args, **kwargs):
        observed.append(Path.cwd())
        assert Path.cwd() != ROOT
        assert list(Path.cwd().iterdir()) == []
        return "no model invoked"
    monkeypatch.setattr(MCPSubprocessAdapter, "run_turn", fake_turn)
    adapter = IsolatedCLI("codex", db_path=tmp_path / "eval.sqlite3", artifact_root=tmp_path)
    assert adapter.run_turn() == "no model invoked"
    assert Path.cwd() == original
    assert not observed[0].exists()


def test_dry_plan_freezes_twelve_trials_without_cli_calls(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("dry-run must not construct a paid CLI adapter")
    monkeypatch.setattr(IsolatedCLI, "run_turn", forbidden)
    manifest = run_pilot(tmp_path)
    assert manifest["status"] == "planned"
    assert len(manifest["plan"]) == 12
    assert manifest["observed_host_calls"] == 0
    assert manifest["hard_spend_limit"] is False
    assert not (tmp_path / "calls.jsonl").exists()
    with pytest.raises(ValueError, match="fresh"):
        run_pilot(tmp_path)


def test_cli_arguments_pin_models_and_disable_codex_native_delegation():
    for key, model in protocol()["models"].items():
        command, args = model_args(model)
        assert args[args.index("--model") + 1] == model["cli_model_id"]
        assert "--resume" not in args
        if key == "strong":
            assert command == "codex"
            assert args[args.index("--disable") + 1] == "multi_agent"


def test_only_transport_schema_reads_are_exempted_from_file_read_policy(tmp_path):
    schema = Path.home() / ".gemini/antigravity-cli/mcp/unity_hub/publish_message.json"

    def audit(tool, key, target, native, states=("DONE",)):
        lines = [json.dumps({"event": "step_update", "step_update": {
            "tool_name": tool, "state": state, "tool_info": {"name": tool, "parameters": {key: str(target)}}}})
            for state in states]
        diagnostic = {"turn_id": "t", "stdout_truncated": False, "stdout_tail": "\n".join(lines)}
        (tmp_path / "diag.json").write_text(json.dumps(diagnostic))
        return audit_transport_reads(tmp_path, "t", native)

    # agy 1.5.3 as recorded in the 2026-11-09 q1 run: read_file/Path, ACTIVE then DONE.
    assert audit("read_file", "Path", schema, ["read_file", "read_file"], ("ACTIVE", "DONE")) == ([], 2)
    assert audit("view_file", "AbsolutePath", schema, ["view_file"]) == ([], 1)
    assert audit("read_file", "Path", ROOT / "eval/pilots/cost-aware-v1/tasks.json", ["read_file"])[0]
    legacy = Path.home() / ".gemini/antigravity-cli/mcp/yiren_hub/publish_message.json"
    assert audit("read_file", "Path", legacy, ["read_file"])[0]
    assert audit("read_file", "Path", schema, ["run_command"])[0]


def test_protocol_debugging_costs_are_not_reset_on_new_experiment(tmp_path):
    ledger = Ledger(tmp_path, protocol()["models"], 0.15, prior_usd=0.045108)
    with pytest.raises(RuntimeError, match="预留"):
        ledger.before("strong")


def test_capacity_failure_keeps_original_reason_and_unknown_cost(tmp_path):
    ledger = Ledger(tmp_path, protocol()["models"], 1.25)
    runner = SimpleNamespace(settings=SimpleNamespace(database_path=tmp_path / "eval.sqlite3", artifact_root=tmp_path, cli_timeout=120))
    tools = SimpleNamespace(repository=SimpleNamespace(list_events_after=lambda *args: []))
    condition = {"id": "B-strong", "mapping": {"chulan": "strong"}}
    factory = lambda *args, **kwargs: SimpleNamespace(run_turn=lambda *args: TurnOutcome(TurnStatus.BLOCKED, "Selected model is at capacity"))
    outcome = BudgetedAdapter(runner, ledger, condition, factory).run_turn("m", "chulan", "t", "codex", "question", tools)
    assert outcome.status == TurnStatus.BLOCKED
    assert "at capacity" in ledger.stop_reason
    assert ledger.calls[-1]["summary"] == "Selected model is at capacity"
    assert ledger.calls[-1]["api_equivalent_usd"] is None


def test_only_local_evaluation_tools_receive_explicit_approval(tmp_path):
    adapter = IsolatedCLI("codex", db_path=tmp_path / "eval.sqlite3", artifact_root=tmp_path)
    parsed = tomllib.loads(adapter._render_codex_config({}, project_path=tmp_path))
    assert set(parsed["mcp_servers"]) == {"yiren_hub"}
    tools = parsed["mcp_servers"]["yiren_hub"]["tools"]
    assert set(tools) == {"publish_message", "log_thought", "read_context", "invoke_agent"}
    assert all(value["approval_mode"] == "approve" for value in tools.values())
    assert "approval_policy" not in parsed
    assert "sandbox_mode" not in parsed


@pytest.mark.parametrize("exit_code", [0, 1])
def test_usage_persists_when_model_finishes_but_mcp_publish_fails(repository, temp_settings, tmp_path, exit_code):
    from backend.app.adapters.mcp_subprocess import MCPSubprocessAdapter
    from backend.app.domain.models import Mission
    from backend.app.mcp.tools import MCPTools
    mission = Mission(id="failed-publish-usage", title="test", goal="test")
    repository.create_mission(mission)
    script = tmp_path / "usage_without_publish.py"
    script.write_text('import json,sys\nprint(json.dumps({"type":"turn.completed","usage":{"input_tokens":321,"output_tokens":12}}))\nsys.exit(' + str(exit_code) + ')\n')
    adapter = MCPSubprocessAdapter(sys.executable, [str(script)], db_path=temp_settings.database_path, artifact_root=temp_settings.artifact_root)
    outcome = adapter.run_turn(mission.id, "chulan", "t", "codex", "test", MCPTools(repository, temp_settings.artifact_root))
    assert outcome.status == TurnStatus.BLOCKED
    usage = [event for event in repository.list_events_after(mission.id, 0) if event.event_type == "turn.usage"]
    assert len(usage) == 1
    assert usage[0].payload["usage"]["input_tokens"] == 321
