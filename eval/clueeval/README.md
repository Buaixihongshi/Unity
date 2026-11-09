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
- 每次调用都必须无工具。三家 CLI 都改成逐条输出事件，`cli_responder` 逐条检查：出现工具事件，或者无法确认没用工具（缺 init/result 事件、退出码非 0、空回复、Antigravity 日志里看不到禁用规则生效），这次调用就记为无效，附上 `invalid_reasons`。CLI 超时或启动失败（如命令不存在）同样记为无效调用，只留错误类型、errno 和 stderr 末尾，不记异常原文里的路径；其他应答器异常按 `responder raised <类型>` 记。这一局随即停下，记录 `valid=false` 和原因后写盘，批次继续跑下一局；`score` 不计入，单独列在 `invalid_games` 里。
- 每次调用都会记录初始上下文里实际找到了哪几张卡（`cards_in_initial_context`）。找到的卡和本席位分到的卡不一致时，CLI 启动前就会报错停下。别人的私卡只能经由讨论记录、在持卡人发过言之后才出现（`cards_in_transcript`）。

## 协议

- 条件：`private`（每席一张私卡讨论）、`shared_only`（只给共享材料）、`solo_full`（单人基线：一个席位拿共享材料加全部私卡，只答一次，不讨论）。`solo_full` 会让路由里的每个角色各自单独作答，自制题用 solo-v4 全材料条件的【补充材料】写法，HiddenBench 题把全部条目接在共享列表后面。
- 席位的推理强度取路由里该角色当前 provider 的 `provider_efforts`：Claude 传 `--effort`，Codex 传 `-c model_reasoning_effort=…`；Antigravity 模型名自带强度，不另外传参。
- 每次调用记录 CLI 自报的 token 计数（`meta.usage`）；只有 Claude 会报费用（`meta.cost_usd`），其余记为缺失。`score` 的 `calls` 按局、按 provider 汇总调用数、无效调用数、调用耗时、token 和费用。
- **结论层级**：这是直接调用各 CLI 的固定轮流讨论，不经过 Unity 的编排（角色人设、publish_message、invoke_agent、记忆）。它能回答“这几个 CLI 按日常路由和参数、在结构化讨论里拼私卡有没有收益”，不能直接代表 Unity 聊天室里的协作表现。manifest 记为 `harness_layer=direct-cli-round-robin`。

- 讨论前，每人先独立作答；然后按轮次轮流发言，默认 3 轮；最后每人独立作答。每次调用都是无状态的：拼上完整的讨论记录，在临时空目录里运行，不开工具，也不续接 CLI 会话。
- 私卡分配和起始发言人都随局号轮换。
- 自制题沿用 SPEC 的做法，告诉参与者“队友手里的材料可能和你不同”。HiddenBench 题按论文 §3.1 不作告知：私卡直接追加在共享材料列表的末尾，不单独标注；最终答案用 `{"vote", "rationale"}` 格式的 JSON。
- 席位按日常角色路由入座：楚岚、宝宝、王也，第四席是陆玲珑。模型取自 `--routing` 指定的 hub_settings；产物只记录摘要，不记录路径。

## HiddenBench 来源

- 数据集：`YuxuanLi1225/HiddenBench`，MIT License，作者 Yuxuan Li 等。论文：arXiv:2505.11556。
- 修订版本 `1e3c25b1fd798c6717f4df0463edd3825c8e37f9`。`benchmark.json` 的 sha256 为 `2815afffca4e470d1dfbc81e625160447df1109ce371968181c9e1e6b90443a3`，题号 9。
- 原始条目的规范 JSON 摘要记录在 `origin.item_sha256`。题面与答案拆开存放，测试会把两部分拼回原条目，再核对这个摘要。

## 已知限制

- CLI 会自己加载用户级指令文件，这部分不在我们拼的提示里：Codex 读 `~/.codex/AGENTS.md`，Antigravity 读 `~/.gemini/GEMINI.md`，Claude 读 `~/.claude/CLAUDE.md`。所以“初始上下文只有材料”只对我们构造的那部分提示成立。manifest 会记下这几个文件的摘要（`cli_global_instructions_sha256`），对比不同批次时要先核对摘要是否一致。Codex 的 `project_doc_max_bytes=0` 屏蔽不了全局文件，实测确认过。
- 各家禁用工具的做法（#140，均已用“读当前目录 canary 文件”的探针实测）：
  - Claude：`--tools ""`，init 事件里工具和 MCP 列表都为空，回合数为 1。
  - Codex：`-s read-only` 只限制写入，不禁命令；改前实测能执行 `ls` 并读出 canary。现在用 `--disable` 关掉 shell、unified_exec、code mode 等能执行代码或访问外部的功能，并关掉 web_search 和 MCP。关掉 code mode 后，每次调用开头固定有一条 `error` 通知，这一条按原文放行；除此之外，凡是不属于 `agent_message`/`reasoning` 的条目，都判为无效。
  - Antigravity：没有关闭工具的开关，日常设置还是 `always-proceed`；改前实测会执行 `ls`、`read_file` 读出 canary。现在用 `--settings` 临时改成 `strict`，并拒绝命令、读写文件、MCP、网址和浏览器。实测主代理和它拉起的子代理都被拦下。即使工具被拦，只要出现工具或子代理步骤，这一局照样判无效。
- 以上是用禁用加事件检测兜底，不是文件系统隔离：答案文件仍在本机磁盘上。CLI 版本升级后，新增的工具未必在禁用清单里；靠的是“没见过的事件一律判无效”来兜底，升级后要先重跑探针。
- `answer_key_opened_by_harness` 只说明脚本自己跑题时没打开答案文件，不是对 CLI 子进程的访问审计。
- #140 之前的产物（如 `smoke-real-1`）没有 `valid` 字段，`score` 会全部列为无效。那批 Antigravity 的 `tool_calls_in_log=0` 不能作为没用工具的证据：实测表明，即使真的读了文件，这个正则也计 0。
- 这里只把首行或 JSON 里的选项解析成 pick，不算排除分和披露率。那部分判分继续由人工确认（见 #129）。
