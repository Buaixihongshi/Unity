# A2A 协同协议（Agent-to-Agent Protocol）

> 基于项目源码：`backend/app/mcp/tools.py`、`backend/app/orchestration/runner.py`、`backend/app/extensions/tasks/adaptive.py`。
>
> 验证测试：`backend/tests/test_a2a_runner.py`、`backend/tests/test_a2a_probe.py`、`backend/tests/test_a2a_adversarial.py`。

Unity 的 A2A 不是黑盒式的全量历史重放，也不是中心化硬编码的工作流，而是基于标准 MCP 工具的点对点动态协作机制。

---

## 1. 工具接口与调用契约

伙伴通过统一 MCP 工具 `invoke_agent` 发起协作：

```json
{
  "name": "invoke_agent",
  "parameters": {
    "target_agent_id": "baobao",
    "purpose": "核对这段安全边界逻辑是否有越权风险",
    "mode": "wait_reply"
  }
}
```

### 字段说明
- `target_agent_id` (string, 必需)：目标角色标识（如 `chulan`, `baobao`, `wangye` 等）。不能委派给自己，必须是已注册的有效角色。
- `purpose` (string, 必需)：简要的委托目的与交付诉求。上限 **4000 字符**（`MAX_A2A_PURPOSE_CHARS`），超出直接报错。复杂背景、长方案或代码片段必须存为产物（artifact）并通过路径引用，杜绝在 purpose 中塞满冗余历史。
- `mode` (string, 默认 `"fire_and_forget"`):
  - `"fire_and_forget"`：单向接力。当前角色交代完任务后即告完成，宿主调度目标角色接手继续推进。
  - `"wait_reply"`：双向求助。当前角色需要等待目标角色的明确反馈后继续工作。发起方进入挂起链条（resume chain），目标角色执行完成后自动唤醒发起方。

---

## 2. 调度与生命周期机制

### 2.1 就近兑现（Immediate Honor）
在宿主编排器 `backend/app/orchestration/runner.py` 中，A2A 队列先于声明的工作流步骤消费：委派在发起它的回合结束后立即兑现，而不是漂到多步流程末尾再重提已讨论过的话题。回合内新产生的委派排在队首，嵌套接力先完成，外层等待者再恢复。

### 2.2 防环路与调用链检测
针对 `wait_reply` 模式，宿主通过 `_peek_resume_chain` 检查等待链。若 A 正在等待 B，B 再次向 A 发起 `wait_reply`，宿主立即判定为死锁环路并抛出 `MCPToolError`，杜绝无限相互等待。

### 2.3 深度预算与防返工
- **去重吸收**：`fire_and_forget` 的目标若已排在剩余工作流里，这次委派会被吸收并记为 `a2a.skipped`，避免同一话题重复发言；`wait_reply` 不会被吸收。
- **深度预算**：每个任务限定最多接力次数（`max_a2a_invocations`，`wait_reply` 的恢复回合不计数）。超出预算的新请求被跳过并记为 `a2a.skipped`，不会中断任务；若出队时预算已耗尽，任务以 blocked 结束，防止多 Agent 陷入相互推诿或客套死循环。

### 2.4 时序铁律：先登记，后发言
宿主规则要求：**需要伙伴接手时，接力工具必须在调用 `publish_message` 发言前登记**。若先发言，CLI 进程可能在输出完成后被宿主回收，导致后续的 `invoke_agent` 调用无法生效。

---

## 3. 自适应任务中的子任务持久化

在开启任务扩展（Adaptive Work）时，`invoke_agent` 会被扩展点接管：
1. 宿主将委托操作落库为 SQLite 中的持久化子任务（subtask）；
2. 委托不会解除主负责人（owner）的最终交付责任，目标角色完成评审或代码后，结果交还给负责人进行集成验收；
3. 禁止自审：代码实现者与审查者必须为不同角色，安全、权限、持久化变更必须经过独立审核。

---

## 4. 观测与审计

每次 A2A 调用都在 SQLite 事件流中留下完整记录：
- `tool.call` / `tool.result` / `tool.error`：记录委派参数与执行状态；
- `a2a.request` / `a2a.invoke` / `a2a.skipped` / `a2a.reply` / `a2a.cancelled`：供 Observability 观测模块追踪协作拓扑；
- Observability 检查项 `claims_match_events` 会核对角色发言中的行动声明与真实的 A2A 事件是否一致（声明了“已转交”但实际未调用或被 skip 会被判定为不符）。
