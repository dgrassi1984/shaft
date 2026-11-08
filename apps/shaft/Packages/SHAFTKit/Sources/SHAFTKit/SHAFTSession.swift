import Foundation
import Observation

/// The app's root state: paired or not, and when paired the `InstancesStore`. Pairing, unpairing and push
/// registration go through it, so a `401 unpaired` anywhere brings the pairing screen back.
@MainActor
@Observable
public final class SHAFTSession {
    public private(set) var credential: ServerCredential?
    public private(set) var store: InstancesStore?
    /// Why the session could not start or pair (the Keychain, the server's refusal), for the pairing screen.
    public private(set) var problem: String?
    /// The push registration's answer: on, or why not (`503 push_off` names what the server lacks).
    public private(set) var pushStatus: String?

    @ObservationIgnored public let pairing: PairingStore
    /// The server's confirmed badge, or zero when unpaired, independent of the app's windows.
    @ObservationIgnored public var onBadge: (@MainActor (Int) -> Void)?
    @ObservationIgnored private let outbox: Outbox
    @ObservationIgnored private let drafts: DraftStore
    @ObservationIgnored private let session: URLSession
    @ObservationIgnored private var deviceToken: Data?

    public init(pairing: PairingStore, outbox: Outbox, drafts: DraftStore, session: URLSession = .shared) {
        self.pairing = pairing
        self.outbox = outbox
        self.drafts = drafts
        self.session = session
        do {
            if let credential = try pairing.current() { try open(credential) }
        } catch {
            problem = error.localizedDescription
        }
    }

    /// The session for this app: the shared Keychain item, the drafts and the outbox under Application Support.
    public static func standard() throws -> SHAFTSession {
        SHAFTSession(pairing: PairingStore(store: try KeychainStore.forMainBundle()),
                     outbox: try Outbox.standard(), drafts: try DraftStore.standard())
    }

    public var isPaired: Bool { credential != nil }

    /// Pair with a pasted link (§2). False when it failed: `problem` says why.
    @discardableResult
    public func pair(pasted: String) async -> Bool {
        do {
            try open(try await pairing.pair(pasted: pasted))
            problem = nil
            if let deviceToken { await registerPush(token: deviceToken) }
            return true
        } catch {
            guard let error = APIError.from(error) else { return false }
            problem = error.message
            return false
        }
    }

    /// Unpair on the server and here.
    public func unpair() async {
        do {
            try await pairing.unpair()
            close()
        } catch {
            guard let error = APIError.from(error) else { return }
            problem = error.message
        }
    }

    /// Forget the pairing here only (the server could not be reached).
    public func forget() {
        try? pairing.forget()
        close()
    }

    /// Hand the APNs token over (`didRegisterForRemoteNotificationsWithDeviceToken`). Kept, and sent again after a
    /// pairing, since it may come before one.
    public func registerPush(token: Data) async {
        deviceToken = token
        guard let store else { return }
        do {
            let answer = try await PushRegistrar(client: store.client).register(token: token)
            pushStatus = answer.push.on ? "Notifications on (\(answer.push.env ?? "?"))" : answer.push.reason
        } catch {
            guard let error = APIError.from(error) else { return }
            pushStatus = error.message
        }
    }

    private func open(_ credential: ServerCredential) throws {
        let outbox = try Outbox(directory: LocalFiles.namespaced(self.outbox.directory, origin: credential.baseURL))
        let drafts = try DraftStore(directory: LocalFiles.namespaced(self.drafts.directory, origin: credential.baseURL))
        store?.onBadge = nil
        store?.stop()
        self.credential = credential
        let store = InstancesStore(client: APIClient(credential: credential, session: session), outbox: outbox,
                                   drafts: drafts)
        store.onUnpaired = { [weak self] in self?.lostPairing() }
        store.onBadge = { [weak self] count in self?.onBadge?(count) }
        self.store = store
    }

    private func close() {
        store?.onBadge = nil
        store?.stop()
        store?.outbox.sender = nil
        store = nil
        credential = nil
        onBadge?(0)
    }

    /// The server no longer knows this device (`shaft web unpair`): forget it and show the pairing screen.
    private func lostPairing() {
        try? pairing.forget()
        close()
        problem = "This device was unpaired on the Mac. Pair it again with a new link."
    }
}
