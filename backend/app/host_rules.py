"""Catalog of the rules the host injects into agent prompts.

Budget, permission, irreversible approval and the collaboration contract are
constraints: the host enforces them in code and the prompt only says where the
wall is. Everything else is a hint. A hint may explain host behaviour, but it
never plans the agent's tool order; tool usage belongs in tool descriptions.

Extensions keep their own rules next to their code (``extensions/*/rules.py``)
with the same ``HostRule`` type, because the core never imports extensions.
"""
from dataclasses import dataclass
from typing import Literal

Kind = Literal['budget', 'permission', 'irreversible', 'contract', 'hint']
Enforcement = Literal['hard', 'partial', 'none']
CONSTRAINT_KINDS = frozenset({'budget', 'permission', 'irreversible', 'contract'})


@dataclass(frozen=True)
class HostRule:
    id: str
    kind: Kind
    enforcement: Enforcement
    text: str
    enforced_by: str = ''  # where the host rejects a violation; empty for hints


SPEAK = HostRule(
    'speak', 'contract', 'hard',
    '【发声】用 yiren_hub.publish_message(content=中文回复) 发出本轮回复；只写在普通输出里不算，'
    '没有成功调用它本轮就算未完成。',
    'adapters/mcp_subprocess.py: _mcp_published_message',
)
HANDOFF_BEFORE_SPEAK = HostRule(
    'handoff_before_speak', 'hint', 'none',
    '需要伙伴接手时，接力要在发言前登记：发言后 CLI 进程可能被结束，之后的 invoke_agent 不保证生效。'
    '接力失败要如实说，不能说对方会接手。',
)
CONTEXT_TOKEN = HostRule(
    'context_token', 'contract', 'hard',
    '每次调用 yiren_hub 工具都必须携带本轮 context_token；旧凭证已失效。'
    '凭证仅用于工具参数，不要写入发言、工作摘要或产物。',
    'storage/execution.py: ExecutionLedger._grant',
)
OPERATION_ID = HostRule(
    'operation_id', 'contract', 'partial',
    '有副作用的工具还须传 operation_id，同一操作重试沿用编号。',
    'storage/execution.py: ExecutionLedger.call 只对已传编号去重',
)
HOST_OWNS_STATUS = HostRule(
    'host_owns_status', 'permission', 'partial',
    '宿主控制任务终态，不要调用 update_status 结束任务。',
    '仅自主工作与圆桌拒绝：extensions/tasks/tools.py、extensions/discussion/tools.py tool_check',
)
NO_REPLAY = HostRule(
    'no_replay', 'irreversible', 'partial',
    '【中断恢复】本会话该角色有过中断记录，最近保存的动作摘要：{last_action}。'
    '部分操作可能已经生效；不得假定此前操作未发生，也不得整轮重放。',
    'storage/execution.py: operation_id 去重只覆盖 yiren_hub 工具，CLI 原生文件和命令效果拦不住',
)
SOLO = HostRule(
    'solo', 'hint', 'none',
    '本轮由你独立完成，不需要征求其他角色意见，也不要假装有别的角色在场。',
)
DELIVERY_BOUNDARY = HostRule(
    'delivery_boundary', 'hint', 'none',
    '【交付边界】讨论、解释、评审直接在聊天里回答；只有用户要文件，或已获授权的执行任务确实需要时才写文件。'
    '没有 write_artifact 工具时，可返回 JSON：'
    '{"message":"本轮回复","artifacts":[{"path":"summary.md","content":"# Summary"}]}。',
)
A2A_USAGE = HostRule(
    'a2a_usage', 'hint', 'none',
    '需要伙伴接手时调 invoke_agent(target, purpose)；要等对方回复才能继续时加 mode="wait_reply"，'
    '你这轮随即结束，下一轮会带上对方回复。只在正文里喊名字不会触发接力。',
)
A2A_BUDGET = HostRule(
    'a2a_budget', 'budget', 'hard',
    '本任务最多接力 {n} 次。',
    'mcp/tools.py: invoke_agent 深度预算',
)
ARTIFACT_QUOTE = HostRule(
    'artifact_quote', 'hint', 'none',
    '【产物清单】（引用别人写的内容以 read_artifact 读到的原文为准；跨任务时传 source_mission_id，不要凭印象引述）：',
)
MODEL_HANDOVER = HostRule(
    'model_handover', 'hint', 'none',
    '【模型交接】（本角色自上次执行以来发生模型/Provider 切换；按以下记录接续，不要因换模型重置已确认的事实）：',
)
AGY_SERVER_NAME = HostRule(
    'agy_server_name', 'hint', 'none',
    '本 CLI 里 yiren_hub 工具挂在 MCP 服务 unity_hub 下：经 call_mcp_tool 调用时 ServerName 填 unity_hub。',
)

RULES = (SPEAK, HANDOFF_BEFORE_SPEAK, CONTEXT_TOKEN, OPERATION_ID, HOST_OWNS_STATUS, NO_REPLAY,
         SOLO, DELIVERY_BOUNDARY, A2A_USAGE, A2A_BUDGET, ARTIFACT_QUOTE, MODEL_HANDOVER,
         AGY_SERVER_NAME)
