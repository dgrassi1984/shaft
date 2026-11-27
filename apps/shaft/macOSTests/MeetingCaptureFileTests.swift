import AudioToolbox
import XCTest

/// The meeting file the Mac writes. Bug 2026-11-27: AirPods in a call run at 24 kHz, where two AAC channels top out
/// at 128 kbit/s; the capture asked for 192 kbit/s, the encoder refused it ('!dat') and then every write, and a
/// 17-minute meeting became a 28-byte file with no moov atom. Each rate a microphone can run at must give a file that
/// holds the audio written to it.
final class MeetingCaptureFileTests: XCTestCase {
    private func write(seconds: Double, sampleRate: Double, online: Bool) throws -> Double? {
        let url = FileManager.default.temporaryDirectory.appending(path: "meeting-\(UUID().uuidString).m4a")
        defer { try? FileManager.default.removeItem(at: url) }
        let capture = MeetingCapture(online: online, url: url, stallSeconds: 20)
        capture.sampleRate = sampleRate
        let ch = capture.channels
        var fmt = AudioStreamBasicDescription(mSampleRate: sampleRate, mFormatID: kAudioFormatLinearPCM,
                                              mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
                                              mBytesPerPacket: 4 * ch, mFramesPerPacket: 1, mBytesPerFrame: 4 * ch,
                                              mChannelsPerFrame: ch, mBitsPerChannel: 32, mReserved: 0)
        let file = try capture.openFile(fmt: &fmt, bitRate: true)
        let frames = 1024
        let buffer = UnsafeMutablePointer<Float>.allocate(capacity: frames * Int(ch))
        defer { buffer.deallocate() }
        for block in 0..<Int(seconds * sampleRate) / frames {
            for i in 0..<frames * Int(ch) { buffer[i] = 0.3 * sin(Float(block * frames + i) * 0.05) }
            var list = AudioBufferList(mNumberBuffers: 1, mBuffers: AudioBuffer(mNumberChannels: ch,
                                       mDataByteSize: UInt32(frames * Int(ch) * 4), mData: buffer))
            XCTAssertEqual(ExtAudioFileWrite(file, UInt32(frames), &list), noErr, "write at \(Int(sampleRate)) Hz")
        }
        XCTAssertEqual(ExtAudioFileDispose(file), noErr, "finalise at \(Int(sampleRate)) Hz")
        return MeetingCapture.secondsInFile(url)
    }

    func testEveryMicrophoneRateGivesAFileThatHoldsTheAudio() throws {
        for rate in [16_000.0, 24_000, 44_100, 48_000] {
            for online in [true, false] {
                let held = try XCTUnwrap(try write(seconds: 2, sampleRate: rate, online: online),
                                         "no audio in the file at \(Int(rate)) Hz, online \(online)")
                XCTAssertEqual(held, 2, accuracy: 0.1, "at \(Int(rate)) Hz, online \(online)")
            }
        }
    }

    func testBitRateIsClampedToWhatTheEncoderOffers() {
        let at24k = [AudioValueRange(mMinimum: 16_000, mMaximum: 16_000), AudioValueRange(mMinimum: 64_000, mMaximum: 64_000),
                     AudioValueRange(mMinimum: 128_000, mMaximum: 128_000)]
        XCTAssertEqual(MeetingCapture.encodeBitRate(wanted: 192_000, offered: at24k), 128_000)
        XCTAssertEqual(MeetingCapture.encodeBitRate(wanted: 96_000, offered: at24k), 64_000)
        XCTAssertEqual(MeetingCapture.encodeBitRate(wanted: 8_000, offered: at24k), 16_000)
        XCTAssertNil(MeetingCapture.encodeBitRate(wanted: 96_000, offered: []))
        let continuous = [AudioValueRange(mMinimum: 64_000, mMaximum: 128_000)]
        XCTAssertEqual(MeetingCapture.encodeBitRate(wanted: 96_000, offered: continuous), 96_000)
        XCTAssertEqual(MeetingCapture.encodeBitRate(wanted: 192_000, offered: continuous), 128_000)
    }

    func testAFileWithNoAudioReadsAsNone() throws {
        let url = FileManager.default.temporaryDirectory.appending(path: "empty-\(UUID().uuidString).m4a")
        defer { try? FileManager.default.removeItem(at: url) }
        try (Data([0, 0, 0, 0x1c]) + Data("ftypM4A ".utf8) + Data(count: 16)).write(to: url)
        XCTAssertNil(MeetingCapture.secondsInFile(url))
    }
}
