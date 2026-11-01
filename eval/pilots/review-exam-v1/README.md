# 代码评审考试：考前准备

用历史 PR 考"找 bug"：考生评审当初引入 bug 的那个 PR，交一张固定格式的答卷，脚本对私有答案表判分。
本目录只有考场和答卷规则，**还没有题目、答案表和任何模型成绩**（`model_calls=0`）。

## 考场

`backend/app/evaluation/exam_room.py`，仅 macOS（`sandbox-exec`）。

- 考场目录：`repo/` 是被评审 PR 合入后的代码（全新单提交历史、无远端），`pr.diff` 是该 PR 的改动，
  `TASK.md` 是考生须知，`out/` 可写。答案表不进考场。
- 文件：禁止读取 `/Users`、`/Volumes`、`/private/tmp`、`/private/var/folders` 下考场以外的内容
  （源仓库及其 Git 对象、答案表、CLI 记忆与会话记录、日常数据库都在其中）；考场外禁止写入。
- 网络：除一个本地代理端口外全部禁止出站。代理只放行白名单主机的 443 CONNECT，
  GitHub 和本机 Unity 服务（8000/8100/8200/8300）都会被拒绝。
- 进程与环境：看不到其他进程；环境变量从零构造，`HOME`/`TMPDIR` 指向考场，不带令牌和 `YIREN_*`。
- 进考场前用 `scan_canaries` 扫一遍考场，修复提交号、修复标题等出现即视为泄题。

## 查考场

`backend/app/evaluation/exam_probe.py` 是假考生，逐项尝试偷看：翻 Git 历史、搜修复标记、读源仓库/答案表/
记忆/会话记录/日常数据库、直连和经代理访问本机服务与 GitHub、调 `gh`、看进程、找令牌。
同时有正向对照：必须能读 `repo/`、`pr.diff`，能在 `out/` 写文件，能 `git log`。
同一考场再不加沙箱跑一遍作为对照，证明这些检查确实能发现泄漏。

```bash
python scripts/review_exam.py probe --base <PR 父提交> --head <PR 合入提交> \
  --canary <修复提交号> --canary "<修复提交标题>" --answer-key <答案表路径> \
  --forbid daily_db=<日常数据库路径> --output output/eval/review-exam-probe
```

`room_ready=true` 要求：沙箱内全部检查符合预期，且考场预扫没有命中泄题标记。

## 答卷与判分

答卷格式见 `TASK.md`，解析与判分在 `backend/app/evaluation/review_exam.py`：

- 只接受裸 JSON 或恰好一个 JSON 代码块；格式不符整份作废，按全部漏报计，不当作"无问题"。
- 一条发现最多跨 30 行，最多 20 条，防止大范围撒网。
- 文件相同且行号区间与答案位置相交（默认容差 ±5 行）算位置命中；同一颗雷再报算重复，不算误报；
  对不上任何雷的都是误报，干净卷上的每条发现都是误报。
- 位置命中不代表理由正确：`judge_queue` 把命中项打乱、去掉组别，交给不参赛的判卷方回答
  same / different / unclear，组别映射单独保存。

```bash
python scripts/review_exam.py grade --sheet answer.txt --key key.json
```

## 还没做的

- 真实 CLI 进考场：Claude/Codex 的启动参数、只读工具集、认证在沙箱内能否工作、代理白名单主机，
  都还没接，也没有用真实模型验证过。
- 题目与答案表、分组跑批、汇总统计。
- 跨平台：Linux 上没有对应实现，测试会跳过沙箱用例。
