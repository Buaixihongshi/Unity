"""Serial, isolated real CLI pilot with observable usage and a soft spend gate.

The gate checks between CLI calls. It cannot cap an in-flight CLI request.
API-equivalent costs deliberately omit cache discounts, and are not invoices.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import shutil
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from backend.app.adapters.base import CLIAdapter, TurnOutcome
from backend.app.adapters.mcp_subprocess import MCPSubprocessAdapter
from backend.app.domain.models import TurnStatus
from backend.app.evaluation.runner import EvalConfig, EvalRunner

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "eval/pilots/cost-aware-v1"


class IsolatedCLI(MCPSubprocessAdapter):
    def _render_codex_config(self, *args, **kwargs):
        config = super()._render_codex_config(*args, **kwargs)
        # These local tools are the explicitly requested evaluation protocol.
        # Keep read-only sandboxing and never-prompt policy for native tools;
        # do not approve arbitrary MCP servers or file/command operations.
        for name in ("publish_message", "log_thought", "read_context", "invoke_agent"):
            config += f'\n[mcp_servers.yiren_hub.tools.{name}]\napproval_mode = "approve"\n'
        return config

    def _mcp_env(self, *args, **kwargs):
        env = super()._mcp_env(*args, **kwargs)
        env["PYTHONPATH"] = str(ROOT)
        return env

    def _antigravity_server_spec(self, env):
        spec = super()._antigravity_server_spec(env)
        spec["env"]["PYTHONPATH"] = str(ROOT)
        return spec

    def _setup_antigravity(self, *args, **kwargs):
        kwargs["include_repo_context"] = False
        return super()._setup_antigravity(*args, **kwargs)

    def run_turn(self, *args, **kwargs):
        # No task files, grader, repository instructions or answers in CLI cwd.
        with tempfile.TemporaryDirectory(prefix="agentclub-closed-book-") as directory:
            with contextlib.chdir(directory):
                return super().run_turn(*args, **kwargs)


def model_args(model):
    """CLI argv for one billed model; the effort is the entry's, omitted when empty (CLI default)."""
    effort = model.get("reasoning_effort") or ""
    if model["provider"] == "codex":
        args = ["exec", "--json", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
                "--model", model["cli_model_id"],
                *(["-c", f'model_reasoning_effort="{effort}"'] if effort else []),
                "-c", 'approval_policy="never"', "-c", 'web_search="disabled"']
        for feature in ("multi_agent", "shell_tool", "apps", "plugins", "hooks", "memories", "browser_use", "image_generation"):
            args += ["--disable", feature]
        return "codex", args
    if model["provider"] == "antigravity":
        return "agy", ["--model", model["cli_model_id"], *(["--effort", effort] if effort else []),
                       "--disable-slash-commands", "--output-format", "stream-json", "--dangerously-skip-permissions", "-p"]
    raise ValueError("Unexpected provider; no fallback permitted")


class Ledger:
    def __init__(self, output, models, limit_usd, prior_usd=0):
        self.path = Path(output) / "calls.jsonl"
        self.models = models
        self.limit = Decimal(str(limit_usd))
        self.spent = Decimal(str(prior_usd))
        self.calls = []
        self.stop_reason = ""

    def cost(self, model_key, usage):
        model = self.models[model_key]
        for key in ("input_tokens", "output_tokens"):
            if type(usage.get(key)) is not int or usage[key] < 0:
                raise ValueError("missing or invalid " + key)
        return (Decimal(usage["input_tokens"]) * Decimal(str(model["input_usd_per_million"]))
                + Decimal(usage["output_tokens"]) * Decimal(str(model["output_usd_per_million"]))) / 1_000_000

    def before(self, model_key):
        reservation = self.cost(model_key, {"input_tokens": 36000, "output_tokens": 4500})
        if self.stop_reason:
            raise RuntimeError(self.stop_reason)
        if self.spent + reservation > self.limit:
            self.stop_reason = "调用前费用预留不足；暂停，未启动下一次调用"
            raise RuntimeError(self.stop_reason)

    def record(self, record, usages):
        if len(usages) != 1:
            self.stop_reason = "用量缺失或不唯一，无法完整计费"
            amount = None
        else:
            try:
                amount = self.cost(record["model_key"], usages[0])
            except ValueError as exc:
                amount = None
                self.stop_reason = str(exc)
        if amount is not None:
            self.spent += amount
        record.update(usage=usages[0] if len(usages) == 1 else None,
                      api_equivalent_usd=float(amount) if amount is not None else None,
                      cumulative_api_equivalent_usd=float(self.spent))
        if self.spent >= self.limit:
            self.stop_reason = "已达到费用停止阈值；不再启动调用"
        self.calls.append(record)
        with self.path.open("a") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def audit_transport_reads(diagnostics_dir, turn_id, native):
    """agy discovers MCP tools by reading its own generated schema files.

    Permit only exact transport schema paths, never arbitrary view_file calls.
    Missing/truncated trace is insufficient evidence to waive a file read.
    """
    if not native:
        return [], 0
    allowed_parent = Path.home() / ".gemini/antigravity-cli/mcp/yiren_hub"
    schema_names = {name + ".json" for name in ("publish_message", "read_context", "log_thought", "invoke_agent")}
    safe_reads = 0
    for path in Path(diagnostics_dir).glob("*.json"):
        diagnostic = json.loads(path.read_text())
        if diagnostic.get("turn_id") != turn_id or diagnostic.get("stdout_truncated"):
            continue
        for line in diagnostic.get("stdout_tail", "").splitlines():
            try:
                step = json.loads(line).get("step_update", {})
            except (ValueError, AttributeError):
                continue
            if step.get("tool_name") != "view_file" or step.get("state") not in ("ACTIVE", "DONE"):
                continue
            target = step.get("tool_info", {}).get("parameters", {}).get("AbsolutePath")
            if isinstance(target, str):
                target = Path(target)
                if target.parent == allowed_parent and target.name in schema_names:
                    safe_reads += 1
    prohibited = [name for name in native if name != "view_file"]
    if native.count("view_file") != safe_reads:
        prohibited.append("view_file outside audited MCP schemas")
    return prohibited, safe_reads


