import Foundation
import Observation
import WatchConnectivity

/// What the watch app knows and does. Everything goes through the iPhone (WatchProtocol.swift): the watch has no
/// network of its own and holds no credential.
@MainActor
@Observable
final class WatchModel: NSObject {
    /// Where a conversation with one instance stands.
    enum Phase: Equatable {
        case idle
        /// The dictated text is on its way to the iPhone and the server.
        case sending(String)
        /// The server took it (`said`); the reply is being written.
        case waiting(String)
        case reply(WatchReplyPayload)
        /// It could not be sent or followed: why, and whether Retry sends the same message again.
        case failed(String, retry: Bool)
    }

    private(set) var instances: [String] = []
    /// Why the instance list could not be read (iPhone out of reach, not paired…), nil while fine.
    private(set) var listProblem: String?
    private(set) var loadingList = false
    private(set) var phase: [String: Phase] = [:]
    let speaker = WatchSpeaker()

    /// The last message sent to each instance, kept for Retry: the same `mid`, so a retry never sends twice.
    private var lastSent: [String: (text: String, mid: String)] = [:]

    func activate() {
        guard WCSession.isSupported() else {
            listProblem = "This watch can't talk to an iPhone."
            return
        }
        WCSession.default.delegate = self
        WCSession.default.activate()
    }

    func phase(of instance: String) -> Phase { phase[instance] ?? .idle }

    func refreshInstances() {
        loadingList = true
        ask(.instances) { [weak self] answer in
            guard let self else { return }
            self.loadingList = false
            if let error = answer.error {
                self.listProblem = error
            } else {
                self.listProblem = answer.instances.isEmpty ? "No instances on this server yet." : nil
                self.instances = answer.instances
                #if DEBUG
                self.runDevTurnIfAsked()
                #endif
            }
        }
    }

    #if DEBUG
    /// The instance a Debug run opened by itself (`SHAFT_WATCH_SAY`), for the list to show.
    private(set) var devOpen: String?

    /// `SHAFT_WATCH_SAY=<text>` (Debug builds, `SIMCTL_CHILD_SHAFT_WATCH_SAY` through simctl): open the first instance
    /// and send that text as if dictated, once. A simulator's dictation sheet can't be driven from a script; the rest
    /// of the turn (the iPhone's send, the reply pushed back, the speech) is the real path.
    /// `SHAFT_WATCH_OPEN=1` opens the first instance without sending anything: its latest reply shows, as for the App
    /// Store screenshot (AppStore/take-screenshots.sh).
    private func runDevTurnIfAsked() {
        let environment = ProcessInfo.processInfo.environment
        guard devOpen == nil, let first = instances.first else { return }
        if let say = environment["SHAFT_WATCH_SAY"], !say.isEmpty {
            devOpen = first
            send(say, to: first)
        } else if environment["SHAFT_WATCH_OPEN"] == "1" {
            devOpen = first
        }
    }
    #endif

    func send(_ text: String, to instance: String) {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        let mid = "watch-" + UUID().uuidString.lowercased()
        lastSent[instance] = (text, mid)
        transmit(text, mid: mid, to: instance)
    }

    func retry(_ instance: String) {
        guard let last = lastSent[instance] else { return }
        transmit(last.text, mid: last.mid, to: instance)
    }

    /// The reply still running, or the last message the instance wrote: when the screen opens with nothing to show,
    /// or still waiting for a reply the iPhone may never have been able to push (it ran out of background time, the
    /// watch app was not running).
    func loadLatest(_ instance: String) {
        let before = phase(of: instance)
        switch before {
        case .idle, .waiting: break
        default: return
        }
        ask(.latest(instance: instance)) { [weak self] answer in
            guard let self, self.phase(of: instance) == before, let reply = answer.reply else { return }
            self.phase[instance] = .reply(reply)
        }
    }

    private func transmit(_ text: String, mid: String, to instance: String) {
        speaker.stop()
        phase[instance] = .sending(text)
        ask(.send(instance: instance, text: text, mid: mid)) { [weak self] answer in
            guard let self, self.lastSent[instance]?.mid == mid else { return }
            // The reply may have been pushed before this answer (or its error) arrived: keep it then.
            guard case .sending = self.phase(of: instance) else { return }
            if let error = answer.error {
                self.phase[instance] = .failed(error, retry: true)
            } else {
                self.phase[instance] = .waiting(answer.said ?? "Sent.")
            }
        }
    }

    /// A reply the iPhone pushed: shown and spoken unless a newer turn with that instance is known. After a restart
    /// nothing is known, and a reply queued for the watch (`transferUserInfo`) is still shown.
    private func received(_ reply: WatchReplyPayload) {
        if let mid = reply.mid, let last = lastSent[reply.instance]?.mid, last != mid { return }   // an older turn
        if reply.text.isEmpty, let failure = reply.failure {
            phase[reply.instance] = .failed(failure, retry: false)
            return
        }
        phase[reply.instance] = .reply(reply)
        speaker.speak(reply)
    }

    private func ask(_ request: WatchRequest, then: @escaping @MainActor (WatchAnswer) -> Void) {
        let session = WCSession.default
        guard session.activationState == .activated else {
            then(.failed("Still connecting to your iPhone. Try again in a moment."))
            return
        }
        guard session.isReachable else {
            then(.failed("Your iPhone is out of reach. Keep it nearby, with SHAFT installed."))
            return
        }
        // WatchConnectivity calls both handlers on its own queue: they must not be main-actor closures (Swift 6 traps
        // when one runs off the main actor), so they are @Sendable and hop to the main actor themselves.
        session.sendMessage(request.dictionary, replyHandler: { @Sendable reply in
            let answer = WatchAnswer(reply)
            Task { @MainActor in then(answer) }
        }, errorHandler: { @Sendable error in
            let message = error.localizedDescription
            Task { @MainActor in then(.failed("Your iPhone didn't answer: \(message)")) }
        })
    }
}

extension WatchModel: WCSessionDelegate {
    nonisolated func session(_ session: WCSession, activationDidCompleteWith activationState: WCSessionActivationState,
                             error: (any Error)?) {
        Task { @MainActor in
            if activationState == .activated { self.refreshInstances() }
        }
    }

    nonisolated func sessionReachabilityDidChange(_ session: WCSession) {
        let reachable = session.isReachable
        Task { @MainActor in
            if reachable, self.instances.isEmpty { self.refreshInstances() }
        }
    }

    nonisolated func session(_ session: WCSession, didReceiveMessage message: [String: Any]) {
        guard let reply = WatchReplyPayload(message) else { return }
        Task { @MainActor in self.received(reply) }
    }

    nonisolated func session(_ session: WCSession, didReceiveUserInfo userInfo: [String: Any] = [:]) {
        guard let reply = WatchReplyPayload(userInfo) else { return }
        Task { @MainActor in self.received(reply) }
    }
}
