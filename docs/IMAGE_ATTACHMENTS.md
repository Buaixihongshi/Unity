# 图片附件设计与实现约定

用户已确认限制；实现版本与验证状态见对应 PR。

v2 变更：补齐超限与失败契约（冯宝宝审查 P2×3）、前端交互与视觉（王也审查）。详见文末「v2 修订记录」。

## 目标与范围

- 用户可以在对话里粘贴、拖拽或选择图片，随消息或插话一起发出；成员能看到图，至少能明确知道自己看不到。
- 本期只支持静态位图：PNG、JPEG、WebP、静态 GIF。**动画 GIF 和动画 WebP 本期不支持**，上传时拒绝并说明原因，不做"只取首帧"。不支持 SVG、PDF 或其他文档。
- 不做：飞书通道、图片生成、成员回传图片（成员产出仍走 artifacts）、自动压缩/缩放/拆条。

## 1. 存储

**表**：新增 `attachments(id TEXT PRIMARY KEY, conversation_id TEXT, data TEXT)`。与 messages 一致，用 JSON 存放字段：

| 字段 | 说明 |
| --- | --- |
| `id` | `att_` + 随机 hex，与内容无关 |
| `conversation_id` | 归属会话；绑定消息和读取时都要校验 |
| `sha256` / `size` / `mime` | mime 以解码结果为准，不信任请求头 |
| `width` / `height` | 解码得到的像素尺寸，用于校验与前端占位 |
| `display_name` | 只用于展示，做过清洗，**不参与拼路径** |
| `message_id` | 上传后为空，发出消息时绑定 |
| `created_at` | |

**文件**：`<数据库所在目录>/attachments/<conversation_id>/<sha256>.<ext>`，同一会话内按内容去重。
不放在 artifacts 下：artifacts 是成员产出，会暴露给成员读写。删除会话时，连同它的附件目录一起删除。

**写入顺序**：流式写到同目录下的临时文件 → 校验全部通过 → 原子 rename 为正式文件 → 写表。任一步失败就删除临时文件，**不产生附件记录**。

**消息**：`Message.attachments: list[str] = []`。messages 表本身就是 JSON 存储，旧数据读出来默认空列表，不需要迁移。

## 2. 上限（精确值）

| 项 | 上限 | 依据 |
| --- | --- | --- |
| 单张原始字节 | **3,932,160 字节（3.75 MiB）** | base64 后为 5,242,880 字节，正好是 Anthropic 单图上限。该上限按 base64 计还是按原始字节计，目前是根据已知报错文本推断的，**待实测核实**；核实前取保守值 |
| 单张像素 | 宽、高各 ≤ 8000 px，且总像素 ≤ 40,000,000 | 8000 px 是 Anthropic 单边上限；总像素用来防解压炸弹 |
| 每条消息 | ≤ 4 张 | |
| 每次模型调用（每个 mission） | ≤ 8 张，base64 编码后合计 ≤ 16 MiB（16,777,216 字节） | 按每张 `4 × ceil(原始字节数 / 3)` 累计；不以原始字节和代替编码后载荷 |

前后端共用一份常量（后端 `attachments/limits.py`，前端从 `/api/config/attachments` 读取），不在前端写死 `5 * 1024 * 1024` 这类数字。

**为什么是"每 mission"**：投递规则是把当前 mission 里所有用户消息和插话的图交给本轮成员（见第 4 节），所以多次插话累积起来的图会进入同一次调用。预算按 mission 累计，在**提交时**校验，而不是等到投递时才发现超了、再悄悄丢掉几张。

## 3. 接口与错误契约

### 3.1 上传 `POST /api/conversations/{id}/attachments`

- 请求体直接是图片字节，`Content-Type` 写图片类型，可选 `X-Attachment-Name` 头。不用 multipart，省掉 `python-multipart` 依赖。
- 后端按流读取并计数，超过单张上限立即中断。前端预检只是为了体验，**后端校验才算数**。
- 校验顺序：字节数 → 魔数白名单 → 声明类型与魔数一致 → 有资源上限的真实解码。
  - 解码使用 Pillow（**新增依赖**，`requirements.txt` 固定 Pillow 13.0.0；打包脚本已经用 uv 安装运行时依赖，wheel 自带原生库，打包产物需要实测一次）。
  - 设置 `Image.MAX_IMAGE_PIXELS = 40_000_000`；先读文件头拿到尺寸，超限就直接拒绝，不进入完整解码；通过后再执行 `load()` 完整解码，以便发现损坏的数据。
  - 帧数 > 1 视为动图，拒绝。
