# 代码评审考试：考前准备

用历史 PR 考"找 bug"：考生评审当初引入 bug 的那个 PR，交一张固定格式的答卷，脚本对私有答案表判分。
本目录只有考场和答卷规则，**还没有题目、答案表和任何模型成绩**（`model_calls=0`）。

## 考场

`backend/app/evaluation/exam_room.py`，仅 macOS（`sandbox-exec`）。

- 考场目录：`repo/` 是被评审 PR 合入后的代码（全新单提交历史、无远端），`pr.diff` 是该 PR 的改动，
  `TASK.md` 是考生须知，`out/` 可写。答案表不进考场。
- 基线：沙箱默认全部拒绝，只导入系统自带的 `system.sb`（系统库和少量系统 Mach 服务）。
  剪贴板、钥匙串、LaunchServices、WindowServer、TCC 等宿主服务都查不到（#94）。
- 文件：只能读考场、`RUNTIME_READ` 里的工具链路径（`/usr/bin`、命令行工具等，不含 `/usr/local`、`/opt`）
  和调用方显式传入的运行依赖；这些路径之外连 stat 都不行，只对它们的上级目录放开元数据（#95）。考场外禁止写入。
- 网络：除一个本地代理端口外全部禁止出站。代理只放行白名单主机的 443 CONNECT，
  GitHub 和本机 Unity 服务（8000/8100/8200/8300）都会被拒绝。代理自己解析一次域名，
  任一结果不是公网地址（回环、私网、链路本地、映射到这些地址的 IPv6 形式）就拒绝，
  否则直接连检查过的那个 IP，不再二次解析（#96）。
- 进程与环境：`ps` 和 sysctl 进程表（`kern.proc.*`）都看不到其他进程；`process-info-pidinfo` 只允许查自己，按 PID 读其他同用户进程的命令行和环境（`KERN_PROCARGS2`/`KERN_PROCARGS`）会返回 EPERM（#97）。这条不能整项拒绝，否则 `python3`、`git` 启动即崩溃（-5）。环境变量从零构造，`HOME`/`TMPDIR` 指向考场，不带令牌和 `YIREN_*`。
- 进考场前用 `scan_canaries` 扫一遍考场，修复提交号、修复标题等出现即视为泄题。

## 查考场

`backend/app/evaluation/exam_probe.py` 是假考生，逐项尝试偷看：翻 Git 历史、搜修复标记、读和 stat 源仓库/答案表/
记忆/会话记录/日常数据库、直连和经代理访问本机服务与 GitHub、调 `gh`、看进程、找令牌；
查找剪贴板/钥匙串等宿主 Mach 服务，运行 `pbpaste`、`security list-keychains`、`defaults read -g`
（只记退出码和字节数，不输出内容）；读 sysctl 进程表，并按 PID 读一个合成宿主进程的参数和环境（只记是否命中合成标记）。`probe` 命令还会在 `/private/var/tmp`、`/private/tmp` 和用户临时目录
各放一个合成答案文件，并在考场外启动一个参数和环境里带合成标记的同用户进程，结束后都会清理。
同时有正向对照：必须能读 `repo/`、`pr.diff`，能在 `out/` 写文件，能 `git log`，能连上代理。
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
- 真实 CLI 需要的额外读取路径（如 Node 安装目录、证书）和 Mach 服务还没确定，接入时要逐项开放并重跑查考场。
  若 CLI 需要查询自己子进程的信息，`process-info-pidinfo` 可考虑放宽到 `(target same-sandbox)`，但并发考场之间是否互相可见尚未验证。
- 沙箱内调用 `/usr/bin/git`、`/usr/bin/python3` 会在 stderr 打印 xcrun 无法写缓存的提示，不影响结果。
