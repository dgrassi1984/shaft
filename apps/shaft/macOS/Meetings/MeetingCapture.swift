import AudioToolbox
import CoreAudio
import Foundation
import SHAFTKit
import os
import Synchronization

private let logger = Logger(subsystem: "io.github.dgrassi1984.shaft", category: "meetings")
fileprivate func log(_ s: String) { logger.info("\(s, privacy: .public)") }

// MARK: - Core Audio helpers (fileprivate)

fileprivate func check(_ status: OSStatus, _ what: String) throws {
    if status != noErr { throw NSError(domain: "MeetingCapture", code: Int(status), userInfo: [NSLocalizedDescriptionKey: "\(what) failed: OSStatus \(status)"]) }
}

fileprivate func address(_ selector: AudioObjectPropertySelector, _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

fileprivate func getValue<T>(_ obj: AudioObjectID, _ selector: AudioObjectPropertySelector, _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal,
                 initial: T, qualifier: UnsafeRawPointer? = nil, qualifierSize: UInt32 = 0) throws -> T {
    var addr = address(selector, scope)
    var value = initial
    var size = UInt32(MemoryLayout<T>.size)
    try check(AudioObjectGetPropertyData(obj, &addr, qualifierSize, qualifier, &size, &value), "get property \(selector)")
    return value
}

fileprivate func getString(_ obj: AudioObjectID, _ selector: AudioObjectPropertySelector) throws -> String {
    var addr = address(selector)
    var value: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    try check(AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &value), "get string \(selector)")
    return (value?.takeRetainedValue() as String?) ?? ""
}

/// Channels per input buffer of a device, in the order its IOProc receives them.
fileprivate func inputBufferChannels(_ device: AudioObjectID) throws -> [Int] {
    var addr = address(kAudioDevicePropertyStreamConfiguration, kAudioObjectPropertyScopeInput)
    var size: UInt32 = 0
    try check(AudioObjectGetPropertyDataSize(device, &addr, 0, nil, &size), "stream configuration size")
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
    defer { raw.deallocate() }
    try check(AudioObjectGetPropertyData(device, &addr, 0, nil, &size, raw), "stream configuration")
    let list = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
    return list.map { Int($0.mNumberChannels) }
}

/// This process's audio object, or 0 when Core Audio has none for it yet (then the tap excludes nothing — we play
/// no audio anyway).
fileprivate func ownProcessObject() -> AudioObjectID {
    var pid = getpid()
    return (try? getValue(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyTranslatePIDToProcessObject,
                          initial: AudioObjectID(0), qualifier: &pid, qualifierSize: UInt32(MemoryLayout<pid_t>.size))) ?? 0
}

fileprivate func hostSeconds(_ host: UInt64) -> Double { Double(AudioConvertHostTimeToNanos(host)) / 1e9 }

fileprivate func iso(_ d: Date) -> String { ISO8601DateFormatter().string(from: d) }


/// A stretch of the recording with no captured audio, filled with silence so the timeline stays aligned.
struct MeetingGap: Codable, Sendable, Equatable { var start: Double; var end: Double; var reason: String }

/// The Mac's meeting capture (docs/meetings.md): the microphone and, online, every other process's audio through a Core
/// Audio process tap, in one private aggregate device (the mic is the clock, the tap drift-compensated), written as one
/// IMA4 CAF file (it survives the app quitting or crashing mid-recording, as the iPhone's does) — channel 1 the mic,
/// channel 2 the system audio. A watchdog rebuilds tap + aggregate when callbacks stop or
/// the tap turns to digital silence while the mic hears speech, and fills each stretch without audio with silence,
/// recorded as a gap. Ported from the measured spike recorder; the IO thread and the control queue share state only
/// through atomics, hence @unchecked Sendable.
final class MeetingCapture: @unchecked Sendable {
    let online: Bool
    let url: URL
    let stallSeconds: Double
    /// The microphone's UID, or nil for the system default input (MeetingMicrophones resolves the operator's choice).
    let inputUID: String?
    let control = DispatchQueue(label: "shaft.meeting.control")
    let io = DispatchQueue(label: "shaft.meeting.io", qos: .userInteractive)
    private var timer: DispatchSourceTimer?
    /// Said once when the system channel stays exact silence: the permission is the likely cause.
    var onWarning: (@Sendable (String) -> Void)?