class BudgetedAdapter(CLIAdapter):
    def __init__(self, runner, ledger, condition, factory=None):
        self.runner, self.ledger, self.condition = runner, ledger, condition
        self.factory = factory or IsolatedCLI

    def run_turn(self, mission_id, agent_id, turn_id, provider_id, prompt, tools):
        key = self.condition["mapping"][agent_id]
        model = self.ledger.models[key]
        if provider_id != model["provider"]:
            raise ValueError("provider/model mapping mismatch")
        self.ledger.before(key)
        old_ids = {event.id for event in tools.repository.list_events_after(mission_id, 0)}
        command, args = model_args(model)
        adapter = self.factory(command, args, db_path=self.runner.settings.database_path,
                               artifact_root=self.runner.settings.artifact_root,
                               mcp_module="backend.app.evaluation.pilot_mcp_server",
                               timeout=self.runner.settings.cli_timeout, idle_timeout=45)
        common = "\n【闭卷评测约束】仅使用给定题目和共享对话。除读取本次 yiren_hub MCP 工具的参数 schema 外，禁止读取本机文件、执行命令、联网、调用原生子 Agent 或 skills。只允许本次 MCP 的上下文读取、简短判断摘要、发言和已开放的 A2A。最终交付格式以 Goal 为准；检查回合只给具体检查意见。"
        try:
            outcome = adapter.run_turn(mission_id, agent_id, turn_id, provider_id, prompt + common, tools)
        except Exception as exc:
            self.ledger.record({"condition": self.condition["id"], "mission_id": mission_id,
                                "turn_id": turn_id, "agent_id": agent_id, "model_key": key,
                                "model_id": model["model_id"], "status": "failed", "error": str(exc)}, [])
            raise
        events = [event for event in tools.repository.list_events_after(mission_id, 0) if event.id not in old_ids]
        usages = [event.payload["usage"] for event in events if event.event_type == "turn.usage" and event.payload.get("turn_id") == turn_id]
        native = [event.payload.get("tool_name") for event in events if event.event_type == "provider.activity" and event.payload.get("turn_id") == turn_id
                  and not str(event.payload.get("tool_name", "")).startswith(("mcp__yiren_hub__", "yiren_hub."))]
        prohibited, schema_reads = audit_transport_reads(self.runner.settings.database_path.parent / "cli-diagnostics", turn_id, native)
        record = {"condition": self.condition["id"], "mission_id": mission_id, "turn_id": turn_id,
                  "agent_id": agent_id, "model_key": key, "model_id": model["model_id"],
                  "cli_model_id": model["cli_model_id"], "status": str(outcome.status), "native_tools": native,
                  "summary": outcome.summary,
                  "transport_schema_reads": schema_reads, "prohibited_native_tools": prohibited}
        self.ledger.record(record, usages)
        if prohibited:
            self.ledger.stop_reason = "观察到闭卷任务禁止的原生工具调用：" + ", ".join(prohibited)
        if outcome.status != TurnStatus.COMPLETED:
            detail = outcome.summary or "Provider failed"
            self.ledger.stop_reason = detail + ("；" + self.ledger.stop_reason if self.ledger.stop_reason else "")
        if self.ledger.stop_reason:
            return TurnOutcome(status=TurnStatus.BLOCKED, summary=self.ledger.stop_reason)
        return outcome


class PilotTrial(EvalRunner):
    def __init__(self, config, ledger, condition):
        self.ledger, self.condition = ledger, condition
        super().__init__(config)

    def _build_adapter(self):
        return BudgetedAdapter(self, self.ledger, self.condition)


