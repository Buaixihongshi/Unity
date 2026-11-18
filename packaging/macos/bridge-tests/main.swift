// Regression suite for the desktop unityBridge (#168/#169).
//
// Compiled together with UnityApp.swift (-DBRIDGE_TESTS) so the exact code
// that ships in the app is what gets exercised — the real WKScriptMessageHandler,
// driven through a real WKWebView against a local loopback HTTP fixture.
// macOS only; run via run.sh (CI is linux and cannot host WebKit).
//
// #168 negatives: preview/API-style pages and every child frame must not be
// able to drive the shell even from the main frame. #169 positives: a
// listener attached after every automatic push still receives state via the
// getState handshake, across remounts and reloads.
import Cocoa
import Network
import WebKit

setvbuf(stdout, nil, _IOLBF, 0)
NSApplication.shared.setActivationPolicy(.accessory)

var passed = 0
var failed: [String] = []
func expect(_ name: String, _ condition: Bool, _ detail: String = "") {
    if condition {
        passed += 1
        print("PASS  \(name)")
    } else {
        failed.append(name)
        print("FAIL  \(name)\(detail.isEmpty ? "" : "  — \(detail)")")
    }
}

// MARK: Runloop helpers — WKWebView and DispatchWorkItem both need the main runloop.

// run(mode:before:) returns as soon as it has serviced ONE event, so a bare
// call is not a reliable delay — with WebKit's event sources flowing, a
// "1.5s settle" could return in milliseconds (#171 fail-control caught this:
// the second automatic push then landed inside the next assertion window).
// Pump in slices until the deadline has actually passed.
func pump(_ seconds: TimeInterval) {
    let deadline = Date().addingTimeInterval(seconds)
    while Date() < deadline {
        RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05))
    }
}

func pumpUntil(_ timeout: TimeInterval, _ condition: () -> Bool) -> Bool {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        if condition() { return true }
        pump(0.05)
    }
    return condition()
}

final class JSBox {
    var value: Any?
    var error: Error?
    var done = false
}

func js(_ webView: WKWebView, _ expr: String) -> Any? {
    let box = JSBox()
    webView.evaluateJavaScript(expr) { value, error in
        box.value = value
        box.error = error
        box.done = true
    }
    return pumpUntil(5) { box.done } ? box.value : nil
}

func jsInt(_ webView: WKWebView, _ expr: String) -> Int {
    (js(webView, expr) as? NSNumber)?.intValue ?? -1
}

// MARK: Loopback fixture server — canned pages, loopback only, no disk.

final class FixtureServer {
    private let listener: NWListener
    private let queue = DispatchQueue(label: "unity-bridge-tests")
    private let pages: [String: (body: String, headers: [String: String])]
    private(set) var port: UInt16 = 0

    init(pages: [String: (body: String, headers: [String: String])]) throws {
        let params = NWParameters.tcp
        params.allowLocalEndpointReuse = true
        // Loopback only, on a concrete free port: the fixture must never
        // listen on other interfaces.
        params.requiredLocalEndpoint = NWEndpoint.hostPort(
            host: .ipv4(.loopback), port: NWEndpoint.Port(rawValue: freeLoopbackPort())!)
        listener = try NWListener(using: params)
        self.pages = pages
    }

    func start() throws -> UInt16 {
        let ready = DispatchSemaphore(value: 0)
        listener.stateUpdateHandler = { state in
            switch state {
            case .ready, .failed: ready.signal()
            default: break
            }
        }
        listener.newConnectionHandler = { [weak self] connection in
            self?.accept(connection)
        }
        listener.start(queue: queue)
        guard ready.wait(timeout: .now() + 5) == .success, let port = listener.port?.rawValue, port != 0 else {
            throw NSError(domain: "unity-bridge-tests", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "fixture server did not come up"])
        }
        self.port = port
        return port
    }

    private func accept(_ connection: NWConnection) {
        connection.start(queue: queue)
        receiveHeaders(connection, Data())
    }

    private func receiveHeaders(_ connection: NWConnection, _ buffer: Data) {
        if buffer.range(of: Data("\r\n\r\n".utf8)) != nil {
            respond(connection, to: buffer)
            return
        }
        connection.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, _, error in
            guard let self, error == nil else { connection.cancel(); return }
            var next = buffer
            if let data { next.append(data) }
            next.count > 65536 ? connection.cancel() : self.receiveHeaders(connection, next)
        }
    }

    private func respond(_ connection: NWConnection, to request: Data) {
        let requestLine = String(data: request, encoding: .utf8)?
            .split(separator: "\r\n").first.map(String.init) ?? ""
        let rawPath = requestLine.split(separator: " ").dropFirst().first.map(String.init) ?? "/"
        let path = String(rawPath.split(separator: "?").first ?? "/")
        let page = pages[path]
        let body = (page?.body ?? "not found").data(using: .utf8) ?? Data()
        var head = """
        HTTP/1.1 \(page == nil ? "404 Not Found" : "200 OK")
        Content-Type: text/html; charset=utf-8
        Cache-Control: no-store
        Connection: close
        Content-Length: \(body.count)

        """
        head = head.replacingOccurrences(of: "\n", with: "\r\n")
        for (key, value) in page?.headers ?? [:] { head += "\(key): \(value)\r\n" }
        head += "\r\n"
        var payload = Data(head.utf8)
        payload.append(body)
        connection.send(content: payload, completion: .contentProcessed { _ in connection.cancel() })
    }
}