    var file: ExtAudioFileRef?
    /// The device's rate now: the client format's. It can change at a rebuild (AirPods switching to call mode).
    var sampleRate: Double = 48_000
    /// The file's rate, fixed at the start; ExtAudioFile converts a later device rate to it.
    var fileRate: Double = 48_000
    var channels: UInt32 { online ? 2 : 1 }
    var inputDevice: AudioDeviceID = 0
    var inputName = ""
    /// The UID of the input actually recorded from, translated again on every rebuild (a USB microphone that
    /// reconnects keeps its UID but gets a new device id).
    var activeUID: String?
    /// Set when the chosen microphone was not connected at start and the system default was used instead.
    var inputFallback: String?
    var tap: AudioObjectID = 0
    var aggregate: AudioObjectID = 0
    var ioDevice: AudioObjectID = 0
    var procID: AudioDeviceIOProcID?
    var bufferChannels: [Int] = []     // per input buffer of ioDevice
    var micBuffers = 0                 // the first micBuffers buffers are the mic; the rest are the tap
    var started = Date()
    var gaps: [MeetingGap] = []
    var rebuilds: [[String: String]] = []
    var stallThreshold: Double
    var noCallbackGrace: Double = 2              // doubles on each rebuild that brings no callbacks, back to 2 on audio
    var warnedSilentTap = false
    var warnedWriteFailed = false
    var warnedSilentMic = false
    var warnedRebuildFailed = false
    /// Said in the sidecar: what the recording could not keep (a gap too long to fill, a rebuild that kept failing).
    var notes: [String] = []

    // Written by the IO thread, read by the watchdog.
    let lastCallback = Atomic<UInt64>(0)        // mach host time of the last IOProc call, or of the last (re)start
    let lastAudio = Atomic<UInt64>(0)           // mach host time of the last IOProc call that wrote audio
    let tapEverHeard = Atomic<Bool>(false)      // any non-zero system-audio sample since the start
    let framesWritten = Atomic<Int>(0)
    /// Frames the writer refused (`ExtAudioFileWriteAsync` failed): reported in the sidecar and to the operator, never
    /// counted as recorded (review 2026-11-25: a refused write still counted, so a short file had a full-length sidecar).
    let framesLost = Atomic<Int>(0)
    let lastWriteError = Atomic<Int32>(0)
    let tapSilentFrames = Atomic<Int>(0)        // consecutive frames of exact-zero tap audio
    let micSilentFrames = Atomic<Int>(0)        // consecutive frames where the mic stayed below −70 dBFS
    let micLoudWhileSilent = Atomic<Int>(0)     // frames in that stretch where the mic was above −50 dBFS
    // IO-thread-only state.
    var pendingGapFromHost: UInt64 = 0           // set by a rebuild: the host time the old device stopped at
    var pendingGapReason = ""
    let scratch = UnsafeMutablePointer<Float>.allocate(capacity: 2 * 65_536)

    init(online: Bool, url: URL, stallSeconds: Double, inputUID: String? = nil) {
        self.online = online
        self.url = url
        self.stallSeconds = stallSeconds
        self.inputUID = inputUID
        self.stallThreshold = stallSeconds
        control.setSpecific(key: Self.onControl, value: true)
    }

    deinit { scratch.deallocate() }      // here, not in stop(): a start that throws never reaches stop()

