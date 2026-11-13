# 开发环境、验收与检查入口

项目统一通过 `scripts/env.sh` 管理环境，通过 `scripts/check.sh` 执行检查。
development 默认使用真实 CLI 并继承日常角色路由、模型与参数；演示须显式 `--demo`。
test 与 CI/离线检查继续使用 fixture；alpha 继承日常 CLI 设置。启动、安装依赖和离线检查不会主动发起模型任务。

## 环境与端口

端口、热更新默认值的唯一来源是 `config/environments.json`。

| 环境 | 后端 / 前端 | 代码与数据 |
| --- | --- | --- |
| local（兼容一键体验） | 8000 / 5173 | 当前 checkout；保留旧版根目录 data/config/artifacts，不隐式搬家 |
| development | 8100 / 5174 | 每项任务独立 worktree；并行时从 8400/5180 起分配空闲端口 |
| test（兼容旧入口） | 8200 / 5175 | 显式指定的 checkout 与私有状态 |
| alpha（主线验收） | 8300 / 5176 | 独立、干净、detached 的 origin/main 镜像；CLI 配置继承日常，任务数据独立 |
| runtime（日常发布） | 8000 / 5173 | 保留给所有者发布；不能通过普通 start 命令启动 |

已有 8200/5175 旧测试服务及其中未提交修改继续保留。新 alpha 不占用旧端口，
也不会自动迁移旧数据库或密钥。当前日常服务不会因 PR 合并、sync 或检查而重启。

环境根目录默认在主仓库同级的 `agentclub-environments/`，通过 Git common-dir
定位，不依赖执行命令时的工作目录。可以显式设置 `AGENTCLUB_ENV_HOME` 改变位置。
新功能位于 `features/<task>/`，alpha 位于 `alpha/`，不接管同名的陌生目录。

## 常用命令

```sh
./scripts/env.sh status
./scripts/env.sh status --json
./scripts/env.sh dev composer
./scripts/env.sh dev composer --detach
./scripts/env.sh start development --checkout /absolute/path/to/candidate --detach
./scripts/env.sh stop development --checkout /absolute/path/to/candidate
./scripts/env.sh dev offline-demo --demo --detach
./scripts/env.sh stop development --checkout /absolute/path/to/offline-demo --demo
./scripts/env.sh alpha sync
./scripts/env.sh alpha start --detach
./scripts/env.sh alpha status
./scripts/env.sh alpha stop
```

`dev` 从刷新后的 origin/main 创建 `codex/task-<task>` 分支。再次调用同名任务复用
原分支、文件和数据，不 reset。合并前验证工具本身时可显式传 `--ref <candidate-ref>`；
该候选环境仍叫 development，不能冒充主线 alpha。PR 2 合并前，旧 main 没有新
生命周期接口，启动器会明确拒绝将它当作已接入的受管环境。

首次启动检查 Python 与 Node、建立各 checkout 的 .venv、安装锁定依赖。开发机推荐
Python 3.12、Node 22（仓库提供 .python-version/.nvmrc）；拒绝旧于 Python 3.11 的
现有虚拟环境，不悄悄回退到系统 Python 3.9。`--no-install` 只使用已就绪依赖。

默认前台运行；Ctrl-C 后先等现有任务完成，再停止自己启动的进程组。`--detach`
生成带实例 ID 和进程出生时间的本地记录。`stop` 校验实例、目录及 supervisor 出生时间，
通过私有令牌执行 drain，拒绝有运行/待执行任务、等待输入或未结束的 V2 讨论、活跃授权或维护任务时停止。
普通任务的历史 blocked 属于停止终态，保留记录且不妨碍切换；其仍有活跃执行授权时仍拒绝停止。
不按端口 kill 陌生进程，不用 stale PID 文件杀进程，不提供绕过活跃任务的 force 停止。
端口冲突直接报错，只有 `dev` 的默认端口会自动分配；显式端口不会被偷偷改写。

### 空闲回收与并行上限

