from typing import Optional

from backend.app import extension_points
from backend.app.adapters.base import CLIAdapter, TurnOutcome
from backend.app.domain.models import TurnStatus
from backend.app.mcp.tools import MCPTools


class FakeCLIAdapter(CLIAdapter):
    def __init__(self, response: Optional[str] = None):
        self.response = response

    def run_turn(
        self,
        mission_id: str,
        agent_id: str,
        turn_id: str,
        provider_id: str,
        prompt: str,
        tools: MCPTools,
    ) -> TurnOutcome:
        from backend.app.attachments.delivery import prepare
        prompt, _ = prepare(tools, mission_id, agent_id, turn_id, provider_id, prompt, 'native')
        taken_over = extension_points.first('demo_turn', self, mission_id, agent_id, turn_id, provider_id, tools)
        if taken_over is not None:
            return taken_over
        content = self.response or "[演示:{agent_id}] {prompt}".format(
            agent_id=agent_id,
            prompt=prompt,
        )
        tools.log_thought(
            mission_id,
            agent_id,
            turn_id,
            "我正在用演示适配器模拟思考：{agent_id} 正在处理这个任务。".format(agent_id=agent_id),
            provider_id,
            kind="reasoning",
            tags=["fake", "smoke-test"],
        )
        tools.publish_message(mission_id, agent_id, turn_id, content, provider_id)

        mission = tools.repository.get_mission(mission_id)
        if mission is not None and mission.room_mode != "roundtable" and _requires_demo_artifact(mission.goal):
            tools.write_artifact(
                mission_id,
                agent_id,
                turn_id,
                "demo-{agent_id}.md".format(agent_id=agent_id),
                "# 演示产物：{agent_id}\n\n{content}\n".format(agent_id=agent_id, content=content),
            )

        return TurnOutcome(status=TurnStatus.COMPLETED)


def _requires_demo_artifact(goal: str) -> bool:
    """Keep offline demo behavior aligned with explicit delivery intent.

    Real providers receive the full delivery policy in their prompt. The fake
    adapter has no model to interpret that policy, so it uses a deliberately
    small set of explicit execution/delivery phrases instead of treating every
    agent turn as a file request.
    """
    normalized = goal.strip().lower()
    markers = (
        "实现一个",
        "实现这个",
        "执行改动",
        "帮我实现",
        "请实现",
        "修复",
        "写一个",
        "写个",
        "写份",
        "生成一个",
        "创建一个",
        "整理成",
        "输出为",
        "交付",
        "implement ",
        "fix ",
        "build ",
        "create ",
        "write ",
        "generate ",
        "report",
        "script",
        "readme",
    )
    return any(marker in normalized for marker in markers)
