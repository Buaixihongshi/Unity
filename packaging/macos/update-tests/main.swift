// Disposable full-bundle acceptance launcher, compiled with the production shell
// and updater. Replaces main.swift only inside the isolated test app.
import Cocoa
import WebKit

setvbuf(stdout, nil, _IOLBF, 0)
let env = ProcessInfo.processInfo.environment
guard Bundle.main.bundleIdentifier?.hasSuffix(".acceptance") == true else { exit(2) }
// Sparkle can relaunch without our environment. Stop before starting a backend.
guard env["UNITY_SUPPORT_DIR"] != nil else {
    let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "unknown"
    let marker = Bundle.main.bundleURL.deletingLastPathComponent().appendingPathComponent("relaunched-" + build)
    try? Data().write(to: marker)
    exit(0)
}
let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
let mode = ProcessInfo.processInfo.environment["UNITY_ACCEPTANCE_MODE"] ?? "manual"

func pump(_ duration: TimeInterval) {
    let until = Date().addingTimeInterval(duration)
    while Date() < until { RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.02)) }
}
func until(_ name: String, timeout: TimeInterval = 90, _ predicate: () -> Bool) {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline { if predicate() { print("PASS \(name)"); return }; pump(0.05) }
    print("FAIL \(name): notice=\(delegate.desktopUpdater.notice ?? [:]) error=\(delegate.desktopUpdater.errorMessage ?? "none")")
    exit(1)
}
func js(_ expr: String) -> Any? {
    var done = false
    var result: Any?
    delegate.webView.evaluateJavaScript(expr) { value, _ in result = value; done = true }
    let deadline = Date().addingTimeInterval(3)
    while !done && Date() < deadline { pump(0.02) }
    return result
}
func command(_ name: String) { _ = js("window.webkit.messageHandlers.unityBridge.postMessage({type:'\(name)'})") }
func check(_ name: String, _ condition: Bool) {
    print("\(condition ? "PASS" : "FAIL") \(name)")
    if !condition { exit(1) }
}

func runAcceptance() {
    until("packaged backend and React mounted") { (js("document.querySelector('#root')?.children.length > 0") as? Bool) == true }
    until("production bridge received state") { (js("typeof window.webkit.messageHandlers.unityBridge.postMessage") as? String) == "function" }
    print("BACKEND_PORT \(delegate.port)")
    if mode == "auto" {
        // Exercise the actual background check with the production download default.
        until("background update ready", timeout: 180) { delegate.desktopUpdater.notice?["type"] as? String == "update-ready" }
    } else {
        until("Sparkle can check") { delegate.desktopUpdater.canCheck }
        _ = js("window.webkit.messageHandlers.unityBridge.postMessage({type:'setAutoDownload',value:false})")
        command("checkForUpdates")
        until("automatic downloads off finds update") { delegate.desktopUpdater.notice?["type"] as? String == "update-found" }
        check("automatic downloads remain off", delegate.desktopUpdater.updater?.automaticallyDownloadsUpdates == false)
        print("FOUND_WITHOUT_DOWNLOAD")
        pump(1)
        command("installNow")
        if mode == "bad" {
            until("bad signature rejected", timeout: 180) { delegate.desktopUpdater.errorMessage != nil }
            print("EXPECTED_ERROR \(delegate.desktopUpdater.errorMessage ?? "")")
            check("bad signature never ready", delegate.desktopUpdater.notice?["type"] as? String != "update-ready")
            print("ACCEPTANCE_DONE")
            app.terminate(nil)
            return
        }
        until("manual download ready", timeout: 180) { delegate.desktopUpdater.notice?["type"] as? String == "update-ready" }
    }
    check("no native updater window", app.windows.filter { $0.isVisible }.count == 1)
    // A true page reload must replay the ready capsule from native state.
    _ = js("location.reload()")
    until("ready capsule survives reload") { (js("document.querySelector('[data-testid=update-capsule]') !== null") as? Bool) == true }
    if mode == "manual" {
        delegate.activityReader = { $0(nil) }
        command("installNow")
        until("unknown activity refuses restart") { delegate.desktopUpdater.errorMessage?.contains("无法确认") == true }
        delegate.activityReader = { $0(3) }
        command("installNow")
        until("busy activity refuses restart") { delegate.desktopUpdater.errorMessage?.contains("还有任务") == true }
        check("staged update survives refusals", delegate.desktopUpdater.notice?["type"] as? String == "update-ready")
        delegate.activityReader = nil
    }
    print("READY_FOR_REAL_CLI")
    // The outer runner executes a real CLI mission against this packaged
    // backend, verifies published messages, then releases this local gate.
    let gate = ProcessInfo.processInfo.environment["UNITY_ACCEPTANCE_GATE"]!
    until("real CLI result observed by runner", timeout: 420) { FileManager.default.fileExists(atPath: gate) }
    if let cid = try? String(contentsOfFile: gate) {
        check("runner provided conversation id", cid.hasPrefix("c_"))
        _ = js("location.reload()")
        until("acceptance conversation visible") { (js("Array.from(document.querySelectorAll('button.conv-item')).some(b => b.innerText.includes('Sparkle real CLI acceptance'))") as? Bool) == true }
        _ = js("Array.from(document.querySelectorAll('button.conv-item')).find(b => b.innerText.includes('Sparkle real CLI acceptance')).click()")
        until("real CLI published result displayed in React") { (js("Array.from(document.querySelectorAll('.chat-row:not(.chat-row-user) .bubble-md')).some(b => b.innerText.includes('SPARKLE_CLI_OK'))") as? Bool) == true }
    }
    print("ACCEPTANCE_DONE")
    // Natural quit exercises install-on-quit; manual exercises restart+drain.
    if mode == "auto" { app.terminate(nil) }
    else { command("installNow") }
}
Timer.scheduledTimer(withTimeInterval: 1, repeats: false) { _ in runAcceptance() }
app.run()
