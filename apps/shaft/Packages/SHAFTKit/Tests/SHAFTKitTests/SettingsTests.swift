import Foundation
import Testing
@testable import SHAFTKit

private let settingsExample = #"""
{"instance":"wisp","paused":false,
 "model":{"value":"new-model","choices":[{"model":"new-model","label":"New model"}]},
 "effort":{"value":"medium","choices":["medium","xhigh","low"]},
 "wake_interval":{"value":5,"floor":10,"effective":10},
 "cloud_cap":{"value":null,"spent_today":0.25},
 "channel":{"value":"auto","choices":["auto","web","telegram"]},
 "operator_name":{"value":"the operator"},
 "voice_mode":{"value":"on","choices":["on","off"]},
 "voice_effort":{"value":"","choices":["","medium","xhigh","low"]}}
"""#

private func serviceExample(pid: Int = 123, running: Bool = true, drain: String = "null") -> String {
    #"{"version":"v0.44.0","services":[{"name":"api","running":\#(running),"pid":\#(pid),"started":"2026-11-05T10:00:00+01:00","commit":"abc","version":"v0.43.0","busy":""},{"name":"scheduler","running":true,"pid":\#(pid),"started":null,"commit":null,"busy":"","drain":\#(drain)}]}"#
}

private let pendingDrain = #"{"state":"draining","waiting":["wisp is replying"],"deadline":"2026-11-05T11:00:00+01:00"}"#

/// Virtual time advances only when the store calls its injected sleep.
private final class RestartClock: @unchecked Sendable {
    private let lock = NSLock()
    private var seconds: TimeInterval = 0
    private var durations: [Duration] = []
    func sleep(_ duration: Duration) {
        lock.withLock {
            durations.append(duration)
            seconds += Double(duration.components.seconds)
        }
    }
    func now() -> Date { lock.withLock { Date(timeIntervalSince1970: seconds) } }
    var sleeps: [Duration] { lock.withLock { durations } }
}

@MainActor
struct SettingsTests {
    private func makeOwner(_ server: StubServer, folder: URL) throws -> InstancesStore {
        InstancesStore(client: server.client, outbox: try Outbox(directory: folder.appending(path: "outbox")),
                       drafts: DraftStore(directory: folder.appending(path: "drafts")))
    }

    @Test func section15SettingsAndServicesDecodeWithTheirWireTypes() throws {
        let settings = try SHAFTJSON.decoder().decode(InstanceSettings.self, from: Data(settingsExample.utf8))
        #expect(settings.instance == "wisp")
        #expect(settings.paused == false)
        #expect(settings.model.value == "new-model")
        #expect(settings.model.choices.map(\.model) == ["new-model"])
        #expect(settings.model.choices.map(\.label) == ["New model"])
        #expect(settings.effort.value == "medium")
        #expect(settings.effort.choices == ["medium", "xhigh", "low"])
        #expect(settings.wakeInterval.value == 5)
        #expect(settings.wakeInterval.floor == 10)
        #expect(settings.wakeInterval.effective == 10)
        #expect(settings.cloudCap.value == nil)
        #expect(settings.cloudCap.spentToday == 0.25)
        #expect(settings.channel.value == "auto")
        #expect(settings.channel.choices == ["auto", "web", "telegram"])
        #expect(settings.operatorName.value == "the operator")
        #expect(settings.voiceMode.value == "on")
        #expect(settings.voiceMode.choices == ["on", "off"])
        #expect(settings.voiceEffort.value == "")
        #expect(settings.voiceEffort.choices == ["", "medium", "xhigh", "low"])
        #expect(settings.watchMeetings == nil)                        // a server before the watch fields
        let watches = settingsExample.replacingOccurrences(of: #""voice_effort""#, with:
            #""watch_mail":{"value":"off","choices":["off","inbox","wake"]},"watch_calendar":{"value":"inbox","choices":["off","inbox","wake"]},"watch_gchat":{"value":"wake","choices":["off","inbox","wake"]},"watch_meetings":{"value":"wake","choices":["off","inbox","wake"]},"voice_effort""#)
        let watching = try SHAFTJSON.decoder().decode(InstanceSettings.self, from: Data(watches.utf8))
        #expect(watching.watch(.watchMeetings)?.value == "wake")
        #expect(watching.watch(.watchGchat)?.choices == ["off", "inbox", "wake"])
        #expect(watching.value(for: .watchCalendar) == .string("inbox"))
        #expect(SettingField.watchMeetings.rawValue == "watch_meetings")
        let unavailableVoiceEffort = settingsExample.replacingOccurrences(
            of: #""choices":["","medium","xhigh","low"]"#, with: #""choices":null"#)
        let unavailable = try SHAFTJSON.decoder().decode(
            InstanceSettings.self, from: Data(unavailableVoiceEffort.utf8))
        #expect(unavailable.voiceEffort.choices == nil)
        #expect(unavailable.voiceEffort.value == "")
        let encoded = try #require(JSONSerialization.jsonObject(
            with: SHAFTJSON.encoder().encode(settings)) as? [String: Any])
        #expect((encoded["voice_mode"] as? [String: Any])?["value"] as? String == "on")
        #expect((encoded["voice_effort"] as? [String: Any])?["value"] as? String == "")
        let nullable = settingsExample.replacingOccurrences(of: #""value":"medium","choices":["medium","xhigh","low"]"#,
                                                           with: #""value":null,"choices":null"#)
            .replacingOccurrences(of: #""floor":10"#, with: #""floor":null"#)
            .replacingOccurrences(of: #""value":null,"spent_today"#, with: #""value":2.5,"spent_today"#)
        let other = try SHAFTJSON.decoder().decode(InstanceSettings.self, from: Data(nullable.utf8))
        #expect(other.effort.value == nil)
        #expect(other.effort.choices == nil)
        #expect(other.wakeInterval.floor == nil)
        #expect(other.cloudCap.value == 2.5)
        let response = try SHAFTJSON.decoder().decode(ServicesResponse.self,
            from: Data(serviceExample(drain: pendingDrain).utf8))
        #expect(response.services.count == 2)
        let api = response.services[0]
        #expect(api.name == "api" && api.running && api.pid == 123)
        #expect(api.started == SHAFTJSON.parseStamp("2026-11-05T10:00:00+01:00"))
        #expect(api.commit == "abc" && api.busy == "" && api.drain == nil)
        let scheduler = response.services[1]
        #expect(scheduler.name == "scheduler" && scheduler.running && scheduler.pid == 123)
        #expect(scheduler.started == nil && scheduler.commit == nil && scheduler.busy == "")
        #expect(scheduler.drain?.pending == true)
        #expect(scheduler.drain?.waiting == ["wisp is replying"])
        #expect(scheduler.drain?.deadline == SHAFTJSON.parseStamp("2026-11-05T11:00:00+01:00"))
    }

    @Test func oldDrainStatesAreNotPending() throws {
        for state in ["drained", "abandoned", "cancelled"] {
            let drain = try SHAFTJSON.decoder().decode(ServiceDrain.self,
                from: Data(#"{"state":"\#(state)","waiting":[],"deadline":null}"#.utf8))
            #expect(!drain.pending)
        }
    }

    @Test func modelSaveShowsResetNotes() async throws {
        let folder = try temporaryFolder("settings-model-notes")
        defer { try? FileManager.default.removeItem(at: folder) }
        let note = "LLM_REASONING_EFFORT reset to medium: ternary-bonsai-2-27b-mlx does not take ultra"
        let server = StubServer { request in
            request.method == "GET" ? .json(200, settingsExample)
                : .json(200, #"{"settings":\#(settingsExample),"applies":"next run","notes":["\#(note)"]}"#)
        }
        let owner = try makeOwner(server, folder: folder)
        let store = owner.settings("wisp")
        await store.load()
        #expect(await store.save(.model, value: .string("ternary-bonsai-2-27b-mlx")))
        #expect(store.notice == note + " · Applies from the next run")
    }

    @Test func olderSaveResponseWithoutNotesDecodes() throws {
        let response = try SHAFTJSON.decoder().decode(SettingSave.self,
            from: Data(#"{"settings":\#(settingsExample),"applies":"now"}"#.utf8))
        #expect(response.notes == nil)
        #expect(response.applies == "now")
    }

    @Test func saveReplacesTheShownSettingsWithTheServersAnswer() async throws {
        let folder = try temporaryFolder("settings-save")
        defer { try? FileManager.default.removeItem(at: folder) }
        let fresh = settingsExample.replacingOccurrences(of: "the operator", with: "Server spelling")
        let server = StubServer { request in
            request.method == "GET" ? .json(200, settingsExample)
                : .json(200, #"{"settings":\#(fresh),"applies":"next run"}"#)
        }
        let owner = try makeOwner(server, folder: folder)
        let store = owner.settings("wisp")
        #expect(store === owner.settings("wisp"))
        #expect(owner.services === owner.services)
        await store.load()
        await store.save(.operatorName, value: .string("typed spelling"))
        #expect(store.settings?.operatorName.value == "Server spelling")
        #expect(store.notice == "Applies from the next run")
        #expect(store.saving == nil && store.saveError == nil)
        let request = try #require(server.requests.last)
        #expect(request.path == "/app/v1/settings/wisp")
        #expect(request.json?["field"] as? String == "operator_name")
        #expect(request.json?["value"] as? String == "typed spelling")
        #expect(request.json?["expect"] as? String == "the operator")
    }

    @Test func voiceSettingsSaveTheirWireFieldsAndRefreshTheDisplayedValue() async throws {
        for (field, wire, before, after, applies) in [
            (SettingField.voiceMode, "voice_mode", "on", "off", "now"),
            (SettingField.voiceEffort, "voice_effort", "", "low", "next run")
        ] {
            let folder = try temporaryFolder("voice-settings-" + wire)
            defer { try? FileManager.default.removeItem(at: folder) }
            var freshSettings = try SHAFTJSON.decoder().decode(InstanceSettings.self, from: Data(settingsExample.utf8))
            if field == .voiceMode { freshSettings.voiceMode.value = after }
            else { freshSettings.voiceEffort.value = after }
            let fresh = String(decoding: try SHAFTJSON.encoder().encode(freshSettings), as: UTF8.self)
            let server = StubServer { request in
                request.method == "GET" ? .json(200, settingsExample)
                    : .json(200, #"{"settings":\#(fresh),"applies":"\#(applies)"}"#)
            }
            let owner = try makeOwner(server, folder: folder)
            let store = owner.settings("wisp")
            await store.load()
            #expect(await store.save(field, value: .string(after)))
            let request = try #require(server.requests.last)
            #expect(request.path == "/app/v1/settings/wisp")
            #expect(request.json?["field"] as? String == wire)
            #expect(request.json?["expect"] as? String == before)
            #expect(request.json?["value"] as? String == after)
            #expect(store.settings?.value(for: field) == .string(after))
            #expect(store.notice == (applies == "now" ? "Applies now" : "Applies from the next run"))
            #expect(await store.save(field, value: .string(before)))
            #expect(server.requests.last?.json?["expect"] as? String == after)
        }
    }

    @Test func emptyVoiceEffortIsAStringAndRefusalsKeepBothVoiceSettings() async throws {
        let folder = try temporaryFolder("voice-settings-refused")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { request in
            request.method == "GET" ? .json(200, settingsExample)
                : .json(409, #"{"error":{"code":"refused","message":"Choose a listed voice setting."}}"#)
        }
        let owner = try makeOwner(server, folder: folder)
        let store = owner.settings("wisp")
        await store.load()
        for (field, value) in [(SettingField.voiceMode, "off"), (SettingField.voiceEffort, "")] {
            #expect(!(await store.save(field, value: .string(value))))
            #expect(store.saveError == "Choose a listed voice setting.")
            #expect(store.settings?.voiceMode.value == "on")
            #expect(store.settings?.voiceEffort.value == "")
            #expect(server.requests.last?.json?["value"] as? String == value)
        }
        #expect(server.requests.last?.json?["expect"] as? String == "")
        #expect(server.requests.filter { $0.method == "POST" }.count == 2)
    }

    @Test func numericAndNullableSettingsSendTheShownValueAsExpect() async throws {
        let folder = try temporaryFolder("settings-expect")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { request in
            request.method == "GET" ? .json(200, settingsExample)
                : .json(200, #"{"settings":\#(settingsExample),"applies":"now"}"#)
        }
        let owner = try makeOwner(server, folder: folder)
        let store = owner.settings("wisp")
        await store.load()
        await store.save(.wakeInterval, value: .int(0))
        #expect(server.requests.last?.json?["field"] as? String == "wake_interval")
        #expect(server.requests.last?.json?["expect"] as? Int == 5)
        #expect(server.requests.last?.json?["value"] as? Int == 0)
        await store.save(.cloudCap, value: .double(2.5))
        #expect(server.requests.last?.json?["field"] as? String == "cloud_cap")
        #expect(server.requests.last?.json?["expect"] is NSNull)
        #expect(server.requests.last?.json?["value"] as? Double == 2.5)
        await store.save(.effort, value: .null)
        #expect(server.requests.last?.json?["field"] as? String == "effort")
        #expect(server.requests.last?.json?["expect"] as? String == "medium")
        #expect(server.requests.last?.json?["value"] is NSNull)
    }

    @Test func staleShowsTheFreshValueAndNeverSendsAgain() async throws {
        let folder = try temporaryFolder("settings-stale")
        defer { try? FileManager.default.removeItem(at: folder) }
        let fresh = settingsExample.replacingOccurrences(of: "the operator", with: "Elsewhere")
        let server = StubServer { request in
            request.method == "GET" ? .json(200, settingsExample)
                : .json(409, #"{"error":{"code":"stale","message":"changed","settings":\#(fresh)}}"#)
        }
        let owner = try makeOwner(server, folder: folder)
        let store = owner.settings("wisp")
        await store.load()
        await store.save(.operatorName, value: .string("Mine"))
        #expect(store.settings?.operatorName.value == "Elsewhere")
        #expect(store.saveError?.contains("changed elsewhere") == true)
        #expect(store.notice == nil)
        #expect(server.requests.filter { $0.method == "POST" }.count == 1)
    }

    @Test func refusedKeepsTheOldValueAndShowsTheServersMessage() async throws {
        let folder = try temporaryFolder("settings-refused")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { request in
            request.method == "GET" ? .json(200, settingsExample)
                : .json(409, #"{"error":{"code":"refused","message":"Choose a listed model."}}"#)
        }
        let owner = try makeOwner(server, folder: folder)
        let store = owner.settings("wisp")
        await store.load()
        await store.save(.model, value: .string("invalid"))
        #expect(store.settings?.model.value == "new-model")
        #expect(store.saveError == "Choose a listed model.")
        #expect(server.requests.count == 2)
    }

    @Test func a404MarksBothRoutesUnsupportedAndASuccessClearsIt() async throws {
        let folder = try temporaryFolder("settings-404")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { _ in .json(404, #"{"error":{"code":"not_found","message":"old server"}}"#) }
        let owner = try makeOwner(server, folder: folder)
        let settings = owner.settings("wisp")
        await settings.load()
        await owner.services.load()
        #expect(settings.unsupported && settings.loadError?.status == 404)
        #expect(owner.services.unsupported && owner.services.loadError?.status == 404)
        server.setAnswer { request in
            .json(200, request.path == "/app/v1/services" ? serviceExample() : settingsExample)
        }
        await settings.load()
        await owner.services.load()
        #expect(!settings.unsupported && settings.loadError == nil)
        #expect(!owner.services.unsupported && owner.services.loadError == nil)
    }

    @Test func servicesLoadExposesServerAndServiceVersions() async {
        let server = StubServer { _ in .json(200, serviceExample()) }
        let store = ServicesStore(client: server.client)
        await store.load()
        #expect(store.serverVersion == "v0.44.0")
        #expect(store.services.first?.version == "v0.43.0")
        #expect(store.services.last?.version == nil)
    }

    @Test func servicesWithoutVersionsStillDecode() throws {
        let old = serviceExample()
            .replacingOccurrences(of: #""version":"v0.44.0","#, with: "")
            .replacingOccurrences(of: #""version":"v0.43.0","#, with: "")
        let answer = try SHAFTJSON.decoder().decode(ServicesResponse.self, from: Data(old.utf8))
        #expect(answer.version == nil)
        #expect(answer.services.allSatisfy { $0.version == nil })
    }

    @Test func nullIsSentExplicitlyAndPauseAndRestartCarryLongerTimeouts() async throws {
        let server = StubServer { request in
            if request.path.contains("/services") { return .json(200, serviceExample()) }
            if request.path.hasSuffix("/pause") || request.path.hasSuffix("/resume") {
                return .json(200, #"{"settings":\#(settingsExample)}"#)
            }
            return .json(200, #"{"settings":\#(settingsExample),"applies":"now"}"#)
        }
        let client = server.client
        _ = try await client.saveSetting("wisp", field: .cloudCap, value: .null, expect: .double(2.5))
        let save = try #require(server.requests.last)
        #expect(save.json?["value"] is NSNull)
        #expect(save.json?["expect"] as? Double == 2.5)
        #expect(save.timeout == URLRequest(url: server.baseURL).timeoutInterval)
        _ = try await client.setPaused("wisp", paused: true)
        #expect(server.requests.last?.path == "/app/v1/settings/wisp/pause")
        #expect(server.requests.last?.timeout == 70)
        _ = try await client.setPaused("wisp", paused: false)
        #expect(server.requests.last?.path == "/app/v1/settings/wisp/resume")
        #expect(server.requests.last?.timeout == 70)
        _ = try await client.restart(service: "api")
        #expect(server.requests.last?.timeout == 100)
    }

    @Test func pauseUsesOnlyTheReturnedSettingsAndARefusalKeepsThem() async throws {
        let folder = try temporaryFolder("settings-pause")
        defer { try? FileManager.default.removeItem(at: folder) }
        let paused = settingsExample.replacingOccurrences(of: "false", with: "true")
        let server = StubServer { request in
            request.method == "GET" ? .json(200, settingsExample)
                : .json(200, #"{"settings":\#(paused)}"#)
        }
        let owner = try makeOwner(server, folder: folder)
        let store = owner.settings("wisp")
        await store.load()
        await store.setPaused(true)
        #expect(store.settings?.paused == true)
        #expect(store.notice == "Applies now")
        server.setAnswer { _ in .json(409, #"{"error":{"code":"refused","message":"Cannot resume."}}"#) }
        await store.setPaused(false)
        #expect(store.settings?.paused == true)
        #expect(store.saveError == "Cannot resume.")
        #expect(store.notice == nil)
    }

    @Test func stopEndsSchedulerPollingAndLoadResumesThePendingDrainWithoutAnotherPost() async {
        let sleeps = AsyncStream<Void>.makeStream()
        defer { sleeps.continuation.finish() }
        let server = StubServer { request in
            .json(200, serviceExample(drain: request.method == "POST" ? pendingDrain : "null"))
        }
        let store = ServicesStore(client: server.client, sleep: { duration in
            #expect(duration == .seconds(5))
            sleeps.continuation.yield(())
            try await Task.sleep(for: .seconds(3600))
        })
        await store.load()
        let restart = Task { await store.restart("scheduler") }
        var iterator = sleeps.stream.makeAsyncIterator()
        _ = await iterator.next()
        #expect(store.outcome["scheduler"] == .waiting)
        store.stop()
        await restart.value
        #expect(store.restarting.isEmpty)
        let requestCount = server.requests.count
        server.setAnswer { _ in .json(200, serviceExample(drain: pendingDrain)) }
        await store.load()
        _ = await iterator.next()
        #expect(store.restarting == ["scheduler"])
        #expect(server.requests.count == requestCount + 1)
        store.stop()
        server.setAnswer { _ in .json(200, serviceExample(pid: 456,
            drain: #"{"state":"drained","waiting":[],"deadline":null}"#)) }
        await store.load()
        #expect(store.outcome["scheduler"] == .restarted)
        #expect(server.requests.filter { $0.method == "POST" }.count == 1)
    }

    @Test func cancelPostsOnceAndReportsTheCancelledDrainWithItsWaiters() async {
        let server = StubServer { request in
            if request.method == "POST" {
                return .json(200, serviceExample(drain:
                    #"{"state":"cancelled","waiting":["wisp"],"deadline":null}"#))
            }
            return .json(200, serviceExample())
        }
        let store = ServicesStore(client: server.client)
        await store.load()
        await store.cancel()
        #expect(store.outcome["scheduler"] == .failed("Scheduler restart cancelled; waiting for wisp."))
        #expect(server.requests.last?.path == "/app/v1/services/scheduler/cancel")
        #expect(server.requests.filter { $0.method == "POST" }.count == 1)
    }

    @Test func aRestartIsNotReportedUntilThePidChanges() async throws {
        let steps = Steps()
        let clock = RestartClock()
        let server = StubServer { request in
            if request.method == "POST" { return .json(200, serviceExample()) }
            return .json(200, serviceExample(pid: steps.next() < 3 ? 123 : 456))
        }
        let store = ServicesStore(client: server.client, sleep: { clock.sleep($0) }, now: { clock.now() })
        await store.load()
        await store.restart("api")
        #expect(store.outcome["api"] == .restarted)
        #expect(store.services.first?.pid == 456)
        #expect(clock.sleeps == [.seconds(2), .seconds(2)])
        #expect(server.requests.filter { $0.method == "POST" }.count == 1)
        #expect(store.restarting.isEmpty)
    }

    @Test func aNewPidMustAlsoBeRunningToConfirmARestart() async {
        let steps = Steps()
        let clock = RestartClock()
        let server = StubServer { request in
            if request.method == "POST" { return .json(200, serviceExample()) }
            let step = steps.next()
            return .json(200, serviceExample(pid: step == 1 ? 123 : 456, running: step != 2))
        }
        let store = ServicesStore(client: server.client, sleep: { clock.sleep($0) }, now: { clock.now() })
        await store.load()
        await store.restart("api")
        #expect(store.outcome["api"] == .restarted)
        #expect(clock.sleeps == [.seconds(2), .seconds(2)])
    }

    @Test func failedPollsDoNotFailARestartBeforeTheDeadline() async throws {
        let steps = Steps()
        let clock = RestartClock()
        let server = StubServer { request in
            if request.method == "POST" { return .json(200, serviceExample()) }
            switch steps.next() {
            case 1: return .json(200, serviceExample())
            case 2: throw StubServer.NetworkDown()
            case 3: return .json(503, #"{"error":{"code":"unavailable","message":"starting"}}"#)
            default: return .json(200, serviceExample(pid: 456))
            }
        }
        let store = ServicesStore(client: server.client, sleep: { clock.sleep($0) }, now: { clock.now() })
        await store.load()
        await store.restart("api")
        #expect(store.outcome["api"] == .restarted)
        #expect(clock.sleeps.count == 3)
    }

    @Test func sixtySecondsWithoutANewPidIsUnconfirmed() async {
        let clock = RestartClock()
        let server = StubServer { _ in .json(200, serviceExample()) }
        let store = ServicesStore(client: server.client, sleep: { clock.sleep($0) }, now: { clock.now() })
        await store.load()
        await store.restart("api")
        #expect(store.outcome["api"] == .unconfirmed)
        #expect(clock.now() == Date(timeIntervalSince1970: 60))
        #expect(server.requests.last?.timeout == 2)
        #expect(store.restarting.isEmpty)
    }

    @Test func busyOrRestartingFailsWithTheServersWordsWithoutRetrying() async {
        for code in ["busy", "restarting"] {
            let server = StubServer { request in
                request.method == "GET" ? .json(200, serviceExample())
                    : .json(409, #"{"error":{"code":"\#(code)","message":"wisp is replying"}}"#)
            }
            let store = ServicesStore(client: server.client, sleep: { _ in Issue.record("must not poll") })
            await store.load()
            await store.restart("api")
            #expect(store.outcome["api"] == .failed("wisp is replying"))
            #expect(server.requests.count == 2)
            #expect(store.restarting.isEmpty)
        }
    }

    @Test func schedulerDrainRestartsOnlyOnANewPid() async {
        let steps = Steps()
        let clock = RestartClock()
        let server = StubServer { request in
            if request.method == "POST" { return .json(200, serviceExample(drain: pendingDrain)) }
            let step = steps.next()
            return .json(200, serviceExample(pid: step < 3 ? 123 : 456,
                drain: step == 2 ? pendingDrain : "null"))
        }
        let store = ServicesStore(client: server.client, sleep: { clock.sleep($0) })
        await store.load()
        await store.restart("scheduler")
        #expect(store.outcome["scheduler"] == .restarted)
        #expect(clock.sleeps == [.seconds(5), .seconds(5)])
    }

    @Test func drainedOldPidKeepsPollingUntilReplacementAppears() async {
        let steps = Steps()
        let clock = RestartClock()
        let drained = #"{"state":"drained","waiting":[],"deadline":null}"#
        let server = StubServer { request in
            if request.method == "POST" { return .json(200, serviceExample(drain: pendingDrain)) }
            let step = steps.next()
            return .json(200, serviceExample(pid: step < 4 ? 123 : 456,
                drain: step == 1 ? "null" : drained))
        }
        let store = ServicesStore(client: server.client, sleep: { clock.sleep($0) }, now: { clock.now() })
        await store.load()
        await store.restart("scheduler")
        #expect(store.outcome["scheduler"] == .restarted)
        #expect(clock.sleeps == [.seconds(5), .seconds(5), .seconds(5)])
    }

    @Test func drainedTimeoutKeepsTrackingForReopeningAfterReplacement() async {
        let clock = RestartClock()
        let drained = #"{"state":"drained","waiting":[],"deadline":null}"#
        let server = StubServer { request in
            .json(200, serviceExample(drain: request.method == "POST" ? drained : "null"))
        }
        let store = ServicesStore(client: server.client, sleep: { clock.sleep($0) }, now: { clock.now() })
        await store.load()
        server.setAnswer { _ in .json(200, serviceExample(drain: drained)) }
        await store.restart("scheduler")
        #expect(store.outcome["scheduler"] == .unconfirmed)
        #expect(clock.now() == Date(timeIntervalSince1970: 90))
        #expect(store.restarting.isEmpty)
        store.stop()
        server.setAnswer { _ in .json(200, serviceExample(pid: 456, drain: drained)) }
        await store.load()
        #expect(store.outcome["scheduler"] == .restarted)
    }

    @Test func drainedDeadlineStillAppliesWhenLaterPollsFail() async {
        let clock = RestartClock()
        let drained = #"{"state":"drained","waiting":[],"deadline":null}"#
        let server = StubServer { request in
            .json(200, serviceExample(drain: request.method == "POST" ? drained : "null"))
        }
        let store = ServicesStore(client: server.client, sleep: { clock.sleep($0) }, now: { clock.now() })
        await store.load()
        server.setAnswer { request in
            if request.method == "POST" { return .json(200, serviceExample(drain: drained)) }
            throw StubServer.NetworkDown()
        }
        await store.restart("scheduler")
        #expect(store.outcome["scheduler"] == .unconfirmed)
        #expect(clock.now() == Date(timeIntervalSince1970: 90))
        #expect(store.restarting.isEmpty)
    }

    @Test func untrackedOldDrainedRecordIsIgnored() async {
        let server = StubServer { _ in .json(200, serviceExample(drain:
            #"{"state":"drained","waiting":[],"deadline":null}"#)) }
        let store = ServicesStore(client: server.client, sleep: { _ in Issue.record("must not poll") })
        await store.load()
        #expect(store.outcome["scheduler"] == nil)
        #expect(store.restarting.isEmpty)
    }

    @Test func refusedSaveReturnsFalseSoTheTextDraftCanBeKept() async throws {
        let folder = try temporaryFolder("settings-save-result")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { request in
            request.method == "GET" ? .json(200, settingsExample)
                : .json(409, #"{"error":{"code":"refused","message":"Keep this edit"}}"#)
        }
        let store = try makeOwner(server, folder: folder).settings("wisp")
        await store.load()
        let saved = await store.save(.operatorName, value: .string("Mine"))
        #expect(!saved)
        #expect(store.saveError == "Keep this edit")
        #expect(store.settings?.operatorName.value == "the operator")
    }

    @Test func overlappingSaveReturnsFalseSoTheSecondDraftIsNotDropped() async throws {
        let folder = try temporaryFolder("settings-overlapping-save")
        defer { try? FileManager.default.removeItem(at: folder) }
        let entered = AsyncStream<Void>.makeStream()
        defer { entered.continuation.finish() }
        let release = DispatchSemaphore(value: 0)
        let server = StubServer { request in
            if request.method == "GET" { return .json(200, settingsExample) }
            entered.continuation.yield(())
            _ = release.wait(timeout: .now() + 3)
            return .json(200, #"{"settings":\#(settingsExample),"applies":"now"}"#)
        }
        let store = try makeOwner(server, folder: folder).settings("wisp")
        await store.load()
        let first = Task { await store.save(.operatorName, value: .string("First")) }
        var iterator = entered.stream.makeAsyncIterator()
        _ = await iterator.next()
        let second = await store.save(.operatorName, value: .string("Second"))
        #expect(!second)
        release.signal()
        #expect(await first.value)
        #expect(server.requests.filter { $0.method == "POST" }.count == 1)
    }

    @Test func anAbandonedDrainFailsAndNamesWhatItWasWaitingFor() async {
        let steps = Steps()
        let server = StubServer { request in
            if request.method == "POST" { return .json(200, serviceExample(drain: pendingDrain)) }
            return .json(200, serviceExample(drain: steps.next() == 1 ? "null" :
                #"{"state":"abandoned","waiting":["wisp is replying"],"deadline":null}"#))
        }
        let store = ServicesStore(client: server.client, sleep: { _ in })
        await store.load()
        await store.restart("scheduler")
        #expect(store.outcome["scheduler"] == .failed("Scheduler restart abandoned; waiting for wisp is replying."))
        #expect(store.restarting.isEmpty)
    }
}