def write_report(output, manifest, rows, ledger):
    manifest.update(completed_trials=len(rows), observed_host_calls=len(ledger.calls),
                    observed_api_equivalent_usd=float(ledger.spent), stop_reason=ledger.stop_reason,
                    status="planned" if manifest["mode"] == "dry-run" else ("paused" if ledger.stop_reason else ("completed" if len(rows) == len(manifest["plan"]) else "running")))
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    lines = ["# 首轮真实 CLI 评测", "", "状态：" + manifest["status"], "",
             "两道开发题；单次重复。费用为未应用缓存折扣的 API 等价估算，实际套餐账单未知；不是硬费用上限。", "",
             f"已执行 {len(rows)}/{len(manifest['plan'])} 个任务，{len(ledger.calls)} 个宿主调用；可观测费用 ${ledger.spent:.6f}。", "",
             "| 任务 | 条件 | 成功 | 状态 | API 等价费用 | 耗时秒 | 回合 | A2A |", "|---|---|---|---|---:|---:|---:|---:|"]
    for row in rows:
        cost = row["api_equivalent_usd"]
        lines.append(f"| {row['task_id']} | {row['condition']} | {'是' if row['passed'] else '否'} | {row['status']} | {f'${cost:.6f}' if cost is not None else '未知'} | {row['wall_ms']/1000:.1f} | {row['turns_started']} | {row['a2a_invokes']} |")
    if ledger.stop_reason:
        lines += ["", "暂停原因：" + ledger.stop_reason]
    lines += ["", "原始记录：trials.jsonl、calls.jsonl、各任务的 eval.sqlite3 与 cli-diagnostics。", "",
              "只检验当前 CLI 产品配置；角色顺序固定、未覆盖同构团队、未验证隐藏子 Agent 完全隔离，不据此做正式能力排名。"]
    (output / "report.md").write_text("\n".join(lines) + "\n")


def run_pilot(output, *, limit_usd=1.25, execute=False, timeout=120, prior_usd=0):
    output = Path(output).resolve()
    if (output / "manifest.json").exists():
        raise ValueError("Use a fresh output directory; existing trials must not be overwritten")
    output.mkdir(parents=True, exist_ok=True)
    protocol_path = PACK / "protocol.json"
    protocol = json.loads(protocol_path.read_text())
    stage = next(stage for stage in protocol["stages"] if stage["id"] == "smoke")
    plan = []
    rng = random.Random(protocol["seed"])
    for task_id in stage["task_ids"]:
        conditions = list(protocol["conditions"])
        rng.shuffle(conditions)
        plan += [{"task_id": task_id, "condition": condition} for condition in conditions]
    ledger = Ledger(output, protocol["models"], limit_usd, prior_usd)
    manifest = {"mode": "real" if execute else "dry-run", "plan": plan, "models": protocol["models"],
                "stage": "smoke", "soft_stop_usd": limit_usd, "hard_spend_limit": False,
                "prior_api_equivalent_usd": prior_usd,
                "execution_revision": "3-scoped-mcp-approval",
                "started_at": datetime.now().astimezone().isoformat(),
                "protocol_sha256": hashlib.sha256(protocol_path.read_bytes()).hexdigest(),
                "tasks_sha256": hashlib.sha256((PACK / "tasks.json").read_bytes()).hexdigest(),
                "model_verification": "Explicit CLI model arguments; cache/list preflight. Server-side model snapshot not independently attested.",
                "account_billed_cost_usd": None}
    write_report(output, manifest, [], ledger)
    if not execute:
        return manifest
    for model in protocol["models"].values():
        if shutil.which(model_args(model)[0]) is None:
            raise ValueError("CLI missing: " + model["provider"])
    rows = []
    for index, item in enumerate(plan, start=1):
        condition, task_id = item["condition"], item["task_id"]
        trial_dir = output / f"{index:02d}-{task_id}-{condition['id']}"
        config_dir = trial_dir / "config"
        config_dir.mkdir(parents=True)
        agents = json.loads((ROOT / "config/agents.json").read_text())
        for agent in agents["agents"]:
            agent["default_provider_id"] = protocol["models"][condition["mapping"][agent["id"]]]["provider"]
            agent["fallback_provider_ids"] = []
        (config_dir / "agents.json").write_text(json.dumps(agents, ensure_ascii=False))
        config = EvalConfig(tasks_path=PACK / "tasks.json", output_dir=trial_dir, config_dir=config_dir,
                            mode="real", arms=[condition["arm"]], task_ids=[task_id], repeats=1,
                            seed=protocol["seed"], persona_mode="neutral", cli_timeout=timeout, quiet=True)
        print(f"[{index}/{len(plan)}] {task_id} {condition['id']} starting", flush=True)
        runner = PilotTrial(config, ledger, condition)
        runner.run()
        row = json.loads(runner.trials_path.read_text().splitlines()[-1])
        costs = [call["api_equivalent_usd"] for call in ledger.calls if call["mission_id"] == row["mission_id"]]
        row.update(condition=condition["id"], api_equivalent_usd=sum(costs) if costs and all(cost is not None for cost in costs) else None)
        rows.append(row)
        with (output / "trials.jsonl").open("a") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        write_report(output, manifest, rows, ledger)
        print(f"  status={row['status']} passed={row['passed']} cumulative=${ledger.spent:.6f}", flush=True)
        if ledger.stop_reason:
            break
    return manifest
