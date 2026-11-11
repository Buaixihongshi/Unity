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

func pump(_ seconds: TimeInterval) {
    RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(seconds))
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
// the page itself.
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
// until the new one commits. Page identity comes from the page itself.
func loadAndWait(_ url: URL, tag: String) {
    webView.load(URLRequest(url: url))
    _ = pumpUntil(10) {
        (js(webView, "window.__fixturePage") as? String) == tag
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

webView.reload()
_ = pumpUntil(10) { (js(webView, "document.readyState") as? String) == "complete" }
expect("T4 reload: page receives state again after reload",
       pumpUntil(6) { jsInt(webView, "window.stateEvents") >= 1 })

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

// MARK: Summary

defaults.removePersistentDomain(forName: suiteName)
print("")
print("bridge tests: \(passed) passed, \(failed.count) failed")
if !failed.isEmpty {
    failed.forEach { print("  failed: \($0)") }
    exit(1)
}
exit(0)