func freeLoopbackPort() -> UInt16 {
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
    return UInt16(bigEndian: addr.sin_port)
}

// MARK: Fixture pages

let pageScript = """
window.__docNonce = Math.random().toString(36).slice(2) + '.' + String(Date.now());
window.stateEvents = 0;
window.lastState = null;
document.addEventListener('unity-bridge', function (e) {
  var d = e.detail;
  if (d && d.type === 'state') { window.stateEvents += 1; window.lastState = d; }
});
window.postToBridge = function (cmd) {
  try { window.webkit.messageHandlers.unityBridge.postMessage(cmd); }
  catch (e) { window.postError = String(e); }
};
"""

// The SPA entry: registers its listener, can post commands on demand, and —
// only when asked with ?late — attaches a second consumer at 1.8s (after
// every automatic push) which must still receive state via getState (#169).
// Every fixture page sets window.__fixturePage: webView.url flips when a
// navigation STARTS, while evaluateJavaScript keeps hitting the outgoing
// document until the new one commits, so page identity has to be read from
// the page itself. Each document also gets a random __docNonce: the tag
// distinguishes pages, but a reload serves the same tag again — only a
// changed nonce proves the reloaded document has committed (#171).
let entryPage = """
<!doctype html><html><body><div id="ok">entry</div>
<iframe id="nested" src="/nested-frame.html"></iframe>
<iframe id="srcdoc" srcdoc="<script>window.postToBridge = function (c) { window.webkit.messageHandlers.unityBridge.postMessage(c); };</script>"></iframe>
<script>
window.__fixturePage = 'entry';
\(pageScript)
window.remount = { ran: false, events: 0 };
if (location.search.indexOf('late') >= 0) {
  setTimeout(function () {
    document.addEventListener('unity-bridge', function (e) {
      if (e.detail && e.detail.type === 'state') window.remount.events += 1;
    });
    window.postToBridge({ type: 'getState' });
    window.remount.ran = true;
  }, 1800);
}
</script></body></html>
"""

let childFramePage = """
<!doctype html><html><body>child frame
<script>
window.__fixturePage = 'child';
\(pageScript)
</script></body></html>
"""

// Same script, different serving policy: /preview.html carries the exact CSP
// the backend sends for snapshot previews (opaque origin, #168's report).
let previewBody = """
<!doctype html><html><body>preview</body></html>
<script>
window.__fixturePage = 'preview';
\(pageScript)
</script>
"""
let plainBody = """
<!doctype html><html><body>plain same-origin page</body></html>
<script>
window.__fixturePage = 'plain';
\(pageScript)
</script>
"""
let otherOriginBody = """
<!doctype html><html><body>other loopback port</body></html>
<script>
window.__fixturePage = 'other';
\(pageScript)
</script>
"""
let statusBody = """
<!doctype html><html><body>status</body></html>
<script>
window.__fixturePage = 'status';
\(pageScript)
</script>
"""

let previewCSP = "sandbox allow-scripts allow-forms; default-src 'none'; form-action 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:"

// MARK: Harness

let autoDownloadKey = "SUAutomaticallyUpdate"

func makeServer(pages: [String: (body: String, headers: [String: String])]) -> FixtureServer {
    for _ in 0..<5 {
        if let server = try? FixtureServer(pages: pages), let port = try? server.start(), port != 0 {
            return server
        }
        pump(0.1)
    }
    fatalError("could not start the fixture server")
}

