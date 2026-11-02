# Antigravity 会话并行：工作区插件替代全局配置锁

## 问题与范围

Antigravity CLI 早期版本（1.0.x）只从全局 `~/.gemini/config/mcp_config.json` 读取 MCP。
宿主因此每轮都改写这个文件、跑完再恢复，并用 `fcntl.flock` 和进程内唯一的
`AntigravitySessionPool.worker` 串行所有 agy 轮次。结果是：

- 不同会话里的 Antigravity 角色互相排队，前一轮跑测试几分钟，后一轮就等几分钟；
- 两个会话交替时互相淘汰常驻进程（`scope_changed`），每次都冷启动；
- 排队期间界面只显示"运行中"，点停止要等锁释放后才生效（实测等待 659.8 秒）。

Claude、Codex、Gemini 和讨论路径已经各自用临时配置，不在本次范围。

## 依据（本机 agy 1.4.12 实测）

- 工作区 `.agents/plugins.json`（`{"entries": [{"path", "include_only"}]}`）列出的本地插件，
  其 `mcp_config.json` 中的 MCP 服务在 `-p` 无界面模式下加载；写成数组时 agy 只记日志、静默忽略。
- 两个 agy 进程同时运行、各自工作区挂不同插件，各自只看到并调用自己的服务。
- 插件服务以 `<插件名>_<服务名>` 命名，经 `call_mcp_tool` 调用时 `ServerName` 为该名。
- 全局配置里已有同名服务时，插件服务被丢弃。旧版 Unity 实例运行期间仍会往全局写
  `yiren_hub`，所以插件命名为 `unity` / `hub`，模型看到的服务名是 `unity_hub`。
- `--add-dir` 目录下的 `.agents/` 也会加载，所以插件只写进每轮的临时工作区，不写进仓库。
- agy 同时加载全局配置里的服务。首轮实机验证中，旧版实例正在运行时注入了 `yiren_hub`，
  新实例的 agy 一并加载并选用了它，回复被写进旧实例数据库里正在运行的那一轮。

## 行为

1. `antigravity_config.write_workspace_plugin` 在临时工作区写插件与 `plugins.json`；
   常驻会话与一次性 `mcp_subprocess` 两条路径都改用它，不再改写全局配置。
2. 常驻池按"数据库、会话、角色、Provider、命令与参数"为键各保留一个 worker。同键轮次
   共用一个 stdin，继续串行；不同键并行。存活 worker 上限默认 4，满时先淘汰最久未用的空闲
   worker（`capacity_evicted`），全忙则排队。
3. 排队可见、可停：排队时写 `provider.activity`（`tool_name=worker_queue`），停止请求
   立即结束排队并返回 `stopped`；排队超过本轮预算返回 BLOCKED，本轮未发送。
4. Antigravity 提示追加 `host_rules.AGY_SERVER_NAME`，说明 `yiren_hub` 工具在 `unity_hub` 下；
   `turn_deadline` 同时识别两个服务名。
5. 与旧版共存：agy 运行期间（常驻 worker 存活期间、一次性进程退出前）对 `.yiren-mcp.lock`
   持共享锁。新实例之间互不阻塞；旧版每轮要独占锁，因此旧版轮次与新版 agy 互斥，新 agy
   启动时全局配置里不会有旧版的 `yiren_hub`。持共享锁时不可能有旧版在运行，残留哨兵只能
   来自被强杀的旧版，在启动 agy 前恢复。等待旧版时写 `worker_queue` 活动，停止请求立即生效。
   全部实例升级后，这把锁只剩新实例之间的共享持有，不再造成等待。
6. 保留 `recover_antigravity_global_config`：启动时非阻塞地恢复旧版残留。

## 兼容与风险

- 需要支持工作区插件的 agy（本机 1.4.12 已验证；最低版本未确认）。旧版 agy 不加载插件时，
  本轮表现为"未经 MCP 发布"的 BLOCKED，不会写错数据。
- 不再隐藏用户自己的全局 MCP 服务：它们会和 `unity_hub` 一起出现在 Unity 的 agy 轮次里。
  这是对原"运行期间只保留 yiren_hub"约定的放宽，合并前需所有者确认。
- 每个并行 worker 约占一个 agy 进程和一个 MCP 子进程；上限 4 控制资源。
- 过渡期内新旧实例的 Antigravity 轮次仍互相等待（旧版独占锁不设超时）；旧版一次性路径
  等待时不响应停止，这是旧版行为，升级后消失。

## 恢复

回滚本 PR 即恢复全局改写与单 worker 串行；两种方式不共享状态，回滚不需要数据迁移。