- 成功返回 `201 {id, mime, size, width, height, display_name, url}`。

| 情况 | 状态码 | `code` |
| --- | --- | --- |
| 超过单张字节上限 | 413 | `ATTACHMENT_TOO_LARGE`（附带 `limit_bytes`） |
| 类型不在白名单、魔数与声明不符、SVG | 415 | `ATTACHMENT_UNSUPPORTED_TYPE` |
| 动图 | 415 | `ATTACHMENT_ANIMATED` |
| 解码失败（文件损坏） | 422 | `ATTACHMENT_DECODE_FAILED` |
| 尺寸或像素超限 | 422 | `ATTACHMENT_DIMENSIONS_EXCEEDED`（附带实际尺寸与上限） |

所有错误响应统一为 `{code, message, ...detail}`，并删除临时文件，不留附件记录。

### 3.2 读取 `GET /api/attachments/{id}/content`

返回存储时判定的 mime，带 `X-Content-Type-Options: nosniff` 和 `Content-Disposition: inline`；读取时校验会话归属。

### 3.3 发送 `MissionRequest` / `InterruptRequest`

- 增加 `attachment_ids: list[str]`。带了附件时 `goal`/`content` 可以为空。
- `revises_message_id` 修订消息时沿用原附件，也可以替换。
- 后端校验全部通过后才写消息、启动模型；**任何一项失败，整条消息都不发送、不启动模型**，前端保留草稿：

| 情况 | 状态码 | `code` |
| --- | --- | --- |
| 本条超过 4 张 | 422 | `ATTACHMENT_COUNT_EXCEEDED` |
| 附件不存在、不属于本会话或已绑定其他消息 | 422 | `ATTACHMENT_INVALID` |
| 加上本条后，mission 累计超过 8 张或 16 MiB | 422 | `MISSION_ATTACHMENT_BUDGET_EXCEEDED`（附带已用和上限） |

后端**不截断**：不会只取前 4 张，也不会丢弃超出预算的图。

### 3.4 清理

未绑定的附件超过 24 小时后，在启动时清理。只删除没有被任何消息引用的附件。

## 4. 投递：provider 能力声明

每个 provider 或连接声明 `image_input: native | path | none`，调度时按声明投递。不支持就明确降级，不悄悄丢图。

**投递哪些图**：当前 mission 里触发本轮的用户消息和插话所带的图片（受第 2 节的 mission 预算约束），交给本 mission 内每个被调度的成员，包括 A2A 接力的成员。历史消息里的图片**只在模型上下文里**不重复发送像素，写一行占位：`[图片 att_xxx，之前已提供]`；聊天界面照常显示历史图片（见第 5 节）。

| 路径 | 代码位置 | claude | codex | agy |
| --- | --- | --- | --- | --- |
| 普通对话 MCP 子进程 | `adapters/mcp_subprocess.py` | native：加 `--input-format stream-json`，prompt 和 image 块（base64）写入 stdin 后关闭 stdin | native：`[..., prompt, '-i', p1, '-i', p2]`，prompt 在前，`-i` 放最后 | path：prompt 末尾追加「附件（图片）：绝对路径」 |
| agy 常驻会话 | `adapters/antigravity_session.py` | — | — | path：同上。stream-json 输入已实测不收 image 块 |
| 讨论/任务沙箱 | `extensions/discussion/deliberation_cli.py` | native：同上；`--tools ""` 不影响 image 块 | native：同上 | path：**待实测**，读不了就降级为 none |
| API 直连 | `adapters/api_transport.py` | anthropic：`content` 改为 image 块 + text 块 | openai chat：`image_url` 填 data URL；responses：`input_image` | — |
| fake | `adapters/fake.py` | 记录收到的附件，供测试断言 | | |