let server = makeServer(pages: [
    "/": (entryPage, [:]),
    "/index.html": (entryPage, [:]),
    "/nested-frame.html": (childFramePage, [:]),
    "/preview.html": (previewBody, ["Content-Security-Policy": previewCSP]),
    "/plain.html": (plainBody, [:]),
])
let otherServer = makeServer(pages: ["/": (otherOriginBody, [:])])

let delegate = AppDelegate()
let suiteName = "unity-bridge-tests-" + UUID().uuidString
let defaults = UserDefaults(suiteName: suiteName)!
defaults.removePersistentDomain(forName: suiteName)
delegate.defaults = defaults
delegate.port = Int(server.port)

let config = WKWebViewConfiguration()
config.websiteDataStore = .nonPersistent()
let content = WKUserContentController()
content.add(delegate, name: "unityBridge")
config.userContentController = content
let webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 800, height: 600), configuration: config)
webView.navigationDelegate = delegate
delegate.webView = webView

// readyState and webView.url are both unreliable navigation signals: the
// outgoing page already reports "complete", and webView.url flips when the
// navigation starts while evaluateJavaScript keeps hitting the old document
// until the new one commits. Page identity comes from the page itself — and
// it must include the nonce, not just the tag: / and /?late=1 share the
// 'entry' tag, so a tag-only check passes on the outgoing document and a
// following reload() then cancels the in-flight navigation and reloads the
// OLD url (#171's second face). The first load has no outgoing document
// (nonce reads nil); a fresh nonce plus the tag is enough there.
func loadAndWait(_ url: URL, tag: String) {
    let outgoingNonce = js(webView, "window.__docNonce") as? String ?? "nil"
    webView.load(URLRequest(url: url))
    _ = pumpUntil(10) {
        (js(webView, "window.__fixturePage") as? String) == tag
            && (js(webView, "window.__docNonce") as? String ?? "nil") != outgoingNonce
            && (js(webView, "document.readyState") as? String) == "complete"
    }
    // Let the didFinish state push (0.4s) land so absence assertions below
    // compare against a quiet baseline.
    pump(1.2)
}

func entryURL(_ path: String) -> URL {
    URL(string: "http://127.0.0.1:\(server.port)\(path)")!
}

// Wait for the automatic post-load push and return the baseline count.
func baselineStates() -> Int {
    _ = pumpUntil(4) { jsInt(webView, "window.stateEvents") >= 1 }
    return jsInt(webView, "window.stateEvents")
}

func rejectedScenario(_ name: String, tag: String, post: () -> String) {
    // Guard against racing a navigation: a command evaluated in the wrong
    // document would make both assertions below pass vacuously or poison
    // later ones.
    let currentPage = js(webView, "window.__fixturePage") as? String ?? "nil"
    expect("\(name): command runs on the \(tag) page", currentPage == tag, "got \(currentPage)")
    let before = jsInt(webView, "window.stateEvents")
    _ = js(webView, post())
    pump(1.0)
    let untouched = (defaults.object(forKey: autoDownloadKey) as? Bool) == false
    let silent = jsInt(webView, "window.stateEvents") == before
    expect("\(name): preference untouched", untouched,
           "SUAutomaticallyUpdate=\(String(describing: defaults.object(forKey: autoDownloadKey)))")
    expect("\(name): no state push", silent,
           "stateEvents \(before) → \(jsInt(webView, "window.stateEvents"))")
}

// MARK: Scenarios

expect("T0 fresh suite has no stored preference", defaults.object(forKey: autoDownloadKey) == nil)

// --- positives on the app entry document -------------------------------

loadAndWait(entryURL("/"), tag: "entry")
_ = baselineStates()

_ = js(webView, "window.postToBridge({type:'setAutoDownload', value:false})")
_ = pumpUntil(3) { (defaults.object(forKey: autoDownloadKey) as? Bool) == false }
expect("T1 entry: setAutoDownload accepted from the entry document",
       (defaults.object(forKey: autoDownloadKey) as? Bool) == false)
expect("T1 entry: state push reflects the written preference",
       (js(webView, "window.lastState && window.lastState.autoDownload") as? Bool) == false)

