# Changelog

本项目的重要变更记录在此文件中。

## [1.0.0] - 2026-11-02

### 相比 v0.2.0-preview.1 的升级

- 新增会话右侧产物栏及跨会话文件仓库，支持查看与下载生成的文件（#90）。
- 普通聊天支持独立停止和消息编辑入口，回车换行；重启后收尾中断回合，减少会话锁死（#81、#85、#104）。
- 桌面版可从菜单选择项目仓库，讨论、任务与仓库维护作为按需启用的扩展（#53、#75、#77）。
- 观测分为 Trace 可信度与 Agent 行为两层，失败按归属拆分，支持回合详情与成本查看（#109）。
- 修正自定义角色名称、连接控件尺寸及观测回合表格行对齐（#113）。
- Antigravity 使用工作区插件挂载 MCP，不同会话的轮次可并行（#118）。
- 加入人物头像，以及莫比乌斯环启动、等待动效（#78、#116）。
- 桌面包、前端包元数据和健康接口版本统一为 1.0.0。提供 Apple Silicon DMG 与 SHA-256 校验文件。

以下为本阶段累计变更。

### Branding

- 对外项目名称调整为 Unity，统一页面、引导、模型提示及项目介绍；GitHub 仓库同步更名为 `Buaixihongshi/Unity`，数据目录和内部协议标识保持兼容。

### Added

- 新增 macOS 桌面版：`scripts/package-macos.sh` 生成内置运行环境的 `Unity.app` 与拖拽安装 dmg，从 Releases 下载即可使用；新增莫比乌斯缎带图标。安装说明见 README。
- 会话与任务拆为独立入口，新增全局任务列表、独立新建与可编辑 Workflow；保留自主编排、真实依赖调度和根任务集成验收，兼容历史任务板。
- 相同内容快照增加按执行上下文的授权引用，支持多个独立任务复用基线并保持访问隔离。

- 新增上下文记忆：角色长期记忆以 Markdown 文件为权威存储（`memory_root`，默认 `<状态>/data/memory`），SQLite 仅保存可重建索引、会话短期检查点和模型切换执行状态；新增 MCP 工具 `memory`（读取/写入/修订/忘记本角色记忆）与 `save_checkpoint`（会话滚动检查点），`read_context` 支持按消息 ID 原文补读并返回记忆/检查点/交接节；prompt 上下文装配统一接入普通任务、圆桌与自主工作三条路径，Provider 验证任务不注入记忆；角色有效模型变化（设置或回退）记录交接并在下一次 prompt 注入一次；新增 `/api/memory/*` 管理接口与设置内"记忆"页签（查看、修订、忘记/恢复/清除、重建索引、检查点与交接记录）。详见 [上下文记忆文档](docs/CONTEXT_MEMORY.md)。
- 新增可选的 Antigravity 常驻进程模式：按会话与角色隔离、每轮执行凭证、正式回复去重、宿主退出监护及中断恢复记录；默认关闭，详见 [2026-10-18 升级记录](docs/upgrades/2026-10-18-antigravity-session-recovery.md)。
- 新增代码仓库维护内核：受信仓库登记、隔离 Git worktree、无 shell 验证契约、持久化维护状态、重启恢复、审批/反馈记录与受控进化提案。
- 设置新增「观测」页签：跨全部会话统计总量、检查项、Trace 覆盖率、花费、Agent / Provider 与最近失败，并支持全部 / 今天 / 7 天 / 30 天窗口；可下钻到逐回合表，再点开单回合的步骤、参数、输出与检查项。接口 `GET /api/observability/overview` 与 `GET /api/observability/turns/{turn_id}`。
- 新增 `GET /api/observability`：按回合给出步骤（工具 / 原生调用 / 协作 / 产物）、三项读时检查、成本与 trace 覆盖率；说明见 `docs/OBSERVABILITY.md`。
- 可观测性字段补齐：三个 CLI 的原生工具调用现在记录 `args` / `output` / `ok` / `error` / `duration_ms`，并新增 `provider.session`（session_id、model）；MCP 的 `tool.call` / `tool.result` / `tool.error` 增加 `call_id`、`ok`、`duration_ms`；`turn.usage` 增加 `model` / `stop_reason` / `duration_ms`。所有输出落库前统一脱敏并截断。
- 可观测性采集补强：v2 的 `read_context` 记录冻结阶段的可读摘要（phase、任务、其他 Agent 观点、材料清单），v2 `read_artifact` 补上工具事件与正文；codex / claude 的原生工具调用补上 `activity_id` 后才会落成 `provider.activity`，并附带命令、搜索结果或工具参数摘要；`tool.result` 可携带脱敏并截断的输出（思考正文、消息正文、产物摘要等）；成本在 provider 未报价时按费率估算。
- 新增 A/B/C/D 多 Agent 对照评测：Single Agent、Self Reflection、固定多 Agent 工作流和动态 A2A 共享任务集、lead 与 Provider；B/C/D 共享 4 回合检查骨架，输出质量、成本、延迟、配对置信区间和退化标记。入口为 `scripts/run_eval.py`，说明见 `docs/EVALUATION.md`。

### Changed

