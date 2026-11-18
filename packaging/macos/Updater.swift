import Cocoa
import Sparkle

/// Sparkle owns transport, signature verification, extraction and installation.
/// This driver only owns the bridge UI and explicit user choices, on the main thread.
final class DesktopUpdater: NSObject, SPUUpdaterDelegate, SPUUserDriver {
    private(set) var updater: SPUUpdater?
    var changed: (() -> Void)?
    var emit: (([String: Any]) -> Void)?
    var requestRestart: ((@escaping () -> Void) -> Void)?
    private(set) var notice: [String: Any]?
    private(set) var errorMessage: String?
    private(set) var checking = false
    private var version = ""
    private var build = ""
    private var downloadRequested = false
    private var restartChoice: (() -> Void)?
    private var observation: NSKeyValueObservation?

    var canCheck: Bool { updater?.canCheckForUpdates ?? false }

    func start(bundle: Bundle = .main) {
        guard let key = bundle.object(forInfoDictionaryKey: "SUPublicEDKey") as? String,
              Data(base64Encoded: key)?.count == 32,
              let feed = bundle.object(forInfoDictionaryKey: "SUFeedURL") as? String,
              let url = URL(string: feed), url.scheme == "https" ||
                (url.scheme == "http" && url.host == "127.0.0.1" && bundle.bundleIdentifier != "io.github.buaixihongshi.unity") else {
            errorMessage = "此构建未配置更新签名公钥与更新源"
            changed?()
            return
        }
        let engine = SPUUpdater(hostBundle: bundle, applicationBundle: bundle, userDriver: self, delegate: self)
        updater = engine
        do {
            try engine.start()
            observation = engine.observe(\.canCheckForUpdates, options: [.new]) { [weak self] _, _ in
                self?.changed?()
            }
        } catch {
            updater = nil
            fail(error.localizedDescription)
        }
        changed?()
    }

    func setAutoDownload(_ value: Bool) {
        updater?.automaticallyDownloadsUpdates = value
        // Enabling downloads while the found capsule is visible should proceed.
        if value, notice?["type"] as? String == "update-found", canCheck {
            updater?.checkForUpdates()
        }
        changed?()
    }

    func check() {
        guard let updater, updater.canCheckForUpdates else { changed?(); return }
        checking = true
        errorMessage = nil
        changed?()
        updater.checkForUpdates()
    }

    /// The same command means download when found, restart when prepared.
    func installNow() {
        if let restartChoice {
            requestRestart?(restartChoice)
        } else if notice?["type"] as? String == "update-found", canCheck {
            downloadRequested = true
            check()
        }
    }

    func fail(_ message: String) {
        checking = false
        errorMessage = message
        changed?()
        emit?(["type": "update-error", "message": message])
    }

    private func publish(_ event: [String: Any]) {
        notice = event
        checking = false
        errorMessage = nil
        changed?()
        emit?(event)
    }

    private func ready(_ restart: @escaping () -> Void) {
        downloadRequested = false
        restartChoice = restart
        publish(["type": "update-ready", "version": version, "build": build, "willInstallOnQuit": true])
    }

    // MARK: Engine delegate (also receives silent background update results)

    func updater(_ updater: SPUUpdater, didFindValidUpdate item: SUAppcastItem) {
        version = item.displayVersionString
        build = item.versionString
        checking = false
        changed?()
    }

    func updater(_ updater: SPUUpdater, willInstallUpdateOnQuit item: SUAppcastItem,
                 immediateInstallationBlock: @escaping () -> Void) -> Bool {
        version = item.displayVersionString
        build = item.versionString
        ready(immediateInstallationBlock)
        return true
    }

    func updater(_ updater: SPUUpdater, didFinishUpdateCycleFor updateCheck: SPUUpdateCheck, error: Error?) {
        checking = false
        if let error = error as NSError? {
            if error.domain == SUSparkleErrorDomain && error.code == SUError.noUpdateError.rawValue {
                publish(["type": "up-to-date", "checkedAt": ISO8601DateFormatter().string(from: Date())])
            } else {
                downloadRequested = false
                restartChoice = nil
                notice = nil
                fail(error.localizedDescription)
            }
        }
        changed?()
    }

    func updater(_ updater: SPUUpdater, shouldDownloadReleaseNotesForUpdate item: SUAppcastItem) -> Bool { false }

    // MARK: Custom user driver — no second native update window

    func show(_ request: SPUUpdatePermissionRequest,
                                     reply: @escaping (SUUpdatePermissionResponse) -> Void) {
        reply(SUUpdatePermissionResponse(automaticUpdateChecks: true, sendSystemProfile: false))
    }
    func showUserInitiatedUpdateCheck(cancellation: @escaping () -> Void) { checking = true; changed?() }
    func showUpdateFound(with item: SUAppcastItem, state: SPUUserUpdateState,
                         reply: @escaping (SPUUserUpdateChoice) -> Void) {
        version = item.displayVersionString
        build = item.versionString
        guard !item.isInformationOnlyUpdate else {
            reply(.dismiss)
            fail("此版本需手动下载安装，请查看发布说明")
            return
        }
        if state.stage == .installing {
            ready { reply(.install) }
        } else if state.stage == .downloaded || downloadRequested || updater?.automaticallyDownloadsUpdates == true {
            downloadRequested = false
            notice = nil
            reply(.install)
        } else {
            publish(["type": "update-found", "version": version, "build": build])
            // Finish this cycle so turning automatic downloads off still allows
            // tomorrow's check. The capsule command starts a new user check.
            reply(.dismiss)
        }
    }
    func showUpdateReleaseNotes(with downloadData: SPUDownloadData) {}
    func showUpdateReleaseNotesFailedToDownloadWithError(_ error: Error) {}
    func showUpdateNotFoundWithError(_ error: Error, acknowledgement: @escaping () -> Void) { acknowledgement() }
    func showUpdaterError(_ error: Error, acknowledgement: @escaping () -> Void) {
        fail(error.localizedDescription)
        acknowledgement()
    }
    func showDownloadInitiated(cancellation: @escaping () -> Void) {}
    func showDownloadDidReceiveExpectedContentLength(_ expectedContentLength: UInt64) {}
    func showDownloadDidReceiveData(ofLength length: UInt64) {}
    func showDownloadDidStartExtractingUpdate() {}
    func showExtractionReceivedProgress(_ progress: Double) {}
    func showReady(toInstallAndRelaunch reply: @escaping (SPUUserUpdateChoice) -> Void) { ready { reply(.install) } }
    func showInstallingUpdate(withApplicationTerminated applicationTerminated: Bool,
                             retryTerminatingApplication: @escaping () -> Void) {
        // Sparkle permits retry when termination was canceled; every retry rechecks activity.
        if !applicationTerminated { restartChoice = retryTerminatingApplication }
    }
    func showUpdateInstalledAndRelaunched(_ relaunched: Bool, acknowledgement: @escaping () -> Void) { acknowledgement() }
    func dismissUpdateInstallation() { checking = false; changed?() }
    func showUpdateInFocus() { changed?(); if let notice { emit?(notice) } }
}
