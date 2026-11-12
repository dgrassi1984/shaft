import Foundation
import Testing
@testable import SHAFTKit

// §19: the version gate (GET /app/v1/version) and, on the Mac, Update server (`shaft deploy --check` / `shaft deploy`)
// against scripted answers: nothing here runs a real `shaft` or reaches a server.

@MainActor
struct ServerCompatibilityTests {
    @Test func aServerAtOrAboveTheMinimumIsCompatible() {
        #expect(ServerCompatibility.verdict(.success(ServerVersion(server: "v0.60.0", api: 1)), minimum: 1)
                == .compatible(server: "v0.60.0"))
        #expect(ServerCompatibility.verdict(.success(ServerVersion(server: "v0.70.0", api: 3)), minimum: 2)
                == .compatible(server: "v0.70.0"))
    }

    @Test func aLowerAPIVersionNeedsANewerServer() {
        #expect(ServerCompatibility.verdict(.success(ServerVersion(server: "v0.60.0", api: 1)), minimum: 2)
                == .needsNewerServer(server: "v0.60.0"))
    }

    @Test func aServerWithoutTheRouteNeedsANewerServer() {
        let missing = APIError.server(status: 404, error: ServerError(code: "not_found", message: "Nothing at /app/v1/version."))
        #expect(ServerCompatibility.verdict(.failure(missing)) == .needsNewerServer(server: nil))
        #expect(ServerCompatibility.verdict(.failure(.http(status: 404, body: "<html>"))) == .needsNewerServer(server: nil))
    }

    @Test func anUnreachableServerBlocksNothing() {
        let down = APIError.transport(code: -1004, message: "Could not connect to the server.")
        #expect(ServerCompatibility.verdict(.failure(down)) == .unknown("Could not connect to the server."))
        let broken = APIError.server(status: 500, error: ServerError(code: "internal", message: "boom"))
        #expect(ServerCompatibility.verdict(.failure(broken)) == .unknown("boom"))
    }

    @Test func theAppNeedsAPIVersionOne() {
        #expect(ServerCompatibility.minimumServerAPI == 1)
        #expect(ServerCompatibility.message(for: "v0.40.0") == "This app needs a newer SHAFT server (the server runs v0.40.0).")
        #expect(ServerCompatibility.message(for: nil) == "This app needs a newer SHAFT server.")
    }

    @Test func theStoreAsksTheVersionRouteAndKeepsTheVerdict() async throws {
        let folder = FileManager.default.temporaryDirectory.appending(path: "compat-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { request in
            #expect(request.path == "/app/v1/version")
            return .json(404, #"{"error":{"code":"not_found","message":"Nothing at /app/v1/version."}}"#)
        }
        let store = InstancesStore(client: server.client, outbox: try Outbox(directory: folder.appending(path: "o")),
                                   drafts: DraftStore(directory: folder.appending(path: "d")))
        #expect(store.compatibility == nil)
        await store.checkCompatibility()
        #expect(store.compatibility == .needsNewerServer(server: nil))
        server.setAnswer { _ in .json(200, #"{"server":"v0.61.0","api":1}"#) }
        await store.checkCompatibility()
        #expect(store.compatibility == .compatible(server: "v0.61.0"))
    }
}

#if os(macOS)
/// `shaft` as the updater runs it: each call is recorded and answered from the script (its stdout lines streamed).
@MainActor
final class ScriptedShaft {
    var calls: [[String]] = []
    var answers: [([String]) throws -> ProcessResult]

    init(_ answers: [([String]) throws -> ProcessResult]) { self.answers = answers }

    func run(_ args: [String], _ onLine: (@Sendable (String) -> Void)?) async throws -> ProcessResult {
        calls.append(args)
        let result = try answers.removeFirst()(args)
        for line in String(decoding: result.stdout, as: UTF8.self).split(separator: "\n") { onLine?(String(line)) }
        return result
    }

    static func lines(_ status: Int32 = 0, _ lines: String...) -> ([String]) throws -> ProcessResult {
        { _ in ProcessResult(status: status, stdout: Data(lines.joined(separator: "\n").utf8), stderr: Data()) }
    }
}

private func row(_ step: String, ok: Bool = true, detail: String = "done", extra: String = "") -> String {
    #"{"step":"\#(step)","ok":\#(ok),"detail":"\#(detail)"\#(extra)}"#
}

private let allSteps = ["preflight", "fetch", "main", "template", "update", "scheduler"]

@MainActor
struct ServerUpdaterTests {
    let repository = URL(string: "https://github.com/example/shaft")!
    /// The checkout the updater would deploy in: a real folder, so its canonical path can be compared.
    let checkout = FileManager.default.temporaryDirectory.appending(path: "updater-checkout", directoryHint: .isDirectory)

    private func updater(_ shaft: ScriptedShaft, located: ServerLocation? = nil,
                         reachable: @escaping @MainActor () async -> Bool = { true }) -> ServerUpdater {
        try? FileManager.default.createDirectory(at: checkout, withIntermediateDirectories: true)
        let location = located ?? .project(checkout.path)
        return ServerUpdater(repository: repository, checkout: checkout, run: { try await shaft.run($0, $1) },
                             locate: { location }, reachable: reachable, sleep: { _ in })
    }

    @Test func nothingIsOfferedForAServerRunningFromAnotherCheckout() async {
        let shaft = ScriptedShaft([])
        let other = updater(shaft, located: .project("/Users/someone/Development/shaft"))
        other.checkForUpdate()
        await other.finished()
        #expect(other.check == .elsewhere(project: "/Users/someone/Development/shaft"))
        other.update()
        let silent = updater(shaft, located: .notSaid)       // a server before `project`, or not over loopback
        silent.checkForUpdate()
        await silent.finished()
        #expect(silent.check == .unverified)
        let down = updater(shaft, located: .unreachable("Could not connect to the server."))
        down.checkForUpdate()
        await down.finished()
        #expect(down.check == .failed("Could not connect to the server."))
        #expect(shaft.calls.isEmpty)                          // no `shaft deploy` ran for any of them
    }

    @Test func theServerSaysWhereItRunsFromOnlyWithProject() async {
        let server = StubServer { _ in .json(200, #"{"server":"v0.61.0","api":1,"project":"/Users/op/SHAFT"}"#) }
        #expect(await ServerLocation.of(server.client) == .project("/Users/op/SHAFT"))
        server.setAnswer { _ in .json(200, #"{"server":"v0.61.0","api":1}"#) }        // the tailnet's answer
        #expect(await ServerLocation.of(server.client) == .notSaid)
        server.setAnswer { _ in .json(404, #"{"error":{"code":"not_found","message":"Nothing at /app/v1/version."}}"#) }
        #expect(await ServerLocation.of(server.client) == .notSaid)
        server.setAnswer { _ in .json(500, #"{"error":{"code":"internal","message":"boom"}}"#) }
        #expect(await ServerLocation.of(server.client) == .unreachable("boom"))
        #expect(await ServerLocation.of(nil) == .unreachable("This app is not paired."))
    }

    @Test func theSameCheckoutThroughASymlinkIsTheSame() throws {
        let link = FileManager.default.temporaryDirectory.appending(path: "updater-link-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: checkout, withIntermediateDirectories: true)
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: checkout)
        defer { try? FileManager.default.removeItem(at: link) }
        #expect(ServerUpdater.same(link.path, checkout.path))
        #expect(!ServerUpdater.same("/nowhere/at/all", checkout.path))
    }

    @Test func anUnfinishedDeployIsOfferedToFinish() async {
        let finish = allSteps.map { row($0) } + [row("services"), row("verify")]
        let shaft = ScriptedShaft([
            ScriptedShaft.lines(0, #"{"ok":true,"current":"v0.61.0","latest":null,"template_tag":true,"incomplete":"v0.61.0","incomplete_step":"update"}"#),
            { _ in ProcessResult(status: 0, stdout: Data(finish.joined(separator: "\n").utf8), stderr: Data()) },
        ])
        let updater = updater(shaft)
        updater.checkForUpdate()
        await updater.finished()
        #expect(updater.check == .incomplete(tag: "v0.61.0", step: "update", current: "v0.61.0"))
        updater.update()
        await updater.finished()
        #expect(shaft.calls.last == ["deploy", "v0.61.0", "--json"])
        #expect(updater.phase == .done)
        #expect(updater.check == .upToDate(current: "v0.61.0"))
    }

    @Test func theCheckOffersTheNewerRelease() async {
        let shaft = ScriptedShaft([ScriptedShaft.lines(0, #"{"ok":true,"current":"v0.60.0","latest":"v0.61.0","template_tag":true}"#)])
        let updater = updater(shaft)
        updater.checkForUpdate()
        #expect(updater.check == .checking)
        await updater.finished()
        #expect(shaft.calls == [["deploy", "--check", "--json"]])
        #expect(updater.check == .available(latest: "v0.61.0", current: "v0.60.0"))
        #expect(updater.releasePage("v0.61.0").absoluteString == "https://github.com/example/shaft/releases/tag/v0.61.0")
    }

    @Test func theCheckSaysUpToDateOrTheRealError() async {
        let shaft = ScriptedShaft([
            ScriptedShaft.lines(0, #"{"ok":true,"current":"v0.61.0","latest":null,"template_tag":true}"#),
            ScriptedShaft.lines(1, #"{"ok":false,"error":"git ls-remote origin failed: could not resolve host"}"#),
            { _ in ProcessResult(status: 2, stdout: Data(), stderr: Data("shaft: error: invalid choice: 'deploy'".utf8)) },
        ])
        let updater = updater(shaft)
        updater.checkForUpdate()
        await updater.finished()
        #expect(updater.check == .upToDate(current: "v0.61.0"))
        updater.update()                                   // nothing offered: nothing runs
        #expect(shaft.calls.count == 1)
        updater.checkForUpdate()
        await updater.finished()
        #expect(updater.check == .failed("git ls-remote origin failed: could not resolve host"))
        updater.checkForUpdate()                           // a server older than the verb
        await updater.finished()
        #expect(updater.check == .failed("shaft deploy --check failed (exit 2): shaft: error: invalid choice: 'deploy'"))
    }

    @Test func anUpdateStreamsItsStepsReconnectsAndEndsUpToDate() async {
        let lines = allSteps.map { row($0) } + [row("services", detail: "restarted api, web", extra: #","restarted":["api","web"]"#),
                                                row("verify")]
        let shaft = ScriptedShaft([
            ScriptedShaft.lines(0, #"{"ok":true,"current":"v0.60.0","latest":"v0.61.0","template_tag":true}"#),
            { _ in ProcessResult(status: 0, stdout: Data(lines.joined(separator: "\n").utf8), stderr: Data()) },
        ])
        var probes = 0
        var finished = 0
        let updater = updater(shaft, reachable: {
            probes += 1
            return probes >= 3                              // the restarted web service answers on the third try
        })
        updater.onFinished = { finished += 1 }
        updater.checkForUpdate()
        await updater.finished()
        updater.update()
        #expect(updater.phase == .running)
        await updater.finished()
        #expect(shaft.calls.last == ["deploy", "v0.61.0", "--json"])
        #expect(updater.rows.map(\.step) == allSteps + ["services", "verify"])
        #expect(updater.rows.first { $0.step == "services" }?.restarted == ["api", "web"])
        #expect(probes == 3)
        #expect(updater.reconnecting == false)
        #expect(updater.phase == .done)
        #expect(updater.check == .upToDate(current: "v0.61.0"))
        #expect(finished == 1)
    }

    @Test func aWaitingServicesStepOffersRetryWithServicesOnly() async {
        let first = allSteps.map { row($0) } + [row("services", detail: "waiting: alpha (1 open conversation)",
                                                    extra: #","waiting":true"#)]
        let shaft = ScriptedShaft([
            ScriptedShaft.lines(0, #"{"ok":true,"current":"v0.60.0","latest":"v0.61.0","template_tag":true}"#),
            { _ in ProcessResult(status: 0, stdout: Data(first.joined(separator: "\n").utf8), stderr: Data()) },
            ScriptedShaft.lines(0, row("services", detail: "restarted api", extra: #","restarted":["api"]"#), row("verify")),
        ])
        let updater = updater(shaft)
        updater.checkForUpdate()
        await updater.finished()
        updater.update()
        await updater.finished()
        #expect(updater.phase == .waiting("waiting: alpha (1 open conversation)"))
        #expect(updater.reconnecting == false)
        updater.retryServices()
        await updater.finished()
        #expect(shaft.calls.last == ["deploy", "v0.61.0", "--json", "--services-only"])
        #expect(updater.rows.map(\.step) == allSteps + ["services", "verify"])   // the earlier steps stay shown
        #expect(updater.rows.first { $0.step == "services" }?.waiting == false)
        #expect(updater.phase == .done)
    }

    @Test func aRefusedStepFailsWithItsOwnError() async {
        let shaft = ScriptedShaft([
            ScriptedShaft.lines(0, #"{"ok":true,"current":"v0.60.0","latest":"v0.61.0","template_tag":true}"#),
            ScriptedShaft.lines(1, row("preflight", ok: false, detail: "dirty",
                                       extra: #","error":"the primary checkout has tracked changes: supervisor/core.py""#)),
        ])
        let updater = updater(shaft)
        updater.checkForUpdate()
        await updater.finished()
        updater.update()
        await updater.finished()
        #expect(updater.phase == .failed("the primary checkout has tracked changes: supervisor/core.py"))
        #expect(updater.rows.map(\.ok) == [false])
        #expect(updater.check == .available(latest: "v0.61.0", current: "v0.60.0"))   // still offered: Try again
    }

    @Test func aServerThatNeverAnswersAgainIsSaid() async {
        let lines = allSteps.map { row($0) } + [row("services", extra: #","restarted":["web"]"#), row("verify")]
        let shaft = ScriptedShaft([
            ScriptedShaft.lines(0, #"{"ok":true,"current":"v0.60.0","latest":"v0.61.0","template_tag":true}"#),
            { _ in ProcessResult(status: 0, stdout: Data(lines.joined(separator: "\n").utf8), stderr: Data()) },
        ])
        let updater = updater(shaft, reachable: { false })
        updater.checkForUpdate()
        await updater.finished()
        updater.update()
        await updater.finished()
        guard case .failed(let why) = updater.phase else {
            Issue.record("expected a failure, got \(updater.phase)")
            return
        }
        #expect(why.contains("has not answered since its restart"))
        #expect(updater.reconnecting == false)
    }

    @Test func aStoppedRunChangesNothing() async {
        let shaft = ScriptedShaft([
            ScriptedShaft.lines(0, #"{"ok":true,"current":"v0.60.0","latest":"v0.61.0","template_tag":true}"#),
        ])
        let updater = updater(shaft)
        updater.checkForUpdate()
        updater.stop()
        await updater.finished()
        #expect(updater.check == .idle)
    }

    @Test func deployRowsReadTheirOptionalFields() {
        #expect(DeployRow.parse(row("services", detail: "waiting: a", extra: #","waiting":true"#))
                == DeployRow(step: "services", ok: true, detail: "waiting: a", waiting: true))
        #expect(DeployRow.parse(#"{"ok":true,"current":"v1"}"#) == nil)
        #expect(DeployRow.parse("not json") == nil)
    }
}
#endif