// Let any pending automatic pushes settle first (see T4: a single load can
// land two didFinish callbacks, each scheduling its own 0.4s push), so only
// the handshake answer can move the count.
pump(1.5)
let beforeGetState = jsInt(webView, "window.stateEvents")
_ = js(webView, "window.postToBridge({type:'getState'})")
_ = pumpUntil(3) { jsInt(webView, "window.stateEvents") > beforeGetState }
expect("T2 entry: getState answers with a state snapshot",
       jsInt(webView, "window.stateEvents") > beforeGetState)

// --- #168 negatives: everything that is not the entry document ---------

// Both iframes must be loaded (and postToBridge defined) before the tests
// drive them, otherwise a command that never ran would pass vacuously.
_ = pumpUntil(5) {
    (js(webView, "typeof document.getElementById('nested').contentWindow.postToBridge") as? String) == "function"
        && (js(webView, "typeof document.getElementById('srcdoc').contentWindow.postToBridge") as? String) == "function"
}

rejectedScenario("T5 child frame", tag: "entry") {
    "document.getElementById('nested').contentWindow.postToBridge({type:'setAutoDownload', value:true})"
}
rejectedScenario("T6 srcdoc frame", tag: "entry") {
    "document.getElementById('srcdoc').contentWindow.postToBridge({type:'setAutoDownload', value:true})"
}

// --- #169: late second consumer, then reload ---------------------------

loadAndWait(entryURL("/?late=1"), tag: "entry")
_ = baselineStates()
_ = pumpUntil(8) { (js(webView, "window.remount.ran") as? Bool) == true }
expect("T3 late consumer: listener attached at 1.8s receives state via getState",
       jsInt(webView, "window.remount.events") >= 1)

// #171: after reload() the outgoing document still answers evaluateJavaScript
// with readyState "complete" and its accumulated stateEvents, so the previous
// "wait for complete, then stateEvents >= 1" passed on the OLD document before
// the new one committed. Reload from the plain entry page (T3's ?late=1 page
// would recreate its 1.8s consumer, which posts getState itself and would
// satisfy the push assertion through the handshake instead). Prove the new
// document committed via the per-document nonce, then assert each state path
// on that document.
// #171: after reload() the outgoing document still answers evaluateJavaScript
// with readyState "complete" and its accumulated stateEvents, so the previous
// "wait for complete, then stateEvents >= 1" could pass on the OLD document —
// a timing race with the new document's commit (the fail-control below shows
// both outcomes). Reload from the plain entry page (T3's ?late=1 page would
// recreate its 1.8s consumer, which posts getState itself and would satisfy
// the push assertion through the handshake instead). Prove the new document
// committed via the per-document nonce, then assert each state path on it.
loadAndWait(entryURL("/"), tag: "entry")
let oldNonce = js(webView, "window.__docNonce") as? String ?? "nil"
webView.reload()
expect("T4 reload: new document committed (per-document nonce changed)",
       pumpUntil(10) {
           (js(webView, "window.__docNonce") as? String ?? "nil") != oldNonce
               && oldNonce != "nil"
               && (js(webView, "document.readyState") as? String) == "complete"
       })
// The reloaded document counts stateEvents from zero, so only a state event
// delivered to its own listener satisfies this — the automatic post-load push.
expect("T4 reload: reloaded document receives the automatic state push",
       pumpUntil(6) { jsInt(webView, "window.stateEvents") >= 1 })
// And the handshake, asked by the reloaded document itself, still answers.
// Same settling pump as T2: one reload() can land TWO didFinish callbacks
// (observed 7ms apart while fail-controlling #171), each scheduling its own
// 0.4s push — without settling, the second push passes this assertion with
// the handshake answer mutated away. Quiet first, then only the answer can
// move the count.
pump(1.5)
let beforeHandshake = jsInt(webView, "window.stateEvents")
_ = js(webView, "window.postToBridge({type:'getState'})")
expect("T4 reload: getState handshake answers the reloaded document",
       pumpUntil(6) { jsInt(webView, "window.stateEvents") > beforeHandshake })

// --- #168 negatives: non-entry main-frame documents --------------------

loadAndWait(entryURL("/preview.html"), tag: "preview")
_ = pumpUntil(4) { jsInt(webView, "window.stateEvents") >= 1 }  // automatic push only
rejectedScenario("T7 CSP-sandboxed main frame", tag: "preview") {
    "window.postToBridge({type:'setAutoDownload', value:true})"
}

