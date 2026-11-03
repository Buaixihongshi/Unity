// Native shell for the packaged app: starts the bundled backend on a free
// loopback port and hosts the frontend in a WKWebView window.
import Cocoa
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuItemValidation, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
    private static let projectKey = "ProjectDirectory"
    private static let zoomKey = "PageZoom"
    private static let minZoom: CGFloat = 0.5
    private static let maxZoom: CGFloat = 3.0
    private static let zoomStep: CGFloat = 0.1
    private static let mobiusPath = "M256,256C252.7,252.7 249.3,249.3 246,246C242.6,242.7 239.2,239.4 235.7,236.1C232.2,232.8 228.7,229.6 225.1,226.4C221.4,223.3 217.7,220.1 213.9,217.1C210,214.1 206.1,211.1 201.9,208.3C197.8,205.5 193.5,202.8 189,200.3C184.5,197.8 179.9,195.4 175,193.4C170.1,191.3 165.1,189.5 159.8,188C154.6,186.5 149.1,185.3 143.6,184.7C138,184 132.2,183.7 126.4,184C120.6,184.3 114.6,185.1 108.8,186.6C103,188.1 97.1,190.2 91.6,193.1C86.1,195.9 80.7,199.5 76,203.7C71.2,208 66.8,213 63.3,218.4C59.7,223.8 56.8,230 54.9,236.3C53,242.6 52,249.4 52,256C52,262.6 53,269.4 54.9,275.7C56.8,282 59.7,288.2 63.3,293.6C66.8,299 71.2,304 76,308.3C80.7,312.5 86.1,316.1 91.6,318.9C97.1,321.8 103,323.9 108.8,325.4C114.6,326.9 120.6,327.7 126.4,328C132.2,328.3 138,328 143.6,327.3C149.1,326.7 154.6,325.5 159.8,324C165.1,322.5 170.1,320.7 175,318.6C179.9,316.6 184.5,314.2 189,311.7C193.5,309.2 197.8,306.5 201.9,303.7C206.1,300.9 210,297.9 213.9,294.9C217.7,291.9 221.4,288.7 225.1,285.6C228.7,282.4 232.2,279.2 235.7,275.9C239.2,272.6 242.6,269.3 246,266C249.3,262.7 252.7,259.3 256,256C259.3,252.7 262.7,249.3 266,246C269.4,242.7 272.8,239.4 276.3,236.1C279.8,232.8 283.3,229.6 286.9,226.4C290.6,223.3 294.3,220.1 298.1,217.1C302,214.1 305.9,211.1 310.1,208.3C314.2,205.5 318.5,202.8 323,200.3C327.5,197.8 332.1,195.4 337,193.4C341.9,191.3 346.9,189.5 352.2,188C357.4,186.5 362.9,185.3 368.4,184.7C374,184 379.8,183.7 385.6,184C391.4,184.3 397.4,185.1 403.2,186.6C409,188.1 414.9,190.2 420.4,193.1C425.9,195.9 431.3,199.5 436,203.7C440.8,208 445.2,213 448.7,218.4C452.3,223.8 455.2,230 457.1,236.3C459,242.6 460,249.4 460,256C460,262.6 459,269.4 457.1,275.7C455.2,282 452.3,288.2 448.7,293.6C445.2,299 440.8,304 436,308.3C431.3,312.5 425.9,316.1 420.4,318.9C414.9,321.8 409,323.9 403.2,325.4C397.4,326.9 391.4,327.7 385.6,328C379.8,328.3 374,328 368.4,327.3C362.9,326.7 357.4,325.5 352.2,324C346.9,322.5 341.9,320.7 337,318.6C332.1,316.6 327.5,314.2 323,311.7C318.5,309.2 314.2,306.5 310.1,303.7C305.9,300.9 302,297.9 298.1,294.9C294.3,291.9 290.6,288.7 286.9,285.6C283.3,282.4 279.8,279.2 276.3,275.9C272.8,272.6 269.4,269.3 266,266C262.7,262.7 259.3,259.3 256,256Z"
    private static let mobiusCapPath = "M298.1,294.9C294.3,291.9 290.6,288.7 286.9,285.6C283.3,282.4 279.8,279.2 276.3,275.9C272.8,272.6 269.4,269.3 266,266C262.7,262.7 259.3,259.3 256,256C252.7,252.7 249.3,249.3 246,246C242.6,242.7 239.2,239.4 235.7,236.1C232.2,232.8 228.7,229.6 225.1,226.4C221.4,223.3 217.7,220.1 213.9,217.1"
    private var window: NSWindow!
    private var webView: WKWebView!
    private var backend: Process?
    private var port = 0
    private var quitting = false
    private var projectItem: NSMenuItem!

    private var resources: URL { Bundle.main.resourceURL! }
    private var logURL: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/Unity/backend.log")
    }

    /// The Git repository Workflow tasks branch from; nil keeps the backend in its private workspace.
    private var projectDirectory: URL? {
        guard let path = UserDefaults.standard.string(forKey: Self.projectKey) else { return nil }
        var isDirectory: ObjCBool = false
        return FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory) && isDirectory.boolValue
            ? URL(fileURLWithPath: path, isDirectory: true) : nil
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        webView = WKWebView(frame: .zero, configuration: config)
        let savedZoom = UserDefaults.standard.double(forKey: Self.zoomKey)
        if savedZoom >= Double(Self.minZoom) && savedZoom <= Double(Self.maxZoom) {
            webView.pageZoom = CGFloat(savedZoom)
        }
        webView.navigationDelegate = self
        webView.uiDelegate = self
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1360, height: 860),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Unity"
        window.minSize = NSSize(width: 900, height: 600)
        window.contentView = webView
        // Kept alive after ⌘W so a quit confirmation can still attach to it.
        window.isReleasedWhenClosed = false
        window.setFrameAutosaveName("UnityMainWindow")
        window.center()
        window.makeKeyAndOrderFront(nil)
        showStatus("正在启动 Unity…")
        startBackend()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    // MARK: Backend lifecycle

    private func freePort() -> Int {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        addr.sin_port = 0
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafeMutablePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.bind(fd, $0, len) }
        }
        _ = withUnsafeMutablePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.getsockname(fd, $0, &len) }
        }
        return Int(UInt16(bigEndian: addr.sin_port))
    }

    private func startBackend() {
        port = freePort()
        try? FileManager.default.createDirectory(at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil, attributes: [.posixPermissions: 0o600])
        }
        let log = try? FileHandle(forWritingTo: logURL)
        log?.seekToEndOfFile()

        let process = Process()
        process.executableURL = resources.appendingPathComponent("python/bin/python3")
        process.arguments = ["-m", "backend.app.desktop", "--port", String(port)]
        process.currentDirectoryURL = resources.appendingPathComponent("app")
        var env = ProcessInfo.processInfo.environment
        // Never write bytecode into the signed bundle (the launcher sets this again for children).
        env["PYTHONPYCACHEPREFIX"] = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Caches/Unity/pycache").path
        // The launcher makes this the backend's cwd, i.e. the Workflow project repository.
        if let project = projectDirectory { env["UNITY_WORKSPACE"] = project.path }
        process.environment = env
        process.standardOutput = log
        process.standardError = log
        process.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                // A backend replaced by a project switch is expected to exit.
                guard let self, !self.quitting, proc === self.backend else { return }
                self.showStatus("后端已退出（代码 \(proc.terminationStatus)）。日志：~/Library/Logs/Unity/backend.log")
            }
        }
        do {
            try process.run()
            backend = process
            waitForHealth(deadline: Date().addingTimeInterval(90))
        } catch {
            showStatus("无法启动后端：\(error.localizedDescription)")
        }
    }

    private var origin: URL { URL(string: "http://127.0.0.1:\(port)")! }

    private func waitForHealth(deadline: Date) {
        var request = URLRequest(url: origin.appendingPathComponent("api/health"))
        request.timeoutInterval = 2
        URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
            DispatchQueue.main.async {
                guard let self, self.backend?.isRunning == true else { return }
                if (response as? HTTPURLResponse)?.statusCode == 200 {
                    self.webView.load(URLRequest(url: self.origin))
                } else if Date() > deadline {
                    self.showStatus("后端启动超时。日志：~/Library/Logs/Unity/backend.log")
                } else {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { self.waitForHealth(deadline: deadline) }
                }
            }
        }.resume()
    }

    /// Sums the activity counters in /api/health; nil when they cannot be read.
    private func fetchActiveWork(_ completion: @escaping (Int?) -> Void) {
        var request = URLRequest(url: origin.appendingPathComponent("api/health"))
        request.timeoutInterval = 3
        URLSession.shared.dataTask(with: request) { data, _, _ in
            var total: Int?
            if let data, let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let runtime = json["runtime"] as? [String: Any],
               let activity = runtime["activity"] as? [String: Any] {
                total = activity.values.compactMap { ($0 as? NSNumber)?.intValue }.reduce(0, +)
            }
            DispatchQueue.main.async { completion(total) }
        }.resume()
    }

    // Asynchronous so the confirmation works for menu, Dock and Apple Event quits alike.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !quitting, let backend, backend.isRunning else { return .terminateNow }
        fetchActiveWork { [weak self] active in
            guard let self else { return }
            guard let active, active > 0 else { return self.stopBackendThenQuit() }
            let alert = NSAlert()
            alert.messageText = "还有 \(active) 项任务未结束"
            alert.informativeText = "包括正在运行或等待你回复的讨论。退出会中断正在进行的模型调用。"
            alert.addButton(withTitle: "取消")
            alert.addButton(withTitle: "仍然退出")
            self.window.makeKeyAndOrderFront(nil)
            alert.beginSheetModal(for: self.window) { response in
                if response == .alertFirstButtonReturn {
                    NSApp.reply(toApplicationShouldTerminate: false)
                } else {
                    self.stopBackendThenQuit()
                }
            }
        }
        return .terminateLater
    }

    /// Blocking: graceful SIGTERM first, SIGKILL after 10 seconds.
    private static func stop(_ backend: Process?) {
        guard let backend, backend.isRunning else { return }
        backend.terminate()
        let deadline = Date().addingTimeInterval(10)
        while backend.isRunning && Date() < deadline { usleep(100_000) }
        if backend.isRunning { kill(backend.processIdentifier, SIGKILL) }
    }

    private func stopBackendThenQuit() {
        quitting = true
        DispatchQueue.global().async { [backend] in
            Self.stop(backend)
            DispatchQueue.main.async { NSApp.reply(toApplicationShouldTerminate: true) }
        }
    }

    private func restartBackend() {
        let old = backend
        backend = nil
        showStatus("正在切换项目仓库…")
        DispatchQueue.global().async {
            Self.stop(old)
            DispatchQueue.main.async { self.startBackend() }
        }
    }

    // MARK: Project repository

    private static func gitTopLevel(_ url: URL) -> URL? {
        let git = Process()
        git.executableURL = URL(fileURLWithPath: "/usr/bin/git")
        git.arguments = ["-C", url.path, "rev-parse", "--show-toplevel"]
        let output = Pipe()
        git.standardOutput = output
        git.standardError = FileHandle.nullDevice
        guard (try? git.run()) != nil else { return nil }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        git.waitUntilExit()
        guard git.terminationStatus == 0,
              let path = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
              !path.isEmpty else { return nil }
        return URL(fileURLWithPath: path, isDirectory: true)
    }

    @objc private func chooseProject() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.prompt = "选择"
        panel.message = "选择 Workflow 任务使用的 Git 仓库；每个任务会在其中建立独立的分支和工作树。"
        panel.directoryURL = projectDirectory
        panel.beginSheetModal(for: window) { [weak self] response in
            guard let self, response == .OK, let url = panel.url else { return }
            // Let the panel's sheet finish closing before showing another one.
            DispatchQueue.main.async {
                guard let repository = Self.gitTopLevel(url) else {
                    let alert = NSAlert()
                    alert.messageText = "所选文件夹不是 Git 仓库"
                    alert.informativeText = "Workflow 需要在 Git 仓库中为任务创建分支和工作树。"
                    alert.beginSheetModal(for: self.window)
                    return
                }
                self.switchProject(to: repository)
            }
        }
    }

    @objc private func resetProject() { switchProject(to: nil) }

    private func switchProject(to url: URL?) {
        guard url?.path != projectDirectory?.path else { return }
        let apply = {
            if let url {
                UserDefaults.standard.set(url.path, forKey: Self.projectKey)
            } else {
                UserDefaults.standard.removeObject(forKey: Self.projectKey)
            }
            self.refreshProjectItem()
            self.restartBackend()
        }
        fetchActiveWork { [weak self] active in
            guard let self else { return }
            guard let active, active > 0 else { return apply() }
            let alert = NSAlert()
            alert.messageText = "还有 \(active) 项任务未结束"
            alert.informativeText = "切换仓库会重启后端，中断正在进行的模型调用。"
            alert.addButton(withTitle: "取消")
            alert.addButton(withTitle: "仍然切换")
            alert.beginSheetModal(for: self.window) { if $0 == .alertSecondButtonReturn { apply() } }
        }
    }

    private func refreshProjectItem() {
        let project = projectDirectory
        projectItem.title = "项目仓库：" + (project?.lastPathComponent ?? "未选择")
        projectItem.toolTip = project?.path ?? "未选择时使用数据目录中的 workspace，Workflow 任务无法创建工作区。"
    }

    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        menuItem.action == #selector(resetProject) ? projectDirectory != nil : true
    }

    private func showStatus(_ text: String) {
        let escaped = text.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
        let isError = escaped.contains("退出") || escaped.contains("无法") || escaped.contains("超时") || escaped.contains("错误")
        let indicatorColor = isError ? "#c84a32" : "#7ba79d"
        let statusTitle = isError ? "后端服务异常" : escaped
        let detailHtml = isError ? "<p class=\"sub\">\(escaped)</p>" : ""
        let cruiseClass = isError ? "" : "active"
        let breatheClass = isError ? "" : "breathe"

        let html = """
        <!DOCTYPE html>
        <html>
        <head>
        <meta charset="utf-8">
        <style>
          :root {
            --bg: #fdfbf7;
            --ink: #18191a;
            --muted: #6e6b65;
            --sub: #908d86;
            --shadow: rgba(0, 0, 0, 0.12);
            --ind: \(indicatorColor);
          }
          @media (prefers-color-scheme: dark) {
            :root {
              --bg: #121314;
              --ink: #f5f5f7;
              --muted: #9ea0a5;
              --sub: #707277;
              --shadow: rgba(0, 0, 0, 0.45);
            }
          }
          body {
            margin: 0;
            padding: 24px;
            height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            background: var(--bg);
            color: var(--ink);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", sans-serif;
            box-sizing: border-box;
          }
          .box {
            display: flex;
            flex-direction: column;
            align-items: center;
            text-align: center;
            max-width: 480px;
          }
          .logo-wrap {
            width: 140px;
            height: 140px;
            margin-bottom: 20px;
          }
          .logo-wrap.breathe {
            animation: breathe 3.2s ease-in-out infinite;
          }
          svg {
            width: 100%;
            height: 100%;
            overflow: visible;
            color: var(--ink);
          }
          .shadow {
            fill: none;
            stroke: var(--shadow);
            stroke-width: 64px;
            stroke-linecap: round;
            filter: blur(4px);
          }
          .mobius {
            fill: none;
            stroke: currentColor;
            stroke-width: 56px;
            stroke-linecap: round;
            stroke-linejoin: round;
          }
          .cap {
            fill: none;
            stroke: currentColor;
            stroke-width: 56px;
            stroke-linecap: round;
            stroke-linejoin: round;
          }
          .cruise {
            fill: none;
            stroke: #7ba79d;
            stroke-width: 24px;
            stroke-linecap: round;
            display: none;
          }
          .cruise.active {
            display: block;
            filter: drop-shadow(0 0 6px #7ba79d);
            animation: cruise 2.4s linear infinite;
          }
          .title {
            margin: 0 0 8px;
            font-size: 15px;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 8px;
            letter-spacing: -0.2px;
          }
          .dot {
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background: var(--ind);
            box-shadow: 0 0 6px var(--ind);
          }
          .sub {
            margin: 0;
            font-size: 13px;
            color: var(--sub);
            line-height: 1.5;
            word-break: break-all;
          }
          @keyframes breathe {
            0%, 100% { transform: scale(1); }
            50% { transform: scale(1.02); }
          }
          @keyframes cruise {
            0% { stroke-dashoffset: 1000; }
            100% { stroke-dashoffset: 0; }
          }
          @media (prefers-reduced-motion: reduce) {
            .logo-wrap.breathe { animation: none !important; }
            .cruise.active { display: none !important; }
          }
        </style>
        </head>
        <body>
          <div class="box">
            <div class="logo-wrap \(breatheClass)">
              <svg viewBox="0 0 512 512" aria-hidden="true">
                <path class="shadow" d="\(Self.mobiusCapPath)" />
                <path class="mobius" pathLength="1000" d="\(Self.mobiusPath)" />
                <path class="cap" d="\(Self.mobiusCapPath)" />
                <path class="cruise \(cruiseClass)" pathLength="1000" stroke-dasharray="140 860" stroke-dashoffset="1000" d="\(Self.mobiusPath)" />
              </svg>
            </div>
            <div class="title"><span class="dot"></span><span>\(statusTitle)</span></div>
            \(detailHtml)
          </div>
        </body>
        </html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    // MARK: Navigation, popups and downloads

    private func isApp(_ url: URL?) -> Bool { url?.host == "127.0.0.1" && url?.port == port }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if action.shouldPerformDownload { return decisionHandler(.download) }
        if let url = action.request.url, ["http", "https"].contains(url.scheme ?? ""), !isApp(url) {
            NSWorkspace.shared.open(url)
            return decisionHandler(.cancel)
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        decisionHandler(response.canShowMIMEType ? .allow : .download)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String,
                  completionHandler: @escaping (URL?) -> Void) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = suggestedFilename
        panel.directoryURL = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
        panel.beginSheetModal(for: window) { completionHandler($0 == .OK ? panel.url : nil) }
    }

    // target=_blank links (logs, previews) open in the default browser.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "确定")
        alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let alert = NSAlert()
        alert.messageText = prompt
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        field.stringValue = defaultText ?? ""
        alert.accessoryView = field
        alert.addButton(withTitle: "确定")
        alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn ? field.stringValue : nil) }
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.beginSheetModal(for: window) { completionHandler($0 == .OK ? panel.urls : nil) }
    }

    // MARK: Menu (WKWebView needs an Edit menu for ⌘C/⌘V/⌘A)

    @objc private func reload() { _ = isApp(webView.url) ? webView.reload() : webView.load(URLRequest(url: origin)) }
    @objc private func openLogs() { NSWorkspace.shared.activateFileViewerSelecting([logURL]) }
    @objc private func openData() {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        NSWorkspace.shared.open(support.appendingPathComponent("Unity"))
    }

    // MARK: Page Zoom

    private func updateZoom(_ newZoom: CGFloat) {
        let clamped = min(max(newZoom, Self.minZoom), Self.maxZoom)
        let rounded = (clamped * 100).rounded() / 100
        webView.pageZoom = rounded
        UserDefaults.standard.set(Double(rounded), forKey: Self.zoomKey)
    }

    @objc private func zoomIn() { updateZoom(webView.pageZoom + Self.zoomStep) }
    @objc private func zoomOut() { updateZoom(webView.pageZoom - Self.zoomStep) }
    @objc private func resetZoom() { updateZoom(1.0) }

    private func buildMenu() {
        let main = NSMenu()
        func submenu(_ title: String, _ items: [NSMenuItem]) {
            let holder = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            let menu = NSMenu(title: title)
            items.forEach(menu.addItem)
            holder.submenu = menu
            main.addItem(holder)
        }
        func item(_ title: String, _ action: Selector?, _ key: String = "", _ mods: NSEvent.ModifierFlags = .command) -> NSMenuItem {
            let entry = NSMenuItem(title: title, action: action, keyEquivalent: key)
            entry.keyEquivalentModifierMask = mods
            return entry
        }
        projectItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        refreshProjectItem()
        submenu("Unity", [
            item("关于 Unity", #selector(NSApplication.orderFrontStandardAboutPanel(_:))),
            .separator(),
            projectItem,
            item("选择项目仓库…", #selector(chooseProject), "o", [.command, .shift]),
            item("使用默认工作目录", #selector(resetProject)),
            .separator(),
            item("打开数据目录", #selector(openData)),
            item("显示后端日志", #selector(openLogs)),
            .separator(),
            item("隐藏 Unity", #selector(NSApplication.hide(_:)), "h"),
            item("退出 Unity", #selector(NSApplication.terminate(_:)), "q"),
        ])
        submenu("编辑", [
            item("撤销", Selector(("undo:")), "z"),
            item("重做", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("剪切", #selector(NSText.cut(_:)), "x"),
            item("复制", #selector(NSText.copy(_:)), "c"),
            item("粘贴", #selector(NSText.paste(_:)), "v"),
            item("全选", #selector(NSText.selectAll(_:)), "a"),
        ])
        let zoomInEqual = item("放大", #selector(zoomIn), "=")
        zoomInEqual.isHidden = true
        submenu("显示", [
            item("重新载入", #selector(reload), "r"),
            .separator(),
            item("实际大小", #selector(resetZoom), "0"),
            item("放大", #selector(zoomIn), "+"),
            zoomInEqual,
            item("缩小", #selector(zoomOut), "-"),
        ])
        submenu("窗口", [
            item("最小化", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("关闭", #selector(NSWindow.performClose(_:)), "w"),
        ])
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
