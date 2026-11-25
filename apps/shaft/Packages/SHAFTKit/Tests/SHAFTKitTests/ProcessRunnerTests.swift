#if os(macOS)
import Foundation
import Synchronization
import Testing
@testable import SHAFTKit

struct ProcessRunnerTests {
    @Test func streamsStdoutBeforeThePipeCloses() async throws {
        let arrivals = Mutex<[(line: String, elapsed: Duration)]>([])
        let started = ContinuousClock.now
        let result = try await ProcessRunner().run(
            URL(fileURLWithPath: "/bin/sh"), ["-c", "echo first; sleep 3; echo second"],
            env: ["PATH": "/bin"], stdin: nil,
            onLine: { line in
                let elapsed = ContinuousClock.now - started
                arrivals.withLock { $0.append((line, elapsed)) }
            }
        )

        let received = arrivals.withLock { $0 }
        #expect(received.map(\.line) == ["first", "second"])
        let first = try #require(received.first)
        #expect(first.elapsed < .milliseconds(1500))
        #expect(result.stdout == Data("first\nsecond\n".utf8))
    }

    @Test func deliversFinalLineWithoutNewline() async throws {
        let lines = Mutex<[String]>([])
        let result = try await ProcessRunner().run(
            URL(fileURLWithPath: "/bin/sh"), ["-c", "printf 'first\nlast'"],
            env: [:], stdin: nil,
            onLine: { line in lines.withLock { $0.append(line) } }
        )

        #expect(lines.withLock { $0 } == ["first", "last"])
        #expect(result.stdout == Data("first\nlast".utf8))
    }
}
#endif
