import AudioToolbox
import CoreAudio
import Foundation
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
/// AAC file — channel 1 the mic, channel 2 the system audio. A watchdog rebuilds tap + aggregate when callbacks stop or
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
    var sampleRate: Double = 48_000
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
        var fmt = AudioStreamBasicDescription(mSampleRate: sampleRate, mFormatID: kAudioFormatLinearPCM,
                                              mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
                                              mBytesPerPacket: 4 * channels, mFramesPerPacket: 1, mBytesPerFrame: 4 * channels,
                                              mChannelsPerFrame: channels, mBitsPerChannel: 32, mReserved: 0)
        // AAC in an .m4a: an hour of two channels is ~85 MB instead of 1.4 GB of float PCM.
        var aac = AudioStreamBasicDescription(mSampleRate: sampleRate, mFormatID: kAudioFormatMPEG4AAC, mFormatFlags: 0,
                                              mBytesPerPacket: 0, mFramesPerPacket: 1024, mBytesPerFrame: 0,
                                              mChannelsPerFrame: channels, mBitsPerChannel: 0, mReserved: 0)
        var f: ExtAudioFileRef?
        try check(ExtAudioFileCreateWithURL(url as CFURL, kAudioFileM4AType, &aac, nil, AudioFileFlags.eraseFile.rawValue, &f), "create file")
        file = f
        try check(ExtAudioFileSetProperty(f!, kExtAudioFileProperty_ClientDataFormat, UInt32(MemoryLayout<AudioStreamBasicDescription>.size), &fmt), "client format")
        var converter: AudioConverterRef?
        var converterSize = UInt32(MemoryLayout<AudioConverterRef?>.size)
        if ExtAudioFileGetProperty(f!, kExtAudioFileProperty_AudioConverter, &converterSize, &converter) == noErr, let converter {
            var bitRate = UInt32(96_000) * channels                     // ~96 kbit/s per channel
            if AudioConverterSetProperty(converter, kAudioConverterEncodeBitRate, UInt32(MemoryLayout<UInt32>.size), &bitRate) == noErr {
                var config: CFArray? = nil
                ExtAudioFileSetProperty(f!, kExtAudioFileProperty_ConverterConfig, UInt32(MemoryLayout<CFArray?>.size), &config)
            }
        }
        try check(ExtAudioFileWriteAsync(f!, 0, nil), "prime async writer")
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
            writeSilence(seconds: min(missing, 600), reason: pendingGapReason)
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
        if status == noErr {
            framesWritten.add(frames, ordering: .relaxed)
        } else {
            framesLost.add(frames, ordering: .relaxed)
            lastWriteError.store(status, ordering: .relaxed)
        }
    }

    func writeSilence(seconds: Double, reason: String) {
        guard seconds > 0.005 else { return }
        let start = Double(framesWritten.load(ordering: .relaxed)) / sampleRate
        var left = Int(seconds * sampleRate)
        while left > 0 {
            let n = min(left, 65_536 / 2)
            scratch.update(repeating: 0, count: n * Int(channels))
            write(frames: n)
            left -= n
        }
        let gap = MeetingGap(start: start, end: Double(framesWritten.load(ordering: .relaxed)) / sampleRate, reason: reason)
        if DispatchQueue.getSpecific(key: MeetingCapture.onControl) != nil { gaps.append(gap) } else { control.async { self.gaps.append(gap) } }
    }

    static let onControl = DispatchSpecificKey<Bool>()

    // MARK: watchdog (control queue)

    func tick() {
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
        guard online else { return }
        if !warnedSilentTap, !tapEverHeard.load(ordering: .relaxed),
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
            pendingGapFromHost = stoppedAt
            pendingGapReason = reason
            tapSilentFrames.store(0, ordering: .relaxed)
            micLoudWhileSilent.store(0, ordering: .relaxed)
            try startIO()
            rebuilds.append(["at": iso(Date()), "reason": reason])
        } catch {
            log("rebuild failed: \(error.localizedDescription); retrying in 1 s")
            rebuilds.append(["at": iso(Date()), "reason": reason + " (failed: \(error.localizedDescription))"])
        }
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
        if file != nil, missing > 0.5 { writeSilence(seconds: min(missing, 600), reason: "no audio before stop") }
        var finalise: OSStatus = noErr
        if let f = file { finalise = ExtAudioFileDispose(f); file = nil }
        let seconds = Double(framesWritten.load(ordering: .relaxed)) / sampleRate
        let lost = Double(framesLost.load(ordering: .relaxed)) / sampleRate
        var problems: [String] = []
        if lost > 0 {
            problems.append(String(format: "%.1f s of audio could not be written (OSStatus %d)", lost,
                                   lastWriteError.load(ordering: .relaxed)))
        }
        if finalise != noErr { problems.append("the file could not be finalised (OSStatus \(finalise))") }
        if !problems.isEmpty { log("stop: " + problems.joined(separator: "; ")) }
        let sidecar: [String: Any] = [
            "started": iso(started), "ended": iso(Date()), "mode": online ? "online" : "inperson",
            "input_device": inputName, "input_device_uid": activeUID ?? "?", "input_chosen": inputUID ?? "default", "sample_rate": sampleRate, "channels": Int(channels),
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

