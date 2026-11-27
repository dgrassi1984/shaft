import AudioToolbox
import SHAFTKit
import XCTest

/// The meeting file the Mac writes. Bug 2026-11-27: AirPods in a call run at 24 kHz, where the AAC encoder refused the
/// bit rate asked for, then every write, and a 17-minute meeting became a 28-byte file. The file is now IMA4 in a CAF,
/// which has no bit rate to refuse and, cut short by a quit or a crash, still holds the audio written before it.
final class MeetingCaptureFileTests: XCTestCase {
    private func url(_ name: String) -> URL {
        FileManager.default.temporaryDirectory.appending(path: "\(name)-\(UUID().uuidString).caf")
    }

    /// Writes `seconds` of a tone and returns the open file (the caller disposes it, or does not: a crash).
    private func write(to url: URL, seconds: Double, sampleRate: Double, online: Bool) throws -> ExtAudioFileRef {
        let capture = MeetingCapture(online: online, url: url, stallSeconds: 20)
        capture.sampleRate = sampleRate
        capture.fileRate = sampleRate
        let ch = capture.channels
        var fmt = capture.clientFormat(sampleRate)
        let file = try capture.openFile(fmt: &fmt)
        let frames = 1024
        let buffer = UnsafeMutablePointer<Float>.allocate(capacity: frames * Int(ch))
        defer { buffer.deallocate() }
        for block in 0..<Int(seconds * sampleRate) / frames {
            for i in 0..<frames * Int(ch) { buffer[i] = 0.3 * sin(Float(block * frames + i) * 0.05) }
            var list = AudioBufferList(mNumberBuffers: 1, mBuffers: AudioBuffer(mNumberChannels: ch,
                                       mDataByteSize: UInt32(frames * Int(ch) * 4), mData: buffer))
            XCTAssertEqual(ExtAudioFileWrite(file, UInt32(frames), &list), noErr, "write at \(Int(sampleRate)) Hz")
        }
        return file
    }

    func testEveryMicrophoneRateGivesAFileThatHoldsTheAudio() throws {
        for rate in [16_000.0, 24_000, 44_100, 48_000] {
            for online in [true, false] {
                let path = url("meeting")
                defer { try? FileManager.default.removeItem(at: path) }
                XCTAssertEqual(ExtAudioFileDispose(try write(to: path, seconds: 2, sampleRate: rate, online: online)), noErr)
                let held = try XCTUnwrap(MeetingCapture.secondsInFile(path), "no audio at \(Int(rate)) Hz, online \(online)")
                XCTAssertEqual(held, 2, accuracy: 0.1, "at \(Int(rate)) Hz, online \(online)")
            }
        }
    }

    /// Review 2026-11-27: a quit or crash while recording left an .m4a without its index. A CAF copied before its
    /// writer was closed (what a crash leaves) opens after trimming, with the audio written so far.
    func testAFileCutShortStillHoldsItsAudio() throws {
        let path = url("open"), copy = url("crashed")
        defer { for p in [path, copy] { try? FileManager.default.removeItem(at: p) } }
        let file = try write(to: path, seconds: 3, sampleRate: 48_000, online: true)
        try FileManager.default.copyItem(at: path, to: copy)
        ExtAudioFileDispose(file)
        try MeetingFiles.trimPartialPacket(of: copy)
        let held = try XCTUnwrap(MeetingCapture.secondsInFile(copy), "a cut-short CAF opens no audio")
        XCTAssertGreaterThan(held, 2)
        XCTAssertTrue(MeetingCapture.recover(copy))
        let side = copy.deletingPathExtension().appendingPathExtension("json")
        defer { try? FileManager.default.removeItem(at: side) }
        let fields = try XCTUnwrap(try JSONSerialization.jsonObject(with: Data(contentsOf: side)) as? [String: Any])
        XCTAssertNotNil(fields["ended"])
        XCTAssertTrue(((fields["problems"] as? [String]) ?? []).contains { $0.contains("recovered") })
    }

    /// The 28-byte file of 2026-11-27 holds nothing: it is kept, never sent.
    func testAFileWithNoAudioIsNotSent() throws {
        let path = FileManager.default.temporaryDirectory.appending(path: "empty-\(UUID().uuidString).m4a")
        defer { try? FileManager.default.removeItem(at: path) }
        try (Data([0, 0, 0, 0x1c]) + Data("ftypM4A ".utf8) + Data(count: 16)).write(to: path)
        XCTAssertNil(MeetingCapture.secondsInFile(path))
        XCTAssertFalse(MeetingCapture.recover(path))
    }

    /// AirPods switching to call mode mid-meeting: the device runs at 24 kHz, the file stays at 48 kHz. The IO thread
    /// resamples (the writer's client format never changes mid-file), so a second of audio is a second in the file.
    func testADeviceRateChangeIsResampledToTheFilesRate() {
        let capture = MeetingCapture(online: false, url: url("rate"), stallSeconds: 20)
        capture.fileRate = 48_000
        capture.sampleRate = 24_000
        var total = 0
        for _ in 0..<(24_000 / 512) {
            for i in 0..<512 { capture.scratch[i] = Float(i) / 512 }
            total += capture.resample(512)
        }
        XCTAssertEqual(Double(total), 2 * Double(24_000 / 512 * 512), accuracy: 4)
    }

    /// Codex review 2026-11-27: hours asleep were filled with silence in one IO callback (a stall), or capped and the
    /// rest lost unsaid. A long gap is marked with its clock length, not filled.
    func testALongGapIsMarkedNotFilled() throws {
        let path = url("gap")
        defer { try? FileManager.default.removeItem(at: path) }
        let capture = MeetingCapture(online: true, url: path, stallSeconds: 20)
        var fmt = capture.clientFormat(48_000)
        capture.file = try capture.openFile(fmt: &fmt)
        XCTAssertEqual(ExtAudioFileWriteAsync(capture.file!, 0, nil), noErr)
        capture.fillGap(seconds: 3600, reason: "no audio (the Mac asleep, or a stall)")
        capture.fillGap(seconds: 59, reason: "rebuild")
        _ = capture.control.sync {}
        XCTAssertEqual(capture.gaps.first?.wallSeconds, 3600)
        XCTAssertEqual(capture.gaps.first?.end, 0)
        XCTAssertEqual(capture.gaps.last?.end ?? 0, 59, accuracy: 0.01)
        XCTAssertEqual(capture.framesLost.load(ordering: .relaxed), 0, "write error \(capture.lastWriteError.load(ordering: .relaxed))")
        XCTAssertEqual(capture.framesWritten.load(ordering: .relaxed), 59 * 48_000)
        ExtAudioFileDispose(capture.file!)
    }
}
