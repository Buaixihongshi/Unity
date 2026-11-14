# 审批、发布与恢复

本工具将“写好代码”“准许合并”“合入 main”“激活日常实例”分开记录。
Agent 可准备和验证候选；实际数据导入、activate、rollback、recover 仍需所有者明确执行或授权。
共用账号凭据无法技术上区分人和 Agent，这一边界需要独立凭据，不能由本地批准字段伪造。

## 1. 当前 head 的检查与合并

```sh
./scripts/release.sh prepare --pr 29
./scripts/release.sh gate status
```

prepare/status 只读。prepare 读取当前 PR head、基分支、两项 Actions CI 和审阅就绪状态；
此结果不代表已审阅、已合并或已发布。合并遵循适用审阅、对话解决与 GitHub 分支保护。
新增提交需重新运行 CI，并核对受影响的审阅。Agent 不自行合并或启用自动合并。

所有者已取消单独的 Owner release / Owner approval 门禁；不再要求手动运行审批 workflow。
针对仍保留旧检查的仓库，在所有者明确授权后可运行：

```sh
./scripts/release.sh gate remove-owner
```

该命令只移除现有主线规则中的 `Owner approval`，保留两项 Actions 来源的 CI、strict 更新、
PR、其余保护和 bypass 设置，并回读确认。保护结构异常时拒绝覆盖；没有重新安装旧检查的入口。
旧 workflow、批准辅助程序及相关活跃指引一起移除，历史报告中的既有批准证据原样保留。

连续 PR 先合并依赖，将下一 PR 改基到 main，处理 squash 差异并重新检查/审阅。

## 2. 准备准确的已合并候选

```sh
./scripts/release.sh stage --sha <完整的已合并提交SHA>
./scripts/release.sh status
```

stage 验证提交位于当前公共 main 历史中，找到对应的实际合并 PR，并检查该 PR head 的两项
Actions CI 成功。生成可追溯链：source_pr → verified_head_sha / ci → merge_commit_sha → deployed sha。
不要求 Owner approval，也不生成虚构批准。直接推送或无法映射到实际合并 PR 的提交不能冒充候选。

候选在环境根目录 releases/<sha> 的独立 detached worktree 安装、完整检查和生产构建。
前端按 runtime 标签构建并保留构建时身份；manifest 记录源码摘要、完整检查报告和所有
dist 文件校验值。报告或产物变动、检查缺项、dirty checkout 均阻止激活；复用候选先复核
这些事实，不重编译正在运行的版本。stage 不启动或停止日常服务。

所有本机路径、检查报告、connection-secrets、备份、current.json、transition.json 和
release-history 都留在私有环境根目录，不提交到仓库。

## 3. 首次状态准备与日常切换

已有服务不得用空状态替换。首次迁移按以下顺序由所有者操作：

1. 在旧界面处理完运行/等待任务，确认 CLI 与维护工作结束，停止原启动终端。
2. import-state 检查旧 checkout 不再监听、数据库没有未完成工作，再以 SQLite backup
   获取一致快照，复制设置、连接密钥、角色配置和产物；保留原目录与原始数据。
3. 核对导入内容及 stage 候选，再明确激活。原服务仍占用端口时工具拒绝，不按端口杀进程。

普通任务的历史 blocked 与现有调度器一致，视为已停止并原样保留，不必改写记录才能迁移。
等待输入、未结束的 V2 讨论、活跃执行授权及维护任务仍阻止迁移；缺少 V2 结束证据也拒绝。
在线 drain 与离线检查共用 backend/app/runtime_activity.py 的判定。

```sh
./scripts/release.sh import-state --from-checkout /absolute/path/to/old-checkout
./scripts/release.sh activate --sha <候选SHA> --expected-current none
```

默认导入旧 checkout 的 data/yirenhub.sqlite3、config、artifacts；自定义布局显式使用
--database、--config、--artifacts。来源必须是 Unity 数据库。复制不跟随产物中的目录
符号链接，不回写原库。导入以准备目录原子落位，不覆盖已有 runtime-state。
只有确实不需要任何旧数据的新实例才使用 `release.sh init --empty`。

后续切换必须给出实际当前 SHA，防止覆盖别人刚完成的发布：

```sh
./scripts/release.sh activate --sha <下一候选SHA> --expected-current <当前部署SHA>
```

