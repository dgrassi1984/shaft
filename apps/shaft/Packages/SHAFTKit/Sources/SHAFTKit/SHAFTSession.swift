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
    /// How the last pairing ended, nil while paired or when none ended in this run: the Mac app's root gate pairs a
    /// lost local pairing again on its own, but never one the operator ended.
    public private(set) var loss: PairingLoss?
    /// The push registration's answer: on, or why not (`503 push_off` names what the server lacks).
    public private(set) var pushStatus: String?

    @ObservationIgnored public let pairing: PairingStore
    /// The server's confirmed badge, or zero when unpaired, independent of the app's windows.
    @ObservationIgnored public var onBadge: (@MainActor (Int) -> Void)?
    @ObservationIgnored private let outbox: Outbox
    @ObservationIgnored private let drafts: DraftStore
    @ObservationIgnored private let session: URLSession
    @ObservationIgnored private var deviceToken: Data?
    @ObservationIgnored private var demoRun: (id: String, session: URLSession)?
    /// The throwaway storage for this run, removed on leave.
    public private(set) var demoDirectory: URL?
    public private(set) var isDemo = false

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

    /// Open a fresh offline demo without saving a pairing or using the paired server's stores.
    public func startDemo() {
        guard !isPaired else { return }
        let directory = FileManager.default.temporaryDirectory.appending(path: "shaft-demo-\(UUID().uuidString)")
        do {
            let outbox = try Outbox(directory: directory.appending(path: "outbox"))
            let drafts = DraftStore(directory: directory.appending(path: "drafts"))
            try FileManager.default.createDirectory(at: drafts.directory, withIntermediateDirectories: true)
            let run = DemoServer.start()
            let credential = ServerCredential(baseURL: DemoServer.baseURL, device: "demo", secret: "demo",
                                              name: "demo", login: "demo")
            demoRun = run
            demoDirectory = directory
            isDemo = true
            self.credential = credential
            let store = InstancesStore(client: APIClient(credential: credential, session: run.session),
                                       outbox: outbox, drafts: drafts)
            store.onBadge = { [weak self] count in self?.onBadge?(count) }
            self.store = store
            problem = nil
            loss = nil
            pushStatus = "Not used in the demo"
        } catch {
            try? FileManager.default.removeItem(at: directory)
            problem = error.localizedDescription
        }
    }

    /// Discard the demo's conversations and files and return to the pairing screen.
    public func leaveDemo() {
        guard isDemo else { return }
        store?.outbox.discard()
        close()
        // Remove the run before cancelling. A send still preparing its upload gets a local refusal.
        if let demoRun { DemoServer.finish(demoRun.id) }
        demoRun?.session.getAllTasks { tasks in tasks.forEach { $0.cancel() } }
        demoRun = nil
        if let demoDirectory { try? FileManager.default.removeItem(at: demoDirectory) }
        demoDirectory = nil
        isDemo = false
        pushStatus = nil
        problem = nil
        loss = nil
    }

    /// Pair with a pasted link (§2). False when it failed: `problem` says why.
    @discardableResult
    public func pair(pasted: String) async -> Bool {
        guard !isDemo else { return false }
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

    /// Pair with a credential made without a link: the Mac app's local pairing (`shaft web pair --local`, run by the
    /// setup's pair step). It is kept through the `PairingStore` and the session opens on it; nothing is asked of the
    /// server. False when it failed: `problem` says why.
    @discardableResult
    public func pair(credential: ServerCredential) async -> Bool {
        guard !isDemo else { return false }
        do {
            try pairing.save(credential)
            try open(credential)
            problem = nil
            if let deviceToken { await registerPush(token: deviceToken) }
            return true
        } catch {
            problem = error.localizedDescription
            return false
        }
    }

    /// Unpair on the server and here.
    public func unpair() async {
        if isDemo { leaveDemo(); return }
        do {
            try await pairing.unpair()
            close()
            loss = .operatorChose
        } catch {
            guard let error = APIError.from(error) else { return }
            problem = error.message
        }
    }

    /// Forget the pairing here only (the server could not be reached).
    public func forget() {
        if isDemo { leaveDemo(); return }
        try? pairing.forget()
        close()
        loss = .operatorChose
    }

    /// Hand the APNs token over (`didRegisterForRemoteNotificationsWithDeviceToken`). Kept, and sent again after a
    /// pairing, since it may come before one.
    public func registerPush(token: Data) async {
        deviceToken = token
        guard !isDemo else { pushStatus = "Not used in the demo"; return }
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
        loss = nil
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
        let server = credential?.baseURL
        try? pairing.forget()
        close()
        loss = server.map(PairingLoss.server)
        problem = server.map(PairingLoss.isLoopback) == true
            ? "The SHAFT server on this Mac no longer knows this app (401 unpaired): its pairing was revoked or the server was reinstalled."
            : "This device was unpaired on the Mac. Pair it again with a new link."
    }
}

/// How a pairing ended.
public enum PairingLoss: Equatable, Sendable {
    /// The operator unpaired or forgot it (Settings).
    case operatorChose
    /// The server at this address answered `401 unpaired`: it no longer knows the device.
    case server(URL)

    /// The server was this Mac's own (`127.0.0.1`, `localhost`, `::1`): the local pairing the Mac app makes itself.
    public var isLocal: Bool {
        if case .server(let url) = self { return Self.isLoopback(url) }
        return false
    }

    public static func isLoopback(_ url: URL) -> Bool {
        ["127.0.0.1", "localhost", "::1", "[::1]"].contains(url.host()?.lowercased() ?? "")
    }
}