每个 development 环境都是一套完整后端（多数还带 Vite），由 Agent 用 `--detach` 启动后不会随回合结束。
为避免环境在宿主上越积越多，development 默认按 `config/environments.json` 回收与限流：

- `idle_stop_minutes`（默认 120）：后端连续这么久没有用户请求、且没有未完成工作时，supervisor 走与
  `stop` 相同的私有令牌 drain 后自行停止，记录 `stopped_reason: "idle"`。drain 返回 409（期间出现新工作）
  时继续运行，稍后再判断。页面可见时每 30 秒的健康刷新算作使用；supervisor 与 `status` 的探测请求带
  `X-AgentClub-Probe`，不算使用。`--idle-stop MINUTES` 可改单个实例，`--idle-stop 0` 关闭；alpha/test 默认不回收。
  后端早于此功能的 `--ref` 候选不报告空闲时间，supervisor 会在日志中说明并保持运行。
- `max_running`（默认 4）：已有这么多受管 development 实例存活时，新的 `dev`/`start development`
  直接报错并列出可执行的 `stop` 命令，不自动停止任何实例；确需更多时显式设置 `AGENTCLUB_MAX_RUNNING`。

用完的环境仍应主动 `./scripts/env.sh stop development --checkout <path>`；空闲回收只是兜底。
开启 reload 时，后端还会排除 checkout 下的 `output/`（打包产物含完整 Python），避免打包触发反复重载。

alpha sync 只更新干净且未运行的受管 alpha；未提交修改、陌生目录和正在监听的服务均
阻止同步。alpha start 在停止状态下先同步主线；已运行时只复用，不自动更新或重启。
alpha 不开启 reload。功能开发开启后端 reload，前端代理地址随实际后端端口统一下发。

## 状态与真实 Provider 隔离

受管环境状态放在各 checkout 的 `.yiren/<profile>/`，包含 config、data、artifacts、
maintenance-worktrees 和 runtime（日志、进程记录、私有控制令牌）。角色路由、hub_settings、
SQLite 及 `data/connection-secrets/` 都随环境隔离；不复制日常聊天和 API 密钥作为种子。
目录权限为 700，控制文件为 600。环境变量中的旧 YIREN 路径不会意外串入新环境。

`dev` 与 `start development` 默认使用独立的 `.yiren/development-real` 状态，继承日常 CLI 配置。
明确关闭演示开关与 fake Provider，不把角色统一成 Codex。日常配置缺失、路由为演示或 API 连接时明确报错，
不静默回退演示。未运行的旧 `.yiren/development` 演示数据仍保留；停止旧演示需传 `--demo`。
`--demo` 使用该独立演示状态，不能同时继承或指定真实 Provider。单 Provider 专项测试保留显式入口：

```sh
./scripts/env.sh dev team-validation --detach
./scripts/env.sh start development --real-cli --providers codex,claude --detach
./scripts/env.sh stop development
```

development 的默认模式与 alpha 每次停止后重新启动时，从 `runtime-state` 读取有效 CLI 设置，包含 Provider 命令参数及启用状态、各角色的主用/备用连接、CLI 模型和运行超时。development 强制真实模式，alpha 保留日常模式设置。alpha 沿用 `.yiren/alpha` 的独立数据，`--real-cli` 不另建一份空的 alpha-real 状态。日常修改后，重启才更新快照；已运行实例不会静默重启或自动同步中途配置。运行记录标明 `cli_config_source=runtime`。

只复制 CLI 配置字段，不复制日常数据库、聊天、API connections、key_ref 或登录凭据文件。角色使用非 CLI 的 API 连接、角色 ID 不对应或日常配置缺失时，明确拒绝继承，不替换成另一模型。日常配置作为只读来源，alpha 的设置修改不写回日常；维护执行继续关闭。