工具重新核对来源与必需 CI/PR 主线保护，认证并 drain 当前受管实例，等待已接入写操作、
确认没有活跃工作；一致备份 SQLite、设置、密钥和产物后停止自己的 supervisor，启动新候选。
生产前端使用已验证的 preview 产物，保留同一 runtime-state；不会覆盖已保存的 Provider 配置。
新实例健康与前后端身份核对成功后，才原子更新 current.json 和历史记录。

新版本启动失败且能证明新进程已退出时，恢复快照并重启已验证前版；若新进程仍活着或
身份不明，记录 recovery_required，绝不在活跃写入者下恢复数据库。

## 4. 回滚与中断恢复

```sh
./scripts/release.sh rollback --expected-current <当前部署SHA> --restore-state
./scripts/release.sh recover --expected-current <current.json记录的SHA或none>
```

**rollback 同时恢复前一版本和发布前状态。** 它先保存当前状态的另一份一致备份，因此
发布后新增的聊天/产物不会被直接删除，但不留在回滚后的活动库中；可从保留的备份恢复。
必须显式 --restore-state，不自动假设旧代码兼容新数据。回滚使用已有、校验通过的候选
及原始来源记录，不要求网络可用或制造一次新批准。

transition.json 在暂停接入前即记录意图，随后记录快照、旧版停止、新版启动和提交阶段。
存在未完成转换时阻止新切换。recover 可重复检查当前实例：若目标已经正式提交且身份
一致则补齐记录；否则在确认归属、空闲和监听者已退出后保留当前状态、恢复快照与前版。
未知进程、busy 工作或校验失败会要求先处理具体问题，不能通过重新启动掩盖状态。

current/transition/history 的记录是操作证据，不是所有者授权。临时 fixture 测试不写真实
GitHub 批准，不使用日常端口或生产数据。备份与历史保留不自动清理；清理需要单独核对。旧候选 manifest 的批准字段可继续用于回滚记录，不会被改写为新格式或补造批准。

## 5. macOS 桌面更新

外壳嵌入固定版本、校验 SHA-256 的 Sparkle。更新源默认是主线
`packaging/macos/appcast.xml`；每天后台检查，默认静默下载，退出时安装。
“立即重启”先检查活动状态，再使用后端 drain 拒绝新写请求；忙碌或状态未知时拒绝。
手动退出与切换仓库在无法确认活动状态时仍要求用户确认。

正式签名密钥由所有者在本机生成、保存并备份，不提交仓库、不交给 CI。使用下载的
Sparkle 发行包内 `bin/generate_keys`，按工具提示生成密钥；只把公钥传入构建。
默认本地构建缺少公钥时明确禁用更新，不能当作已启用自动更新的发布版本。

```sh
SPARKLE_PUBLIC_KEY='<base64 公钥>' REQUIRE_UPDATES=1 ./scripts/package-macos.sh
```

对最终 DMG 使用同一发行包内 `bin/sign_update`，按其 `--help` 说明从所有者钥匙串签名。
把 DMG 上传到 GitHub Release 后，将工具输出的 `sparkle:edSignature` 和 `length`
连同 HTTPS 下载 URL 写入 appcast enclosure。`sparkle:version` 必须与包中
`CFBundleVersion` 相同，且比已发布版本递增；短版本号用于展示。私钥绝不写入 appcast。
包开启 `SUVerifyUpdateBeforeExtraction`，先验签再解包。签名失败保留现有应用。
丢失密钥后的恢复或轮换必须按 [Sparkle 官方安全说明](https://sparkle-project.org/documentation/security/)
单独制定，不能临时换公钥并宣称旧客户端仍可安全更新。

开发期验收使用以下入口，证据留在私有临时目录：

```sh
bash packaging/macos/bridge-tests/run.sh
bash packaging/macos/update-tests/run.sh
```

后者构建完整 arm64/本机架构包，以独立 bundle ID、一次性 Ed25519 密钥、回环 feed、
隔离 support 目录验证后台下载与退出安装、手动下载与重启、坏签名拒绝。
自动和手动场景各在包内后端执行真实 CLI 发言，继承日常 CLI 的角色路由、模型与参数；
不复制日常数据库、聊天或凭据。缺少真实认证/额度时必须报告失败，不能改用演示。
默认选择 baobao，专项验证可用 `ACCEPTANCE_AGENT` 选择其他已有角色，不改写其路由。
`OPENSSL` 可指定支持 Ed25519 的 OpenSSL 3 可执行文件；默认尝试 Homebrew 版本。
测试重启后的程序只记录版本标记并退出，避免环境变量未继承时启动日常数据目录。
这项验收不代替 Developer ID、公证、正式 feed 与另一台 Mac 的分发验收，也不授权发布。
