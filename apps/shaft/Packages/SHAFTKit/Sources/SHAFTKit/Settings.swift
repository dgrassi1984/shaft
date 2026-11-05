import Foundation
import Observation

@Observable
@MainActor
public final class InstanceSettingsStore {
    public let instance: String
    public private(set) var settings: InstanceSettings?
    public private(set) var loadError: APIError?
    public private(set) var saving: SettingField?
    public private(set) var notice: String?
    public private(set) var saveError: String?
    public private(set) var unsupported = false

    @ObservationIgnored private let client: APIClient
    @ObservationIgnored private weak var owner: InstancesStore?
    @ObservationIgnored private var pausing = false

    init(instance: String, owner: InstancesStore) {
        self.instance = instance
        self.owner = owner
        self.client = owner.client
    }

    public func load() async {
        do {
            settings = try await client.settings(instance)
            loadError = nil
            unsupported = false
        } catch is CancellationError {
            return
        } catch {
            let error = APIError.from(error)
            loadError = error
            unsupported = error.status == 404
            if error.isUnpaired { owner?.report(error) }
        }
    }

    public func save(_ field: SettingField, value: SettingValue) async {
        guard saving == nil, !pausing, let settings else { return }
        saving = field
        saveError = nil
        notice = nil
        defer { saving = nil }
        do {
            let answer = try await client.saveSetting(instance, field: field, value: value,
                                                       expect: settings.value(for: field))
            self.settings = answer.settings
            unsupported = false
            loadError = nil
            notice = answer.applies == "next run" ? "Applies from the next run" : "Applies now"
        } catch is CancellationError {
            return
        } catch {
            let error = APIError.from(error)
            if case .server(_, let body) = error, body.code == "stale" {
                if let fresh = body.settings { self.settings = fresh }
                saveError = "This setting changed elsewhere. Review the current value before saving again."
            } else {
                saveError = error.message
            }
            unsupported = error.status == 404
            if error.isUnpaired { owner?.report(error) }
        }
    }

    public func setPaused(_ paused: Bool) async {
        guard !pausing, saving == nil else { return }
        pausing = true
        saveError = nil
        notice = nil
        defer { pausing = false }
        do {
            settings = try await client.setPaused(instance, paused: paused)
            unsupported = false
            loadError = nil
            notice = "Applies now"
        } catch is CancellationError {
            return
        } catch {
            let error = APIError.from(error)
            saveError = error.message
            unsupported = error.status == 404
            if error.isUnpaired { owner?.report(error) }
        }
    }
}

@Observable
@MainActor
public final class ServicesStore {
    public private(set) var services: [ServiceStatus] = []
    public private(set) var loadError: APIError?
    public private(set) var unsupported = false
    public private(set) var restarting: Set<String> = []
    public private(set) var outcome: [String: RestartOutcome] = [:]

    @ObservationIgnored private let client: APIClient
    @ObservationIgnored private weak var owner: InstancesStore?
    @ObservationIgnored private let sleep: @Sendable (Duration) async throws -> Void
    @ObservationIgnored private let now: @Sendable () -> Date
    @ObservationIgnored private var watchers: [String: Task<Void, Never>] = [:]
    @ObservationIgnored private var generation = 0
    @ObservationIgnored private var schedulerPID: Int?
    @ObservationIgnored private var schedulerTracked = false

    public init(client: APIClient,
                sleep: @escaping @Sendable (Duration) async throws -> Void = { try await Task.sleep(for: $0) },
                now: @escaping @Sendable () -> Date = { Date() }) {
        self.client = client
        self.sleep = sleep
        self.now = now
    }

    func attach(to owner: InstancesStore) { self.owner = owner }

    public func load() async {
        let generation = self.generation
        do {
            let answer = try await client.services()
            guard generation == self.generation, !Task.isCancelled else { return }
            apply(answer)
            if let scheduler = service("scheduler"), scheduler.drain?.pending == true,
               !restarting.contains("scheduler") {
                if !schedulerTracked { schedulerPID = scheduler.pid; schedulerTracked = true }
                outcome["scheduler"] = .waiting
                startWatcher("scheduler", oldPID: schedulerPID)
            } else if schedulerTracked, !restarting.contains("scheduler"), let scheduler = service("scheduler") {
                _ = finished(scheduler, oldPID: schedulerPID)
            }
        } catch is CancellationError {
            return
        } catch {
            guard generation == self.generation else { return }
            report(APIError.from(error))
        }
    }

