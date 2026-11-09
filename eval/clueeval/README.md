# clueeval：私卡讨论题集

三道隐藏信息题。每个席位都能看到共享材料，另外各拿一张不同的私卡。

| 题目 | 席位 | 来源 |
|---|---|---|
| `q1-storage` | 3 | Unity 自制，solo-v4 第二轮单人筛选通过版，题面一字未改 |
| `q2-rootcause` | 3 | 同上 |
| `hb-009-critical_hospital_transfer` | 4 | HiddenBench 原题（见下文），保留原有人数和分卡 |
| `hb-009-critical_hospital_transfer-zh` | 4 | 上一题的中文译版 `zh-1`（作者自译，未经独立校对）；只换语言，事实、分卡、人数不变。成绩不能当英文原题成绩比较 |

## 文件与隔离边界

- `questions.json`：只放题面、共享材料、私卡和候选项。加载时如果发现 `correct_answer`、`rationale` 这类答案侧字段，直接拒绝。
- `answer_key.json`：正解、诱饵和出题理由。只有 `run_clueeval.py score` 会读这个文件，跑题阶段不读。
- 每次调用都必须无工具。三家 CLI 都改成逐条输出事件，`cli_responder` 逐条检查：出现工具事件，或者无法确认没用工具（缺 init/result 事件、退出码非 0、空回复、Antigravity 日志里看不到禁用规则生效），这次调用就记为无效，附上 `invalid_reasons`。CLI 超时或启动失败（如命令不存在）同样记为无效调用，只留错误类型、errno 和 stderr 末尾，不记异常原文里的路径；所有调用的 stderr 末尾都先把家目录换成 `~`、临时目录换成 `<tmp>` 再截取（#144）；其他应答器异常按 `responder raised <类型>` 记。这一局随即停下，记录 `valid=false` 和原因后写盘，批次继续跑下一局；`score` 不计入，单独列在 `invalid_games` 里。
- 每次调用都会记录初始上下文里实际找到了哪几张卡（`cards_in_initial_context`）。找到的卡和本席位分到的卡不一致时，CLI 启动前就会报错停下。别人的私卡只能经由讨论记录、在持卡人发过言之后才出现（`cards_in_transcript`）。

## 协议

- 条件：`private`（每席一张私卡讨论）、`shared_only`（只给共享材料）、`solo_full`（单人基线：一个席位拿共享材料加全部私卡，只答一次，不讨论）。`solo_full` 只支持中文题；所选题目里只要有不支持的题号/条件组合，`run` 在任何调用之前就整批拒绝，返回 2（#148）。`solo_full` 会让路由里的每个角色各自单独作答，自制题用 solo-v4 全材料条件的【补充材料】写法，HiddenBench 题把全部条目接在共享列表后面。
- 席位的推理强度取路由里该角色当前 provider 的 `provider_efforts`：Claude 传 `--effort`，Codex 传 `-c model_reasoning_effort=…`；Antigravity 模型名自带强度，不另外传参。
- 每次调用记录 CLI 自报的 token 计数（`meta.usage`）；只有 Claude 会报费用（`meta.cost_usd`），其余记为缺失。`score` 的 `calls` 按局、按 provider 汇总调用数、无效调用数、调用耗时、token 和费用。
- **结论层级**：这是直接调用各 CLI 的固定轮流讨论，不经过 Unity 的编排（角色人设、publish_message、invoke_agent、记忆）。它能回答“这几个 CLI 按日常路由和参数、在结构化讨论里拼私卡有没有收益”，不能直接代表 Unity 聊天室里的协作表现。manifest 记为 `harness_layer=direct-cli-round-robin`。

- 讨论前，每人先独立作答；然后按轮次轮流发言，默认 3 轮；最后每人独立作答。每次调用都是无状态的：拼上完整的讨论记录，在临时空目录里运行，不开工具，也不续接 CLI 会话。
- 私卡分配和起始发言人都随局号轮换。
- 自制题沿用 SPEC 的做法，告诉参与者“队友手里的材料可能和你不同”。HiddenBench 题按论文 §3.1 不作告知：私卡直接追加在共享材料列表的末尾，不单独标注；最终答案用 `{"vote", "rationale"}` 格式的 JSON。
- 席位按日常角色路由入座：楚岚、宝宝、王也，第四席是陆玲珑。模型取自 `--routing` 指定的 hub_settings；产物只记录摘要，不记录路径。

## 编排模式（`--mode orchestrated`）

