# clueeval：私卡讨论题集

三道隐藏信息题。每个席位都能看到共享材料，另外各拿一张不同的私卡。

| 题目 | 席位 | 来源 |
|---|---|---|
| `q1-storage` | 3 | Unity 自制，solo-v4 第二轮单人筛选通过版，题面一字未改 |
| `q2-rootcause` | 3 | 同上 |
| `hb-009-critical_hospital_transfer` | 4 | HiddenBench 原题（见下文），保留原有人数和分卡 |

## 文件与隔离边界

- `questions.json`：只放题面、共享材料、私卡和候选项。加载时如果发现 `correct_answer`、`rationale` 这类答案侧字段，直接拒绝。
- `answer_key.json`：正解、诱饵和出题理由。只有 `run_clueeval.py score` 会读这个文件，跑题阶段不读。
- 每次调用都会记录初始上下文里实际找到了哪几张卡（`cards_in_initial_context`）。找到的卡和本席位分到的卡不一致时，CLI 启动前就会报错停下。别人的私卡只能经由讨论记录、在持卡人发过言之后才出现（`cards_in_transcript`）。

## 协议

- 讨论前，每人先独立作答；然后按轮次轮流发言，默认 3 轮；最后每人独立作答。每次调用都是无状态的：拼上完整的讨论记录，在临时空目录里运行，不开工具，也不续接 CLI 会话。
- 私卡分配和起始发言人都随局号轮换。
- 自制题沿用 SPEC 的做法，告诉参与者“队友手里的材料可能和你不同”。HiddenBench 题按论文 §3.1 不作告知：私卡直接追加在共享材料列表的末尾，不单独标注；最终答案用 `{"vote", "rationale"}` 格式的 JSON。
- 席位按日常角色路由入座：楚岚、宝宝、王也，第四席是陆玲珑。模型取自 `--routing` 指定的 hub_settings；产物只记录摘要，不记录路径。

## HiddenBench 来源

- 数据集：`YuxuanLi1225/HiddenBench`，MIT License，作者 Yuxuan Li 等。论文：arXiv:2505.11556。
- 修订版本 `1e3c25b1fd798c6717f4df0463edd3825c8e37f9`。`benchmark.json` 的 sha256 为 `2815afffca4e470d1dfbc81e625160447df1109ce371968181c9e1e6b90443a3`，题号 9。
- 原始条目的规范 JSON 摘要记录在 `origin.item_sha256`。题面与答案拆开存放，测试会把两部分拼回原条目，再核对这个摘要。
