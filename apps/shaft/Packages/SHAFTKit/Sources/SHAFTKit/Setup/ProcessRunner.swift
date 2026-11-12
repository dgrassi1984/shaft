#if os(macOS)
import Foundation

/// What a finished command left: its exit status and everything it wrote.
public struct ProcessResult: Sendable, Equatable {
    public var status: Int32
    public var stdout: Data
    public var stderr: Data

    public init(status: Int32, stdout: Data, stderr: Data) {
        self.status = status
        self.stdout = stdout
        self.stderr = stderr
    }

    public var succeeded: Bool { status == 0 }

    /// What to show when it failed: its stderr, else its stdout, trimmed.
    public var said: String {
        let err = String(decoding: stderr, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        return err.isEmpty ? String(decoding: stdout, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines) : err
    }
}

/// Runs a command for the setup engine. `env` is the child's whole environment. `stdin` is written to the child and
/// then closed (secrets travel this way, never in `args` or `env`); nil gives it an empty stdin. `onLine` receives each
/// stdout line as it arrives, for progress that streams (`shaft install --json`). Cancelling the calling task stops the
/// child and throws `CancellationError`.
public protocol ProcessRunning: Sendable {
    func run(_ exe: URL, _ args: [String], env: [String: String], stdin: Data?,
             onLine: (@Sendable (String) -> Void)?) async throws -> ProcessResult
}

public extension ProcessRunning {
    func run(_ exe: URL, _ args: [String], env: [String: String], stdin: Data?) async throws -> ProcessResult {
        try await run(exe, args, env: env, stdin: stdin, onLine: nil)
    }
}

/// A command that could not start: the program is missing or not executable.
public struct LaunchError: Error, Sendable, LocalizedError {
    public let exe: String
    public let reason: String
    public var errorDescription: String? { "Could not start \(exe): \(reason)" }
}

/// The real runner: `Process`, both streams read to the end on their own threads (a full pipe never stalls the child).
public struct ProcessRunner: ProcessRunning {
    public init() {}

    public func run(_ exe: URL, _ args: [String], env: [String: String], stdin: Data?,
                    onLine: (@Sendable (String) -> Void)?) async throws -> ProcessResult {
        try Task.checkCancellation()
        let child = Child()
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<ProcessResult, any Error>) in
                child.start(exe, args, env: env, stdin: stdin, onLine: onLine, continuation: continuation)
            }
        } onCancel: {
            child.cancel()
        }
    }
}

/// One child process and the state its threads share, behind a lock. The continuation is resumed exactly once.
private final class Child: @unchecked Sendable {
    private let lock = NSLock()
    private let process = Process()
    private var launched = false
    private var cancelled = false
    private var continuation: CheckedContinuation<ProcessResult, any Error>?
    private var out = Data()
    private var err = Data()

    func start(_ exe: URL, _ args: [String], env: [String: String], stdin: Data?,
               onLine: (@Sendable (String) -> Void)?, continuation: CheckedContinuation<ProcessResult, any Error>) {
        process.executableURL = exe
        process.arguments = args
        process.environment = env
        let outPipe = Pipe(), errPipe = Pipe()
        process.standardOutput = outPipe
        process.standardError = errPipe
        let inPipe = stdin.map { _ in Pipe() }
        process.standardInput = inPipe ?? FileHandle.nullDevice

        let group = DispatchGroup()
        group.enter()
        process.terminationHandler = { [self] _ in
            // Cancelled: answer now, without waiting for grandchildren that may still hold the pipes open.
            if lock.withLock({ cancelled }) { finish(.failure(CancellationError())) }
            group.leave()
        }
        lock.withLock { self.continuation = continuation }
        let stop: Bool = lock.withLock { cancelled }
        if stop {
            finish(.failure(CancellationError()))
            return
        }
        do {
            try process.run()
        } catch {
            finish(.failure(LaunchError(exe: exe.path, reason: error.localizedDescription)))
            return
        }
        let cancelNow: Bool = lock.withLock {
            launched = true
            return cancelled
        }
        if cancelNow { process.terminate() }

        group.enter()
        DispatchQueue.global().async { [self] in
            let data = Self.drain(outPipe.fileHandleForReading, onLine: onLine)
            lock.withLock { out = data }
            group.leave()
        }
        group.enter()
        DispatchQueue.global().async { [self] in
            let data = Self.drain(errPipe.fileHandleForReading, onLine: nil)
            lock.withLock { err = data }
            group.leave()
        }
        if let inPipe, let stdin {
            DispatchQueue.global().async {
                let handle = inPipe.fileHandleForWriting
                try? handle.write(contentsOf: stdin)   // a child that exits without reading closes the pipe: not an error here
                try? handle.close()
            }
        }
        group.notify(queue: .global()) { [self] in
            let (cancelled, result) = lock.withLock {
                (self.cancelled, ProcessResult(status: process.terminationStatus, stdout: out, stderr: err))
            }
            finish(cancelled ? .failure(CancellationError()) : .success(result))
        }
    }

    func cancel() {
        let running: Bool = lock.withLock {
            cancelled = true
            return launched
        }
        if running, process.isRunning { process.terminate() }
    }

    private func finish(_ result: Result<ProcessResult, any Error>) {
        let pending: CheckedContinuation<ProcessResult, any Error>? = lock.withLock {
            defer { continuation = nil }
            return continuation
        }
        pending?.resume(with: result)
    }

    /// Reads a stream to its end; with `onLine`, hands over each complete line (and a last unterminated one).
    private static func drain(_ handle: FileHandle, onLine: (@Sendable (String) -> Void)?) -> Data {
        var all = Data()
        var partial = Data()
        while let chunk = try? handle.read(upToCount: 64 * 1024), !chunk.isEmpty {
            all.append(chunk)
            guard let onLine else { continue }
            partial.append(chunk)
            while let newline = partial.firstIndex(of: 0x0A) {
                onLine(String(decoding: partial[partial.startIndex..<newline], as: UTF8.self))
                partial.removeSubrange(partial.startIndex...newline)
            }
        }
        if let onLine, !partial.isEmpty { onLine(String(decoding: partial, as: UTF8.self)) }
        return all
    }
}
#endif