- 同一批题、同一套提示与评分，但每个席位调用改经 Unity 编排执行：每局一个 mission，每次发言由 MissionRunner 调度为一次 agent 回合，座位 CLI 闭卷运行、只开放 yiren_hub MCP 工具（publish_message、log_thought、read_context、invoke_agent），回合、消息与用量落在本次运行独立的 `hub/eval.sqlite3`；发言内容取自聊天室里已发布的消息。manifest 记为 `harness_layer=unity-orchestrated`。
- 与直连批次的差异如实入 manifest：席位获得中性人格与宿主规则、共享会话里能看到彼此已发布的消息（提示内转写仍然保留，两份并存）；pre/final 回合在同一 mission 上串行执行（直连为并行，只影响墙钟）；费用为调用前预留的软门（`--limit-usd`，冻结 API 等价费率，不是硬上限、不是账单）。
- 座位按日常路由入座，claude、codex、antigravity 都支持。座位模型必须能按 `provider:模型` 在定价表里命中：cost-aware-v1 冻结表加上本目录的 `pricing.json`（补日常路由里那张表没有的 `claude:opus` 和 `antigravity:gemini-3.8-flash-high`，两表不许重复定义同一条）。未定价直接报错，不按 0 计费。`--a2a-budget` 大于 0 才开放 invoke_agent 接力。
- Claude 座位闭卷：`--tools ""` 去掉全部内置工具（文件、命令、联网、子 Agent），宿主再加 `--mcp-config` 和 `--strict-mcp-config`，只剩 yiren_hub；`--allowedTools` 只放行 hub 的四个工具，不用 `--dangerously-skip-permissions`；`--setting-sources ""` 不加载用户设置。每回合从回合记录里核对：记录必须完整，缺结束行、因 16 MiB 容量上限丢过行、读取时到了 20000 条上限，都算违规（#161）；init 事件只许列出 hub 工具、MCP 只许有 yiren_hub；内容块不管是 assistant 事件里的完整块，还是流式 partial 里开出来的块，都用同一张白名单，只许是文本、思考或 hub 工具调用（#162）；没记录到 init 也算违规。任一条违规，这次调用记为禁止，整局作废，批次停下。2026-11-09 实测（Claude Code 2.1.312，`opus` 解析为 `claude-opus-5-5`，effort max）：init 只列出 3 个 hub 工具（单人回合不开放 invoke_agent），读不到 canary 文件，也联不了网。
- Claude 的 `input_tokens` 不含缓存读写，账本把缓存读写一并按输入价计（codex、agy 的缓存本来就算在 `input_tokens` 里）。CLI 自报的 `total_cost_usd` 原样留在 `hub_calls.jsonl` 的 usage 里，可以对照。不打缓存折扣的估算不一定是上限：Claude 的 1 小时缓存写入单价高于普通输入，实测一次调用账本记 $0.139，CLI 自报 $0.141。
- 空闲超时沿用日常规则：座位自己的回合不设空闲超时，接力回合用 `a2a_idle_timeout`，所有调用仍受 `--cli-timeout` 硬上限约束。原先统一 45 秒，max 档的 opus 思考时不产生被计为活动的事件，实测思考到第 45 秒时被杀掉。
- 链路自检不调模型：`backend/tests/test_clueeval_orchestrated.py` 用假 CLI 走通全流程、私卡不串座、费用门熔断与无效发布判定。

## HiddenBench 来源

- 数据集：`YuxuanLi1225/HiddenBench`，MIT License，作者 Yuxuan Li 等。论文：arXiv:2505.11556。
- 修订版本 `1e3c25b1fd798c6717f4df0463edd3825c8e37f9`。`benchmark.json` 的 sha256 为 `2815afffca4e470d1dfbc81e625160447df1109ce371968181c9e1e6b90443a3`，题号 9。
- 原始条目的规范 JSON 摘要记录在 `origin.item_sha256`。题面与答案拆开存放，测试会把两部分拼回原条目，再核对这个摘要。

## 已知限制

- CLI 会自己加载用户级指令文件，这部分不在我们拼的提示里：Codex 读 `~/.codex/AGENTS.md`，Antigravity 读 `~/.gemini/GEMINI.md`，Claude 读 `~/.claude/CLAUDE.md`。所以“初始上下文只有材料”只对我们构造的那部分提示成立。manifest 会记下这几个文件的摘要（`cli_global_instructions_sha256`），对比不同批次时要先核对摘要是否一致。Codex 的 `project_doc_max_bytes=0` 屏蔽不了全局文件，实测确认过。
- 各家禁用工具的做法（#140，均已用“读当前目录 canary 文件”的探针实测）：
  - Claude：`--tools ""`，init 事件里工具和 MCP 列表都为空，回合数为 1。
  - Codex：`-s read-only` 只限制写入，不禁命令；改前实测能执行 `ls` 并读出 canary。现在用 `--disable` 关掉 shell、unified_exec、code mode 等能执行代码或访问外部的功能，并关掉 web_search 和 MCP。关掉 code mode 后，每次调用开头固定有一条 `error` 通知，这一条按原文放行；除此之外，凡是不属于 `agent_message`/`reasoning` 的条目，都判为无效。
  - Antigravity：没有关闭工具的开关，日常设置还是 `always-proceed`；改前实测会执行 `ls`、`read_file` 读出 canary。现在用 `--settings` 临时改成 `strict`，并拒绝命令、读写文件、MCP、读取/执行/下载网址。实测主代理和它拉起的子代理读文件都被拦下。即使工具被拦，只要出现工具或子代理步骤，这一局照样判无效。
  - **Antigravity 的联网搜索拦不住**（#145）：agy 1.5.3 的拒绝规则只认 `command`、`read_file`、`write_file`、`read_url`、`mcp`、`execute_url`、`download_url`、`unsandboxed` 这几类，别的条目会在日志里写一句 `ignoring invalid deny entry` 然后忽略。原清单里的 `browser(*)` 一直没生效，`search_web(*)` 也加不进去。联网 canary 实测：`search_web` 照常执行完成，只能靠步骤检测把这次调用判无效、整局作废。所以 HiddenBench 这类公网有原题的题目，Antigravity 席位一旦联网就会出现无效局；现在日志里只要出现被忽略的拒绝条目，调用也判为无效。
- 以上是用禁用加事件检测兜底，不是文件系统隔离：答案文件仍在本机磁盘上。CLI 版本升级后，新增的工具未必在禁用清单里；靠的是“没见过的事件一律判无效”来兜底，升级后要先重跑探针。
- `answer_key_opened_by_harness` 只说明脚本自己跑题时没打开答案文件，不是对 CLI 子进程的访问审计。
- #140 之前的产物（如 `smoke-real-1`）没有 `valid` 字段，`score` 会全部列为无效。那批 Antigravity 的 `tool_calls_in_log=0` 不能作为没用工具的证据：实测表明，即使真的读了文件，这个正则也计 0。
- 这里只把首行或 JSON 里的选项解析成 pick，不算排除分和披露率。那部分判分继续由人工确认（见 #129）。
