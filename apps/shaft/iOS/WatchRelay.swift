import SHAFTKit
import UIKit
import WatchConnectivity

/// The iPhone end of the Apple Watch app. The watch has no network of its own (no VPN on watchOS, so no tailnet) and
/// no credential: it sends its requests here over WatchConnectivity (SHAFTKit WatchProtocol.swift), and this app
/// answers them with its own pairing (SHAFTKit WatchTurn.swift).
///
/// - `instances`: their names, read fresh from the server.
/// - `send`: sent as a voice message, answered as soon as the server took it; the reply is then followed to its end
///   and pushed to the watch (`sendMessage` while it is reachable, else `transferUserInfo`, which the system delivers
///   when it can). iOS wakes this app in the background for a watch message and gives it a short while: the follow
///   runs inside a background task, and a reply that outlasts it still reaches the watch as the usual notification
///   and through `latest`.
/// - `latest`: the reply still running, else the last message the instance wrote.
@MainActor
final class WatchRelay: NSObject {
    private let model: AppModel

    init(model: AppModel) {
        self.model = model
    }

    /// Activate the session at launch, so a message that woke the app in the background is delivered.
    func start() {
        guard WCSession.isSupported() else { return }
        WCSession.default.delegate = self
        WCSession.default.activate()
    }

    private func handle(_ request: WatchRequest, answer: ReplyHandler) {
        guard let client = model.session?.store?.client else {
            answer(.failed("Open SHAFT on your iPhone and pair it first."))
            return
        }
        switch request {
        case .instances:
            Task {
                do {
                    guard case .value(let list, _) = try await client.instances() else {
                        // No etag was sent, so the server can't answer 304; say so rather than show an empty list.
                        return answer(.failed("The server sent no instance list."))
                    }
                    answer(WatchAnswer(instances: list.instances.map(\.name)))
                } catch {
                    answer(.failed(WatchTurn.describe(error)))
                }
            }
        case .send(let instance, let text, let mid):
            let task = BackgroundTask(name: "Watch turn")
            Task {
                defer { task.end() }
                let sent: SendResponse
                do {
                    sent = try await WatchTurn.send(text, mid: mid, to: instance, client: client)
                } catch {
                    return answer(.failed(WatchTurn.describe(error)))
                }
                answer(WatchAnswer(said: sent.said))
                do {
                    let language = VoicePreferences.language(for: instance)
                    // Always an answer for the watch's turn, so it never waits on a reply that will not come.
                    let reply = try await WatchTurn.reply(to: mid, in: instance, chat: sent.chat, client: client,
                                                          fallback: language)
                        ?? WatchReplyPayload(instance: instance, mid: mid, text: "", spoken: [],
                                             language: language.bcp47, voice: nil,
                                             failure: "\(instance) wrote no reply.")
                    Self.push(reply)
                } catch {
                    Self.push(WatchReplyPayload(instance: instance, mid: mid, text: "", spoken: [],
                                                language: VoicePreferences.language(for: instance).bcp47,
                                                voice: nil, failure: WatchTurn.describe(error)))
                }
            }
        case .latest(let instance):
            Task {
                do {
                    let reply = try await WatchTurn.latest(instance, client: client,
                                                           fallback: VoicePreferences.language(for: instance))
                    answer(WatchAnswer(reply: reply))
                } catch {
                    answer(.failed(WatchTurn.describe(error)))
                }
            }
        }
    }

    /// A reply to the watch: at once while its app is reachable, else queued for when it is.
    private static func push(_ reply: WatchReplyPayload) {
        let session = WCSession.default
        guard session.activationState == .activated, session.isPaired, session.isWatchAppInstalled else { return }
        let payload = reply.dictionary
        if session.isReachable {
            // Called on WatchConnectivity's queue: @Sendable, never a main-actor closure (Swift 6 traps on that).
            session.sendMessage(payload, replyHandler: nil) { @Sendable _ in
                // It went out of reach meanwhile: queue it instead.
                WCSession.default.transferUserInfo(payload)
            }
        } else {
            session.transferUserInfo(payload)
        }
    }
}

extension WatchRelay: WCSessionDelegate {
    nonisolated func session(_ session: WCSession, activationDidCompleteWith activationState: WCSessionActivationState,
                             error: (any Error)?) {}

    nonisolated func sessionDidBecomeInactive(_ session: WCSession) {}

    /// The operator switched watches: activate again for the new one.
    nonisolated func sessionDidDeactivate(_ session: WCSession) {
        session.activate()
    }

    nonisolated func session(_ session: WCSession, didReceiveMessage message: [String: Any],
                             replyHandler: @escaping ([String: Any]) -> Void) {
        let answer = ReplyHandler(replyHandler)
        guard let request = WatchRequest(message) else {
            answer(.failed("This iPhone app is older than the watch app: update it."))
            return
        }
        Task { @MainActor in self.handle(request, answer: answer) }
    }
}

/// WatchConnectivity's reply handler, carried to the main actor. It is called once, from there.
private struct ReplyHandler: @unchecked Sendable {
    let reply: ([String: Any]) -> Void
    init(_ reply: @escaping ([String: Any]) -> Void) { self.reply = reply }
    func callAsFunction(_ answer: WatchAnswer) { reply(answer.dictionary) }
}

/// Time to finish a watch turn after iOS woke the app in the background for it.
@MainActor
final class BackgroundTask {
    private var id: UIBackgroundTaskIdentifier = .invalid
    var onExpiration: (@MainActor @Sendable () -> Void)?

    init(name: String) {
        id = UIApplication.shared.beginBackgroundTask(withName: name) { @Sendable [weak self] in
            Task { @MainActor in
                self?.onExpiration?()
                self?.end()
            }
        }
    }

    func end() {
        guard id != .invalid else { return }
        UIApplication.shared.endBackgroundTask(id)
        id = .invalid
    }
}