    /// Returns when the restart is confirmed, fails, or monitoring stops. Scheduler drains have no local deadline.
    public func restart(_ name: String) async {
        guard !restarting.contains(name) else { return }
        restarting.insert(name)
        outcome[name] = .waiting
        let generation = self.generation
        defer {
            if generation == self.generation { restarting.remove(name) }
        }
        do {
            // Without a baseline, a successful poll cannot prove that a process restarted.
            if service(name) == nil {
                let baseline = try await client.services()
                guard generation == self.generation, !Task.isCancelled else { return }
                apply(baseline)
            }
            guard generation == self.generation, !Task.isCancelled else { return }
            let oldPID = service(name)?.pid
            let answer = try await client.restart(service: name)
            guard generation == self.generation, !Task.isCancelled else { return }
            apply(answer)
            if name == "scheduler" { schedulerPID = oldPID; schedulerTracked = true }
            if let status = service(name), finished(status, oldPID: oldPID) {
                restarting.remove(name)
                return
            }
            let task = startWatcher(name, oldPID: oldPID)
            await task.value
        } catch is CancellationError {
            if generation == self.generation { restarting.remove(name) }
        } catch {
            guard generation == self.generation else { return }
            let error = APIError.from(error)
            outcome[name] = .failed(error.message)
            restarting.remove(name)
            if error.status == 404 { unsupported = true }
            if error.isUnpaired { owner?.report(error) }
        }
    }

    public func cancel() async {
        let oldPID = schedulerTracked ? schedulerPID : service("scheduler")?.pid
        do {
            let answer = try await client.cancelSchedulerRestart()
            watchers.removeValue(forKey: "scheduler")?.cancel()
            restarting.remove("scheduler")
            apply(answer)
            if let status = service("scheduler") {
                _ = finished(status, oldPID: oldPID)
            }
        } catch is CancellationError {
            return
        } catch {
            let error = APIError.from(error)
            outcome["scheduler"] = .failed(error.message)
            if error.status == 404 { unsupported = true }
            if error.isUnpaired { owner?.report(error) }
        }
    }

    public func stop() {
        generation += 1
        for task in watchers.values { task.cancel() }
        watchers.removeAll()
        restarting.removeAll()
    }

    private func service(_ name: String) -> ServiceStatus? { services.first { $0.name == name } }

    private func apply(_ answer: ServicesResponse) {
        services = answer.services
        loadError = nil
        unsupported = false
    }

    private func report(_ error: APIError) {
        loadError = error
        unsupported = error.status == 404
        if error.isUnpaired { owner?.report(error) }
    }

    /// Only a running process with a new, non-null PID confirms a restart.
    private func finished(_ status: ServiceStatus, oldPID: Int?) -> Bool {
        if status.running, let pid = status.pid, pid != oldPID {
            outcome[status.name] = .restarted
            if status.name == "scheduler" { schedulerTracked = false }
            return true
        }
        if let drain = status.drain, ["abandoned", "cancelled"].contains(drain.state) {
            let waiting = drain.waiting.isEmpty ? "" : "; waiting for " + drain.waiting.joined(separator: ", ")
            outcome[status.name] = .failed("Scheduler restart \(drain.state)\(waiting).")
            schedulerTracked = false
            return true
        }
        if status.name == "scheduler", let drain = status.drain, !drain.pending {
            outcome[status.name] = .unconfirmed
            schedulerTracked = false
            return true
        }
        return false
    }

    @discardableResult
    private func startWatcher(_ name: String, oldPID: Int?) -> Task<Void, Never> {
        restarting.insert(name)
        let generation = self.generation
        let deadline = now().addingTimeInterval(60)
        let task = Task { [weak self, sleep, now] in
            while !Task.isCancelled {
                let delay = name == "api" ? max(0, min(2, deadline.timeIntervalSince(now()))) : 5
                do { try await sleep(.seconds(delay)) } catch { break }
                guard let self, !Task.isCancelled, generation == self.generation else { return }
                if name == "api", now() >= deadline {
                    self.outcome[name] = .unconfirmed
                    break
                }
                do {
                    let answer: ServicesResponse
                    if name == "api" {
                        answer = try await self.client.services(timeout: max(0.001, min(15, deadline.timeIntervalSince(now()))))
                    } else {
                        answer = try await self.client.services()
                    }
                    guard !Task.isCancelled, generation == self.generation else { return }
                    if name == "api", now() >= deadline {
                        self.outcome[name] = .unconfirmed
                        break
                    }
                    self.apply(answer)
                    if let status = self.service(name), self.finished(status, oldPID: oldPID) { break }
                } catch is CancellationError {
                    break
                } catch {
                    // A temporarily unreachable service does not disprove a restart.
                    let error = APIError.from(error)
                    if error.isUnpaired { self.report(error); break }
                }
                if name == "api", now() >= deadline {
                    self.outcome[name] = .unconfirmed
                    break
                }
            }
            guard let self, generation == self.generation, !Task.isCancelled else { return }
            self.restarting.remove(name)
            self.watchers.removeValue(forKey: name)
        }
        watchers[name] = task
        return task
    }
}