**落盘位置**：讨论和任务沙箱禁止读取 `/Users`、`/tmp`、artifacts 和数据库目录，只有本轮临时目录可读。所以 CLI 路径统一做法是：每轮把附件**复制到本轮临时目录**，再引用临时路径，轮次结束后随临时目录一起删除。MCP 子进程和常驻会话也用同样的做法，保证几条路径行为一致，并且不需要把整个附件目录开放给 agy。

**agy 常驻会话的待实测项**：worker 启动时用了 `--add-dir <cwd>` 和 `--dangerously-skip-permissions`，临时目录在 add-dir 之外时它能不能读到，需要实测。读不到的话，就在 worker 启动时额外加一个固定的附件中转目录 `--add-dir`。实测通过前，这两条 agy 路径**不算已支持**。

**降级（none）**：prompt 里写「用户附了 N 张图片，你当前的连接看不到图片内容，请据此回答或请用户用文字描述」，同时记录 `attachment.degraded` 事件，前端显示降级标记（见第 5 节）。

**投递时的兜底**：提交时已经按预算校验过，正常情况下投递不会超限。如果上游仍以载荷过大拒收（例如上限实测比预期更严），如实报错并记 `attachment.delivery_failed`，不去图重试，不悄悄漏图。

**API 连接**：新增可选设置 `image_input`，默认 `native`。如果模型不支持视觉，上游会返回 400，这时如实报错，不自动去掉图片重试。用户可以把该连接改成 `none`。

## 5. 前端交互

输入框和插话输入框共用同一个附件组件。每个附件项的状态：

```ts
type AttachmentDraft =
  | { status: 'uploading'; localId: string; file: File; previewUrl: string }
  | { status: 'ready'; localId: string; id: string; url: string; size: number }
  | { status: 'error'; localId: string; previewUrl: string;
      reason: 'TOO_LARGE' | 'UNSUPPORTED' | 'INVALID_IMAGE' | 'UPLOAD_FAILED'; message: string };
```

**添加（粘贴/拖拽/按钮）**

- 选中后立即本地预检：`file.size > limit_bytes` 或类型不在白名单，直接进入 `error`，**不发请求**；通过预检的立即上传。后端返回的 413/415/422 也映射为 `error`。
- 错误项留在缩略图栏，显示为红框半透明卡片，用大白话写原因和实际数值，例如「超过 3.75 MB（实际 8.2 MB）」「暂不支持动图」「图片已损坏」，右上角带 `×` 移除按钮；`UPLOAD_FAILED` 额外提供「重试」。
- **出错不连坐**：已输入的文字和其他正常图片保持不动。

**数量超限**

- 一次加入的图片让总数超过 4 张时，按顺序收下前几张，补满 4 张为止，其余拦下，同时弹 Toast：「每条消息最多 4 张图片，已添加前 N 张，另有 M 张未添加」。被拦下的图明确告知，不悄悄丢掉。
- 已满 4 张时，附件按钮置灰；拖入时显示禁止光标，松手后不收图，只弹 Toast 提示。
- 如果 mission 预算已经用尽，附件按钮置灰并提示「本任务的图片已达上限（8 张 / 16 MB）」；发送时如果后端返回 `MISSION_ATTACHMENT_BUDGET_EXCEEDED`，同样提示，并保留草稿。

**发送门禁**

- 存在 `error` 项时发送按钮禁用，悬停提示「请先移除或重试标红的图片」。
- 存在 `uploading` 项时允许按发送：按钮转为 loading（「图片上传中…」），全部 `ready` 后自动发出；上传期间有任意一张失败，就取消这次自动发送，回到草稿态并标红那一张。
- 后端拒绝发送（422）时，草稿、文字和附件全部保留。

**消息中的显示**

- 气泡里显示缩略图，按 `width/height` 预留占位，避免加载时布局跳动。
- 点击缩略图在当前页打开轻量弹层查看大图（Esc 或点遮罩关闭，多图可左右切换），**不跳转新标签页**。
- 历史消息的图片在界面上照常显示；只有发给模型的上下文才用占位行代替像素。
- 降级：成员走 `none` 投递时，在其回复头部显示一个低调的灰色小标记「未看到图片」，悬停说明原因（例如「该连接不支持图片输入」）；不用红色报错样式，也不打断阅读。

