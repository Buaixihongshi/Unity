# 代码评审任务

你是这次改动的评审者。当前目录里有：

- `repo/`：改动合入后的完整代码（你要评审的版本）；
- `pr.diff`：本次改动相对合入前的差异；
- `out/`：可写的草稿目录。

请像正常 PR 评审一样阅读 `pr.diff`，必要时到 `repo/` 里查看上下文，找出这次改动引入的缺陷
（行为错误、崩溃、数据或安全问题）。代码风格、命名和"可以更好"的建议不算缺陷，不要报。

这次改动也可能没有缺陷。没有把握的不要报：误报和漏报都会扣分。

## 交卷格式

最终回答只包含一个 JSON 对象（可以放在一个 ```json 代码块里），不要附加其他 JSON：

```json
{
  "verdict": "issues",
  "findings": [
    {
      "file": "backend/app/example.py",
      "line_start": 120,
      "line_end": 124,
      "summary": "一句话说明错在哪、会导致什么后果"
    }
  ]
}
```

- `verdict`：发现缺陷写 `"issues"`；认为没有缺陷写 `"no_issues"`，此时 `findings` 必须是空列表 `[]`。
- `file`：相对 `repo/` 的路径，例如 `backend/app/example.py`，不要带 `repo/` 前缀。
- `line_start` / `line_end`：`repo/` 中当前文件的行号（不是 diff 里的行号），一条最多跨 30 行。
- `summary`：不超过 500 字。
- 最多 20 条。同一个缺陷只报一次。
- 只允许以上字段；格式不符的答卷整份作废。