    func start() throws {
        // The chosen microphone is looked up here, at the start, not only when the menu was read: one unplugged in
        // between still falls back to the default, and says so.
        if let inputUID, let device = MeetingMicrophones.device(uid: inputUID) {
            inputDevice = device
        } else {
            if inputUID != nil { inputFallback = "The chosen microphone is not connected: recording from the system default." }
            inputDevice = try MeetingMicrophones.defaultInput()
        }
        activeUID = try? getString(inputDevice, kAudioDevicePropertyDeviceUID)
        inputName = (try? getString(inputDevice, kAudioObjectPropertyName)) ?? "?"
        try buildDevice()
        sampleRate = try getValue(ioDevice, kAudioDevicePropertyNominalSampleRate, initial: Float64(48_000))
        var fmt = clientFormat(sampleRate)
        // IMA4 in a CAF: an hour of two channels at 48 kHz is ~170 MB instead of 1.4 GB of float PCM, and a file cut
        // short by a quit or a crash still opens (MeetingFiles.trimPartialPacket), where an .m4a has no index.
        fileRate = sampleRate
        let f = try openFile(fmt: &fmt)
        do { try check(ExtAudioFileWriteAsync(f, 0, nil), "prime async writer") } catch { ExtAudioFileDispose(f); throw error }
        file = f
        started = Date()
        lastAudio.store(mach_absolute_time(), ordering: .relaxed)    // the clock starts at the start
        pendingGapFromHost = lastAudio.load(ordering: .relaxed)
        pendingGapReason = "device start"
        try startIO()
        let timer = DispatchSource.makeTimerSource(queue: control)
        timer.schedule(deadline: .now() + 1, repeating: 1)
        timer.setEventHandler { [weak self] in self?.tick() }
        timer.resume()
        self.timer = timer
        log("recording \(online ? "online (mic + system)" : "in person (mic)") at \(Int(sampleRate)) Hz from \"\(inputName)\"; input buffers \(bufferChannels), mic buffers \(micBuffers)")
    }

    /// Creates the IMA4 CAF file at `fileRate` and sets the client format (float PCM at the device's rate). IMA4 has a
    /// fixed packet size and no bit-rate setting, so the 24 kHz AirPods case (bug 2026-11-27: an AAC bit rate refused,
    /// a 28-byte file) cannot recur.
    func openFile(fmt: inout AudioStreamBasicDescription) throws -> ExtAudioFileRef {
        var ima = AudioStreamBasicDescription(mSampleRate: fileRate, mFormatID: kAudioFormatAppleIMA4, mFormatFlags: 0,
                                              mBytesPerPacket: 34 * channels, mFramesPerPacket: 64, mBytesPerFrame: 0,
                                              mChannelsPerFrame: channels, mBitsPerChannel: 0, mReserved: 0)
        var created: ExtAudioFileRef?
        try check(ExtAudioFileCreateWithURL(url as CFURL, kAudioFileCAFType, &ima, nil, AudioFileFlags.eraseFile.rawValue, &created), "create file")
        guard let f = created else { throw NSError(domain: "MeetingCapture", code: 2, userInfo: [NSLocalizedDescriptionKey: "create file returned no file"]) }
        do {
            try check(ExtAudioFileSetProperty(f, kExtAudioFileProperty_ClientDataFormat, UInt32(MemoryLayout<AudioStreamBasicDescription>.size), &fmt), "client format")
        } catch { ExtAudioFileDispose(f); throw error }
        return f
    }

    /// The float PCM format the IO thread hands the writer at `rate`.
    func clientFormat(_ rate: Double) -> AudioStreamBasicDescription {
        AudioStreamBasicDescription(mSampleRate: rate, mFormatID: kAudioFormatLinearPCM,
                                    mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
                                    mBytesPerPacket: 4 * channels, mFramesPerPacket: 1, mBytesPerFrame: 4 * channels,
                                    mChannelsPerFrame: channels, mBitsPerChannel: 32, mReserved: 0)
    }

    /// Gaps longer than this are filled only this far, and the rest is said in the sidecar.
    static let maxGapFill: Double = 4 * 3600