- 回到初心：Unity 默认只保留共享上下文核心（共享对话、`invoke_agent` 接力、记忆、产物、观测与评测）。多人讨论（圆桌与 `/discuss`）、任务和仓库维护改为可选扩展，默认关闭，可在「设置 → 扩展」或 `YIREN_DISCUSSION_ENABLED` / `YIREN_TASKS_ENABLED` / `YIREN_MAINTENANCE_ENABLED` 开启，切换即时生效。关闭时普通对话的 MCP 工具列表不含 `shared_work`，prompt 不再提示提议任务，界面隐藏任务页与 `/discuss`，创建接口返回 409；已有记录保留，重新开启后可继续。升级后若仍有进行中的任务，请先开启任务扩展。见 [计划](docs/plans/2026-10-30-core-and-extensions.md)。
- 移除 L4 铁律：`Settings.use_fake_cli` 与 local/runtime 启动默认改为真实 CLI；只有显式 `--demo`、test 环境与测试 fixture 使用离线适配器。仓库内真实 Provider 仍默认关闭，由引导或设置开启。
- 首次启动引导不再使用演示模式：先检测本机 CLI，确认后由张楚岚通过所选真实 CLI 完成第一次对话（同时作为接入验证），再让三位角色使用该 CLI 并通过真实 CLI 互相介绍；没有可用 CLI 时显示安装与登录说明。离线演示环境（`--demo`/test）拦截这些真实调用。
- 观测入口移入设置页签：移除右侧栏按钮、`ObservabilityDialog` 与 `useObservability`，会话主区域不再预留侧栏宽度；原来的单会话细节改为在设置 → 观测里点击回合查看。

- 讨论 V2 更严格对齐六阶段架构：Phase 2 按决策项、关键假设和建议行动判断是否跳过，存在冲突时只邀请与冲突决策项相关的 claim 作者，轮数范围收紧为 2～3；Phase 6 综合稿必须逐项记录非价值问题的技术处置，价值问题仍只由用户决定。
- 任务 Mission 新增可选 `orchestration` 配置；默认仍是 `auto`（现有复杂度路由），评测可显式选择 `single` / `reflection` / `fixed` / `dynamic`。
- 三位角色改为通透机敏、坦率好奇、松弛笃定的讨论伙伴：移除固定工种路由，统一能力边界，更新人格提示、新用户介绍与界面标签。

- 将用户可见的产品名称统一为 AgentClub。
- README 增加首次启动、CLI 接入和团队配置演示图。
- README 按产品定义、核心能力和差异化优势重构叙事，并重点说明角色、Provider 与路由三层解耦。
- 新增完整英文版 `README_EN.md`，并在中英文文档之间提供语言切换入口。
- A2A 接力增加可配置的无进展软超时、结构化活动续期和 Provider fallback；前端按目标轮次真实终态展示接力，不再将已阻塞任务标记为等待中。

### Fixed

- 常驻 Antigravity 的迟到 MCP 调用与重复发布受到执行凭证和操作回执约束；工作进程异常退出时核对已保存回复并保留进度，避免整轮盲目重放。
- 将任务复杂度与文件交付意图解耦：讨论、比较和评审任务不再因为宝儿姐参与而自动生成产物，演示模式与真实 Provider 使用一致的按需交付边界。

## [0.1.0] - 2026-08-05

### Added

- 新用户一键启动和首次对话引导：先通过内置演示模式成功触发张楚岚，再进入真实 CLI。
- Codex-first 接入流程，包括安装/认证提示、MCP 真实对话验证和三角色一键共用 Codex。
- Claude Code 与 Antigravity CLI 的后续扩展入口及常驻团队配置。
- 会话归档、已归档列表和恢复能力。
- 健康检查、onboarding API、Provider 诊断与设置持久化迁移。
- 设置页分类导航、Provider 生命周期状态和角色路由管理。
- `scripts/start.sh` 一键启动与 `scripts/release-check.sh` 发布门禁。

### Changed

- 团队角色保持张楚岚 / 冯宝宝 / 王也，Provider 与角色人格解耦。
- 设置入口统一进入 Header，团队配置并入设置页；移除对话顶部 Agent 参与状态条。
- 真实 MCP turn 只接受 `publish_message` 作为权威角色消息，CLI stdout 降级为调试信息。
- 默认提交配置保持真实 Provider 关闭，避免首次启动产生模型费用。
- 前端构建链升级到 Vite 8、React 插件 6、Vitest 4 和 TypeScript 5.9，清除发布检查发现的 npm high/critical 漏洞；最低 Node.js 版本调整为 `20.19+` 或 `22.12+`。

### Removed

- 从提交配置、角色 fallback、onboarding、Provider 状态和 smoke 路径中移除 Gemini CLI。
- 从用户界面移除 Fake CLI 命名，内部适配器统一展示为“演示模式”。

### Safety

- Codex 和 Claude MCP 配置使用临时隔离目录，不写入用户全局配置。
- Antigravity 全局配置例外保留锁、sentinel 恢复和精确还原语义。
- 归档会话只读且不删除历史数据；恢复后方可继续创建任务。
- 项目采用 MIT License，并提供无 Git 历史的白名单发布导出与隐私检查流程。