## 6. 安全与记录

- 白名单以魔数和解码结果为准，不接受 SVG（可能带 XSS）。附件 id 随机生成，读取时校验会话归属，文件名不参与拼路径。
- 解码有资源上限（像素上限、先读文件头再完整解码），防止解压炸弹耗尽内存。
- TurnRecord 会记录 argv 和 prompt。claude 的 stdin 载荷要做脱敏，只记 `{type: image, attachment_id, sha256}`，不记 base64。codex 的 argv 里只有临时路径。
- 改动涉及存储、协议和调度，合并前请冯宝宝做非作者审阅。

## 7. 验证计划

- 离线（fixture），重点测边界值和边界值加一：
  - 上传：单张 3,932,160 字节通过，3,932,161 字节返回 413；魔数与声明不符、SVG、动图返回 415；截断或损坏的 PNG/JPEG 返回 422；8000×5000 通过，8001×1 和总像素超过 40,000,000 的返回 422；失败后临时目录里没有残留，表里也没有记录。
  - 发送：4 张通过，5 张返回 422，且没有写入消息、没有启动模型；跨会话或已绑定的附件返回 422；mission 累计第 8 张通过、第 9 张返回 422，16 MiB 边界同理。
  - 投递：每个 provider 的 argv 和 stdin 构造；三种 API 协议的载荷结构；降级事件；脱敏；历史图片只出现占位行。
  - 前端：预检拦截不发请求；一次加入 6 张时收下 4 张并弹 Toast；存在错误项时发送被禁用；上传中按发送会在上传完成后自动发出；上传失败会取消发送并保留草稿；422 后草稿保留。
  - 按 `scripts/checks.py` 运行对应分组。
- 真实 CLI（development，继承日常路由）：用左红右蓝探针图，让 claude、codex、agy 在普通对话、讨论两条路径上各跑一次，看是否答对颜色；agy 常驻会话单独再测一次。
- Anthropic 单图上限的计量口径：用 base64 前后分别略超 5 MiB 的图各发一次，确认按哪种口径计；如果按原始字节计，可以把单张上限放宽回 5 MiB。
- 打包：macOS 打包产物里 Pillow 能正常 import 和解码。
- 交付时记录 SHA、检查结果和预览入口，没测到的路径如实注明。

## 决策记录

用户已确认（2026-11-09）：

1. 单张上限先用 3.75 MiB（3,932,160 字节）；实测确认 Anthropic 口径后再决定是否放宽。
2. mission 预算每次调用最多 8 张。
3. 新增 Pillow 依赖，用于真实解码校验；打包产物需实测一次。

用户后续明确确认：

4. mission 合计字节预算沿用 16 MiB（计入 base64 膨胀），与 8 张并存，先触发哪条按哪条拒绝。
5. 本期不支持动图。
6. （沿用 v1）历史图片只在模型侧占位；上传使用原始请求体，不引入 multipart。

## v2 修订记录

- 冯宝宝 P2-1：补齐超限与失败契约（413/415/422 错误码表、整条消息不发送、草稿保留、临时文件清理），上限写成精确字节数，并补充边界值测试。
- 冯宝宝 P2-2：在魔数之外增加有资源上限的真实解码和像素上限；动图本期明确不支持。
- 冯宝宝 P2-3：新增每 mission 预算（8 张 / 16 MiB，已计入 base64 膨胀），在提交阶段校验；撤回"5 MB 能保证所有路径都能投递"的结论，改为保守值加实测核实。
- 王也：前端即时预检和错误卡片、超出 4 张时收下前几张并弹 Toast、上传中发送的门禁、当前页弹层看大图、历史图片在界面上照常显示、降级用低调标记。

## 协议参考

- [OpenAI 图片输入协议](https://developers.openai.com/api/docs/guides/images-vision)：Responses 使用 `input_image`，Chat Completions 使用 `image_url`，均用 data URL 传递图片。
- [Pillow 13.0.0](https://pypi.org/project/pillow/13.0.0/)：固定依赖版本；字节、尺寸和帧数限制由宿主校验。