development 已获用户授权进行真实功能验证，按日常路由执行；自动化离线检查不受此默认值影响。显式 development --demo / 默认 test 环境在读取保存设置及每次任务快照后
仍强制使用演示适配器，并拦截真实验证/探测，不能被旧设置中的 use_fake_cli=false 放开。
local 与 runtime 默认使用真实 CLI（`use_fake_cli=false`），保留用户在设置页或启动引导中完成的 Provider 接入；未接入前角色会明确报告 Provider 未启用，不回退演示。

旧脚本 start.sh、start-isolated.sh、dev-backend.sh、dev-real.sh 保留为新入口别名。
start.sh 保留 local 的旧数据位置；dev-backend.sh 默认改用开发端口 8100 并隔离数据。
只有显式传入 --reset-onboarding 才重置引导，且先校验目标实例；日常启动不清理旧状态。

## 版本识别与停止协议

`GET /api/health` 的 runtime 保留 PR 1 的 started/current/source_changed，并新增
实例 ID、日志路径、activity 计数与 draining 状态，响应禁止缓存。started 是后端模块
加载时冻结的源码身份，current 是磁盘当前身份；同 SHA 的未提交代码也通过摘要区分。
前端在启动/构建时记录自己的身份，`/__environment` 随生产产物保存。页面对照前后端
版本与目录，不能把当前 Git HEAD 当成长期运行进程的版本。

`POST /api/runtime/drain`、`POST /api/runtime/resume` 只接受本实例私有
X-AgentClub-Control 令牌。普通写请求保持并发；drain 先暂关新写入口，等待已接入请求
完成，再确认没有活跃工作并持续拒绝写入（503，带 Retry-After），读取仍可用。
活跃工作判定统一在 backend/app/runtime_activity.py，在线 drain 与离线迁移共用；
缺失 V2 状态证据时按未结束处理。存在工作返回 409，且继续接受请求；缺少/错误令牌返回 403。令牌不出现在健康信息、
前端构建或 PR 中。重载后的 worker 仍属于同一实例，停止不依赖旧 worker PID。

status 是只读盘点，不安装、同步、重启或清理；额外 clone 用 --checkout，额外端口用
--ports backend:frontend。它读取固定环境端口及受管实例登记，不全盘搜索用户文件。
lsof/ps 缺失时监听进程证据可为空；旧服务缺少启动身份时如实显示未知。
完整绝对路径、日志和本地 JSON 报告不能直接上传公开 issue。

## 本地与 CI 共用检查

```sh
./scripts/check.sh backend
./scripts/check.sh frontend
./scripts/check.sh smoke
./scripts/check.sh eval
./scripts/check.sh privacy
./scripts/check.sh hygiene
./scripts/check.sh audit
./scripts/check.sh all
./scripts/release-check.sh --real-cli
```

唯一命令清单在 scripts/checks.py。backend 包含 pip check 和后端全量测试；frontend
包含前端测试、类型检查和生产构建；smoke 包含引导、连续会话、多 Agent 和圆桌四项离线
检查；eval 使用 fixture。all 包含以上全部及 npm audit，网络不可用时不能假装审计通过。
只有 --real-cli 会追加真实模型验收。CI 调用相同分组，保留 Backend tests 与
Frontend tests and build 检查名称，并支持 codex/** 作为连续 PR 的基分支。

隐私检查在开发/CI 中扫描 tracked 及非忽略候选文件，不把正常本地 runtime 目录当成
发布内容；直接扫描导出目录仍使用原有严格模式。报告默认保存在系统临时目录，也可
--output 指定私有位置；记录 SHA、源码摘要、分组、退出码和起止时间。验证期间源码或
SHA 变化时整份报告失败，不能将混合版本的绿色结果用于交付。

## 合并与日常发布

不再要求 Owner release workflow 或 Owner approval 状态。合并仍需当前 head 的两项必需 CI、
适用审阅和分支保护；Agent 不自行合并。新增提交重新运行检查并核对受影响审阅。
合并与激活日常服务分开，日常切换仍需所有者明确授权、任务空闲，并保留准确前版及恢复记录。

初始盘点见 [PR 1 清单](plans/2026-10-22-environment-inventory.md)。PR 2 不删除其中任何目录。