loadAndWait(entryURL("/plain.html"), tag: "plain")
_ = pumpUntil(4) { jsInt(webView, "window.stateEvents") >= 1 }
rejectedScenario("T8 same-origin non-entry path", tag: "plain") {
    "window.postToBridge({type:'setAutoDownload', value:true})"
}

// The shell's own status/error page: loadHTMLString with a nil baseURL.
webView.loadHTMLString(statusBody, baseURL: nil)
_ = pumpUntil(10) { (js(webView, "window.__fixturePage") as? String) == "status"
                    && (js(webView, "document.readyState") as? String) == "complete" }
pump(1.2)
rejectedScenario("T9 loadHTMLString status page", tag: "status") {
    "window.postToBridge({type:'setAutoDownload', value:true})"
}

// A different loopback port: same host, wrong origin. Navigation admission
// would hand this to the browser, so drive it without the app's navigation
// delegate; the bridge handler must still refuse.
webView.navigationDelegate = nil
loadAndWait(URL(string: "http://127.0.0.1:\(otherServer.port)/")!, tag: "other")
rejectedScenario("T10 other-port origin", tag: "other") {
    "window.postToBridge({type:'setAutoDownload', value:true})"
}

// MARK: Sparkle bridge snapshot, restart guard and malformed health (#175)

let health = { (activity: String) -> Data in
    Data("{\"runtime\":{\"activity\":\(activity)}}".utf8)
}
let idleHealth = health("{\"active_missions\":0,\"active_maintenance\":0,\"active_grants\":0}")
expect("S1 valid health proves idle", AppDelegate.activeWork(idleHealth, status: 200) == 0)
expect("S2 non-200 cannot prove idle", AppDelegate.activeWork(idleHealth, status: 503) == nil)
for (name, value) in [("empty", "{}"), ("missing", "{\"active_missions\":0}"),
                      ("negative", "{\"active_missions\":-1,\"active_maintenance\":0,\"active_grants\":0}"),
                      ("boolean", "{\"active_missions\":false,\"active_maintenance\":0,\"active_grants\":0}"),
                      ("fraction", "{\"active_missions\":0.5,\"active_maintenance\":0,\"active_grants\":0}")] {
    expect("S3 \(name) health rejected", AppDelegate.activeWork(health(value), status: 200) == nil)
}
expect("S4 active grants count as busy", AppDelegate.activeWork(health("{\"active_missions\":0,\"active_maintenance\":0,\"active_grants\":1}"), status: 200) == 1)

webView.navigationDelegate = delegate
loadAndWait(entryURL("/"), tag: "entry")
var installs = 0
delegate.desktopUpdater.requestRestart = { delegate.requestUpdateRestart($0) }
delegate.desktopUpdater.showReady(toInstallAndRelaunch: { choice in if choice == .install { installs += 1 } })
// The bridge must replay the pending ready state after a fresh document loads.
let readyNonce = js(webView, "window.__docNonce") as? String
webView.reload()
expect("S5 ready reload commits new document", pumpUntil(10) {
    (js(webView, "window.__docNonce") as? String) != readyNonce
})
_ = js(webView, "window.readySeen=false; document.addEventListener('unity-bridge',e=>{if(e.detail.type==='update-ready')window.readySeen=true}); window.postToBridge({type:'getState'})")
expect("S6 getState replays ready update", pumpUntil(4) { (js(webView, "window.readySeen") as? Bool) == true })
for active: Int? in [2, nil] {
    delegate.activityReader = { $0(active) }
    _ = js(webView, "window.postToBridge({type:'installNow'})")
    pump(0.3)
    expect("S7 \(String(describing: active)) blocks update restart", installs == 0)
}
delegate.activityReader = { $0(0) }
delegate.updateDrainer = { $0(false) }
_ = js(webView, "window.postToBridge({type:'installNow'})")
pump(0.3)
expect("S8 concurrent task/drain rejection blocks restart", installs == 0)
delegate.updateDrainer = { $0(true) }
_ = js(webView, "window.postToBridge({type:'installNow'})")
expect("S8 idle allows explicit update restart", pumpUntil(3) { installs == 1 })

// MARK: Summary

defaults.removePersistentDomain(forName: suiteName)
print("")
print("bridge tests: \(passed) passed, \(failed.count) failed")
if !failed.isEmpty {
    failed.forEach { print("  failed: \($0)") }
    exit(1)
}
exit(0)