    func buildDevice() throws {
        guard online else {
            ioDevice = inputDevice
            bufferChannels = try inputBufferChannels(ioDevice)
            micBuffers = bufferChannels.count
            return
        }
        let own = ownProcessObject()
        let desc = CATapDescription(monoGlobalTapButExcludeProcesses: own != 0 ? [own] : [])
        desc.uuid = UUID()
        desc.name = "SHAFT meeting tap"
        desc.isPrivate = true
        desc.muteBehavior = .unmuted
        var t = AudioObjectID(0)
        try check(AudioHardwareCreateProcessTap(desc, &t), "create process tap (is System Audio Recording allowed for this app?)")
        tap = t
        let micUID = try getString(inputDevice, kAudioDevicePropertyDeviceUID)
        let agg: [String: Any] = [
            kAudioAggregateDeviceNameKey: "SHAFT meeting",
            kAudioAggregateDeviceUIDKey: MeetingMicrophones.aggregatePrefix + UUID().uuidString,
            kAudioAggregateDeviceMainSubDeviceKey: micUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: micUID]],
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapUIDKey: desc.uuid.uuidString, kAudioSubTapDriftCompensationKey: true]],
        ]
        var a = AudioObjectID(0)
        try check(AudioHardwareCreateAggregateDevice(agg as CFDictionary, &a), "create aggregate device")
        aggregate = a
        ioDevice = a
        bufferChannels = try inputBufferChannels(a)
        micBuffers = try inputBufferChannels(inputDevice).count
        if bufferChannels.count <= micBuffers {
            throw NSError(domain: "MeetingCapture", code: 1, userInfo: [NSLocalizedDescriptionKey:
                "the aggregate shows \(bufferChannels.count) input buffers, the mic alone \(micBuffers): no tap stream to record"])
        }
    }

    func startIO() throws {
        var pid: AudioDeviceIOProcID?
        try check(AudioDeviceCreateIOProcIDWithBlock(&pid, ioDevice, io) { [unowned self] _, input, inputTime, _, _ in
            self.onAudio(input, inputTime)
        }, "create IOProc")
        procID = pid
        lastCallback.store(mach_absolute_time(), ordering: .relaxed)
        try check(AudioDeviceStart(ioDevice, pid), "start device")
    }

    func teardownDevice() {
        if let pid = procID { AudioDeviceStop(ioDevice, pid); AudioDeviceDestroyIOProcID(ioDevice, pid) }
        procID = nil
        if aggregate != 0 { AudioHardwareDestroyAggregateDevice(aggregate); aggregate = 0 }
        if tap != 0 { AudioHardwareDestroyProcessTap(tap); tap = 0 }
    }

    // MARK: IO thread

    func onAudio(_ input: UnsafePointer<AudioBufferList>, _ time: UnsafePointer<AudioTimeStamp>) {
        let list = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
        guard list.count > 0, list[0].mNumberChannels > 0 else { return }
        let frames = Int(list[0].mDataByteSize) / (4 * Int(list[0].mNumberChannels))
        guard frames > 0, frames <= 65_536 else { return }
        let host = time.pointee.mFlags.contains(.hostTimeValid) ? time.pointee.mHostTime : mach_absolute_time()

        if pendingGapFromHost != 0 {                // first callback after a rebuild: keep the timeline aligned
            let missing = max(0, hostSeconds(host) - hostSeconds(pendingGapFromHost))
            fillGap(seconds: missing, reason: pendingGapReason)
            pendingGapFromHost = 0
        }

        let ch = Int(channels)
        var micSq: Float = 0
        var tapMax: Float = 0
        for i in 0..<frames {
            var mic: Float = 0, micN = 0, sys: Float = 0, sysN = 0
            for (b, buf) in list.enumerated() {
                guard let data = buf.mData?.assumingMemoryBound(to: Float.self) else { continue }
                let n = Int(buf.mNumberChannels)
                for c in 0..<n {
                    let v = data[i * n + c]
                    if b < micBuffers { mic += v; micN += 1 } else { sys += v; sysN += 1 }
                }
            }
            mic = micN > 0 ? mic / Float(micN) : 0
            sys = sysN > 0 ? sys / Float(sysN) : 0
            micSq += mic * mic
            tapMax = max(tapMax, abs(sys))
            scratch[i * ch] = mic
            if ch == 2 { scratch[i * ch + 1] = sys }
        }
        write(frames: frames)
        lastCallback.store(host, ordering: .relaxed)
        lastAudio.store(host, ordering: .relaxed)
        if tapMax > 0 { tapEverHeard.store(true, ordering: .relaxed) }

        if (micSq / Float(frames)).squareRoot() < 0.0003 {
            micSilentFrames.add(frames, ordering: .relaxed)
        } else {
            micSilentFrames.store(0, ordering: .relaxed)
        }
        if online {
            if tapMax == 0 {
                tapSilentFrames.add(frames, ordering: .relaxed)
                if (micSq / Float(frames)).squareRoot() > 0.003 { micLoudWhileSilent.add(frames, ordering: .relaxed) }
            } else {
                tapSilentFrames.store(0, ordering: .relaxed)
                micLoudWhileSilent.store(0, ordering: .relaxed)
            }
        }
    }

    func write(frames: Int) {
        guard let f = file else { return }
        var abl = AudioBufferList(mNumberBuffers: 1, mBuffers: AudioBuffer(mNumberChannels: channels,
                                  mDataByteSize: UInt32(frames * Int(channels) * 4), mData: scratch))
        let status = ExtAudioFileWriteAsync(f, UInt32(frames), &abl)
        // Counted in the file's frames: the device rate may differ after a rebuild.
        let inFile = sampleRate == fileRate ? frames : Int((Double(frames) * fileRate / sampleRate).rounded())
        if status == noErr {
            framesWritten.add(inFile, ordering: .relaxed)
        } else {
            lastWriteError.store(status, ordering: .relaxed)       // before the count: the watchdog reads both
            framesLost.add(inFile, ordering: .releasing)
        }
    }

    /// Fill a stretch without audio with silence, up to maxGapFill (bug 2026-11-27 review: a lid closed for more than
    /// 10 minutes was filled only 10 minutes, so everything after it moved earlier, unsaid).
    func fillGap(seconds: Double, reason: String) {
        writeSilence(seconds: min(seconds, Self.maxGapFill), reason: reason)
        if seconds > Self.maxGapFill {
            let note = String(format: "%.0f min without audio (%@): only the first %.0f min are kept as silence, so "
                              + "later times are early by %.0f min", seconds / 60, reason, Self.maxGapFill / 60,
                              (seconds - Self.maxGapFill) / 60)
            if DispatchQueue.getSpecific(key: MeetingCapture.onControl) != nil { notes.append(note) } else { control.async { self.notes.append(note) } }
        }
    }

    func writeSilence(seconds: Double, reason: String) {
        guard seconds > 0.005 else { return }
        let start = Double(framesWritten.load(ordering: .relaxed)) / fileRate
        var left = Int(seconds * sampleRate)
        while left > 0 {
            let n = min(left, 65_536 / 2)
            scratch.update(repeating: 0, count: n * Int(channels))
            write(frames: n)
            left -= n
        }
        let gap = MeetingGap(start: start, end: Double(framesWritten.load(ordering: .relaxed)) / fileRate, reason: reason)
        if DispatchQueue.getSpecific(key: MeetingCapture.onControl) != nil { gaps.append(gap) } else { control.async { self.gaps.append(gap) } }
    }

    static let onControl = DispatchSpecificKey<Bool>()

    /// True when `file` can be sent: its sidecar says it ended, or it was cut short and still holds audio (then the
    /// sidecar is written from the file, saying so). False for a file with no readable audio.
    static func recover(_ file: URL) -> Bool {
        let side = file.deletingPathExtension().appendingPathExtension("json")
        var fields = ((try? Data(contentsOf: side)).flatMap { try? JSONSerialization.jsonObject(with: $0) }) as? [String: Any] ?? [:]
        if fields["ended"] != nil { return secondsInFile(file) != nil || isUploaded(file) }
        try? MeetingFiles.trimPartialPacket(of: file)
        guard let seconds = secondsInFile(file), seconds > 0 else { return false }
        let created = (try? file.resourceValues(forKeys: [.creationDateKey]))?.creationDate ?? Date()
        let iso = ISO8601DateFormatter()
        let started = (fields["started"] as? String).flatMap(iso.date(from:)) ?? created
        fields["started"] = iso.string(from: started)
        fields["ended"] = iso.string(from: started.addingTimeInterval(seconds))
        fields["seconds"] = seconds
        fields["mode"] = fields["mode"] ?? "online"
        fields["problems"] = ((fields["problems"] as? [String]) ?? [])
            + [String(format: "SHAFT stopped before the recording ended: %.0f s were recovered", seconds)]
        if let data = try? JSONSerialization.data(withJSONObject: fields, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: side)
        }
        return true
    }

    /// A file whose upload finished (kept 7 days as insurance): never listed as damaged.
    private static func isUploaded(_ file: URL) -> Bool {
        FileManager.default.fileExists(atPath: file.appendingPathExtension("shaft-upload").path)
    }

    /// Seconds of audio a finished file holds, or nil when it cannot be opened as audio (no moov atom).
    static func secondsInFile(_ url: URL) -> Double? {
        var ref: ExtAudioFileRef?
        guard ExtAudioFileOpenURL(url as CFURL, &ref) == noErr, let ref else { return nil }
        defer { ExtAudioFileDispose(ref) }
        var frames: Int64 = 0
        var format = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<Int64>.size)
        guard ExtAudioFileGetProperty(ref, kExtAudioFileProperty_FileLengthFrames, &size, &frames) == noErr else { return nil }
        size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        guard ExtAudioFileGetProperty(ref, kExtAudioFileProperty_FileDataFormat, &size, &format) == noErr,
              format.mSampleRate > 0 else { return nil }
        return Double(frames) / format.mSampleRate
    }

    // MARK: watchdog (control queue)

    func tick() {
        // Said while the meeting is still going on, not at the stop (bug 2026-11-27: every write was refused for
        // 17 minutes and the operator learnt it only from the failed transcript). Checked first, so no other branch
        // of the watchdog can skip it.
        if !warnedWriteFailed, framesLost.load(ordering: .acquiring) > 0 {
            warnedWriteFailed = true
            onWarning?("Audio is not being saved (OSStatus \(lastWriteError.load(ordering: .relaxed))). Stop and start "
                       + "the recording again, or record from the iPhone.")
        }
        let now = mach_absolute_time()
        let sinceCallback = hostSeconds(now) - hostSeconds(lastCallback.load(ordering: .relaxed))
        if sinceCallback > noCallbackGrace {
            // A device that brings no callbacks after a rebuild is given longer each time: rebuilding every 2 s
            // never let a slow start begin (bug 2026-11-25: 17 rebuilds in 98 s, 52 s lost).
            rebuild(reason: "no-callbacks \(Int(sinceCallback)) s")
            noCallbackGrace = min(noCallbackGrace * 2, 30)
            return
        }
        if hostSeconds(now) - hostSeconds(lastAudio.load(ordering: .relaxed)) < 1 { noCallbackGrace = 2 }
        // A muted, dead or wrong microphone records a file of silence (review 2026-11-27): said while it can be fixed.
        if !warnedSilentMic, Double(micSilentFrames.load(ordering: .relaxed)) / sampleRate > 30 {
            warnedSilentMic = true
            onWarning?("The microphone \"\(inputName)\" has heard nothing for 30 s. Check it is not muted, or choose "
                       + "another one under Meeting → Microphone and record again.")
        } else if warnedSilentMic, micSilentFrames.load(ordering: .relaxed) == 0 {
            warnedSilentMic = false
        }
        guard online else { return }
        if !warnedSilentTap, !warnedWriteFailed, !tapEverHeard.load(ordering: .relaxed),
           Double(tapSilentFrames.load(ordering: .relaxed)) / sampleRate > 10 {
            warnedSilentTap = true
            onWarning?("No sound from other apps for 10 s. If the call is playing, allow SHAFT under System Settings → "
                       + "Privacy & Security → Screen & System Audio Recording → System Audio Recording Only.")
        }
        let silent = Double(tapSilentFrames.load(ordering: .relaxed)) / sampleRate
        let loud = Double(micLoudWhileSilent.load(ordering: .relaxed)) / sampleRate
        if silent > stallThreshold && loud > 2 {
            rebuild(reason: "tap-silent \(Int(silent)) s while mic heard \(Int(loud)) s")
            stallThreshold = min(stallThreshold * 2, 300)   // a truly silent far end must not cause a rebuild loop
        } else if silent == 0 {
            stallThreshold = stallSeconds
        }
    }

    func rebuild(reason: String) {
        log("rebuilding capture: \(reason)")
        // The gap runs from the last audio written, also across rebuilds that brought none (bug 2026-11-25: each
        // rebuild restarted the gap at its own start, so only 1 of 17 gaps was filled and the file came out short).
        let stoppedAt = lastAudio.load(ordering: .relaxed)
        teardownDevice()
        if let activeUID, let device = MeetingMicrophones.device(uid: activeUID) { inputDevice = device }
        do {
            try buildDevice()
            try followDeviceRate()
            pendingGapFromHost = stoppedAt
            pendingGapReason = reason
            tapSilentFrames.store(0, ordering: .relaxed)
            micLoudWhileSilent.store(0, ordering: .relaxed)
            try startIO()
            rebuilds.append(["at": iso(Date()), "reason": reason])
            warnedRebuildFailed = false
        } catch {
            log("rebuild failed: \(error.localizedDescription); retrying in 1 s")
            rebuilds.append(["at": iso(Date()), "reason": reason + " (failed: \(error.localizedDescription))"])
            // Said once while it lasts (review 2026-11-27: only logged, the menu still said Recording).
            if !warnedRebuildFailed {
                warnedRebuildFailed = true
                onWarning?("Recording is paused: the audio device could not be restarted (\(error.localizedDescription)). "
                           + "SHAFT keeps trying; the missing time becomes a gap.")
            }
        }
    }

    /// After a rebuild the device may run at another rate (AirPods switch to 24 kHz for a call): the client format
    /// follows it, and ExtAudioFile converts to the file's rate. Runs with the IO stopped.
    func followDeviceRate() throws {
        let rate = try getValue(ioDevice, kAudioDevicePropertyNominalSampleRate, initial: Float64(sampleRate))
        guard rate > 0, rate != sampleRate, let f = file else { return }
        var fmt = clientFormat(rate)
        try check(ExtAudioFileSetProperty(f, kExtAudioFileProperty_ClientDataFormat,
                                          UInt32(MemoryLayout<AudioStreamBasicDescription>.size), &fmt), "client format at \(Int(rate)) Hz")
        log("device rate \(Int(sampleRate)) → \(Int(rate)) Hz: converting to the file's \(Int(fileRate)) Hz")
        sampleRate = rate
    }

    /// Stop and finalise the file; the sidecar (also written next to the file) is returned. Runs on the control queue,
    /// so it never races the watchdog.
    func stop() -> [String: Any] {
        control.sync { () -> [String: Any] in
        timer?.cancel()
        timer = nil
        teardownDevice()
        // Audio that never came before the stop is still time on the meeting's clock: pad it, as a gap.
        let missing = hostSeconds(mach_absolute_time()) - hostSeconds(lastAudio.load(ordering: .relaxed))
        if file != nil, missing > 0.5 { fillGap(seconds: missing, reason: "no audio before stop") }
        var finalise: OSStatus = noErr
        if let f = file { finalise = ExtAudioFileDispose(f); file = nil }
        // The writes the async writer accepted are not proof the audio reached the file (it reports a failure on a
        // later call, or only at the dispose): the length is read back from the file itself.
        let accepted = Double(framesWritten.load(ordering: .relaxed)) / fileRate
        let held = Self.secondsInFile(url)
        let seconds = held ?? 0
        let lost = Double(framesLost.load(ordering: .relaxed)) / fileRate
        var problems: [String] = notes
        if lost > 0 {
            problems.append(String(format: "%.1f s of audio could not be written (OSStatus %d)", lost,
                                   lastWriteError.load(ordering: .relaxed)))
        }
        if finalise != noErr { problems.append("the file could not be finalised (OSStatus \(finalise))") }
        if held == nil, accepted > 0 {
            problems.append(String(format: "the file holds no readable audio of the %.1f s recorded", accepted))
        } else if accepted - seconds > 1 {
            problems.append(String(format: "the file holds %.1f s of the %.1f s recorded", seconds, accepted))
        }
        if !problems.isEmpty { log("stop: " + problems.joined(separator: "; ")) }
        let sidecar: [String: Any] = [
            "started": iso(started), "ended": iso(Date()), "mode": online ? "online" : "inperson",
            "input_device": inputName, "input_device_uid": activeUID ?? "?", "input_chosen": inputUID ?? "default", "sample_rate": fileRate, "channels": Int(channels),
            "channel_map": online ? ["mic", "system"] : ["mic"], "seconds": seconds,
            "input_buffers": bufferChannels, "mic_buffers": micBuffers,
            "gaps": gaps.map { ["start": $0.start, "end": $0.end, "reason": $0.reason] }, "rebuilds": rebuilds,
            "seconds_lost": lost, "problems": problems,
        ]
        let side = url.deletingPathExtension().appendingPathExtension("json")
        if let data = try? JSONSerialization.data(withJSONObject: sidecar, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: side)
        }
        log(String(format: "stopped: %.1f s written, %d gap(s)", seconds, gaps.count))
        return sidecar
        }
    }
}

