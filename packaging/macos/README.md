# macOS 桌面包（原型）

```sh
./scripts/package-macos.sh                      # ad-hoc 签名，产物在 output/macos/
SIGN_IDENTITY="Developer ID Application: …" NOTARY_PROFILE=<profile> ./scripts/package-macos.sh
```

需要 Xcode Command Line Tools、uv、Node（构建前端）。只打本机架构的包（arm64 或 x86_64）。

## 结构

| 位置 | 内容 |
| --- | --- |
| `Contents/MacOS/Unity` | `UnityApp.swift` 编译的外壳：选空闲回环端口、启动后端、WKWebView 窗口、退出确认 |
| `Contents/Resources/python` | uv 提供的可重定位 CPython 与运行时依赖；`unity-app.pth` 让任意 cwd 都能 `import backend` |
| `Contents/Resources/app` | backend（不含 tests）、config、frontend/dist、`build-identity.json` |
| `bridge-tests/` | 桌面桥回归套件（#168/#169）：真实 WKWebView 驱动外壳的处理器，macOS 本地跑 `./bridge-tests/run.sh`；CI 是 linux 无法承载 |

图标源文件是 `AppIcon.svg`（莫比乌斯双色缎带，配色取自前端的 ink / paper / sage），打包时由 `render-svg.swift` 渲染成各尺寸 PNG；网页 favicon 是同一图形的深色简化版 `frontend/public/favicon.svg`。

不用 PyInstaller：后端用 `sys.executable` 启动 MCP 服务器和看门狗，必须是真解释器。
不开 App Sandbox：后端要运行用户自己的 CLI 并读取其登录状态。

## 运行时位置

- 数据：`~/Library/Application Support/Unity/`（`UNITY_SUPPORT_DIR` 可覆盖），智能体工作区默认是其中的 `workspace/`
- 项目仓库：菜单“Unity › 选择项目仓库…”把所选 Git 仓库存入 UserDefaults（`ProjectDirectory`），外壳以 `UNITY_WORKSPACE` 传给后端并作为工作目录；切换会重启后端。仓库被移走时回退到 `workspace/`
- Workflow：任务工作树在 `task-worktrees/`；包内带 `scripts/environment_*.py` 与 `config/environments.json`（工作树锁与预览），打包时逐个导入校验
- 日志：`~/Library/Logs/Unity/backend.log`；字节码缓存：`~/Library/Caches/Unity/`
- PATH 取自登录 shell（`$SHELL -ilc`），并补充常见 CLI 目录
- 维护任务关闭（包内没有 Git checkout），强制真实 CLI

## 未签名包在其他 Mac 上

```sh
xattr -dr com.apple.quarantine /Applications/Unity.app
```
