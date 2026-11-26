import CoreAudio
import Foundation

/// One audio input the Mac can record a meeting from, named by its Core Audio UID (stable across launches and
/// reconnections, unlike the device id).
struct MeetingMicrophone: Identifiable, Hashable, Sendable {
    let uid: String
    let name: String
    var id: String { uid }
}

/// The microphone a meeting records from (docs/meetings.md): the operator's choice in the Meeting menu, kept across
/// launches by UID, or the system default input when none is chosen. A chosen microphone that is not connected when
/// recording starts is never swapped silently: the recording uses the system default and says so.
enum MeetingMicrophones {
    /// What to record from: the device UID (nil = the system default input) and, when the choice could not be
    /// honoured, the line the operator is shown.
    struct Resolution: Equatable, Sendable {
        var uid: String?
        var warning: String?
    }

    /// Resolve the saved choice against the inputs connected now.
    static func resolve(saved uid: String?, savedName: String?, available: [MeetingMicrophone]) -> Resolution {
        guard let uid else { return Resolution(uid: nil, warning: nil) }
        if available.contains(where: { $0.uid == uid }) { return Resolution(uid: uid, warning: nil) }
        let name = savedName.map { "“\($0)”" } ?? "The chosen microphone"
        return Resolution(uid: nil, warning: "\(name) is not connected: recording from the system default microphone.")
    }

    /// The Meeting menu's entries after System Default: every connected input, and the saved choice marked not
    /// connected when it is missing (so the menu still shows what is chosen).
    static func menuEntries(saved uid: String?, savedName: String?, available: [MeetingMicrophone]) -> [MeetingMicrophone] {
        guard let uid, !available.contains(where: { $0.uid == uid }) else { return available }
        return available + [MeetingMicrophone(uid: uid, name: "\(savedName ?? uid) (not connected)")]
    }

    // MARK: Core Audio

    /// Every device with at least one input channel, by name; SHAFT's own private meeting aggregates left out.
    static func connected() -> [MeetingMicrophone] {
        devices().compactMap { device -> MeetingMicrophone? in
            guard inputChannels(device) > 0, let uid = string(device, kAudioDevicePropertyDeviceUID),
                  !uid.hasPrefix(aggregatePrefix) else { return nil }
            return MeetingMicrophone(uid: uid, name: string(device, kAudioObjectPropertyName) ?? uid)
        }
        .sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
    }

    /// The system default input's name, for the menu's System Default line.
    static func defaultName() -> String? {
        guard let device = try? defaultInput() else { return nil }
        return string(device, kAudioObjectPropertyName)
    }

    /// The UID prefix of the aggregate devices MeetingCapture creates for online recordings.
    static let aggregatePrefix = "shaft-meeting-"

    static func defaultInput() throws -> AudioDeviceID {
        var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultInputDevice,
                                              mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var device = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        let status = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &device)
        guard status == noErr, device != 0 else {
            throw NSError(domain: "MeetingCapture", code: Int(status), userInfo: [NSLocalizedDescriptionKey:
                "this Mac has no default microphone (OSStatus \(status))"])
        }
        return device
    }

    /// The device with this UID, or nil when it is not connected.
    static func device(uid: String) -> AudioDeviceID? {
        var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyTranslateUIDToDevice,
                                              mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var cf = uid as CFString
        var device = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        let status = withUnsafePointer(to: &cf) { q in
            AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr,
                                       UInt32(MemoryLayout<CFString>.size), q, &size, &device)
        }
        guard status == noErr, device != kAudioObjectUnknown, inputChannels(device) > 0 else { return nil }
        return device
    }

    /// Call `handler` on the main queue whenever a device is plugged in or out or the default input changes. Kept for
    /// the app's lifetime (the recorder that asks lives as long as the app).
    static func observe(_ handler: @escaping @Sendable () -> Void) {
        for selector in [kAudioHardwarePropertyDevices, kAudioHardwarePropertyDefaultInputDevice] {
            var addr = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                                  mElement: kAudioObjectPropertyElementMain)
            AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &addr, .main) { _, _ in handler() }
        }
    }

    private static func devices() -> [AudioDeviceID] {
        var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices, mScope: kAudioObjectPropertyScopeGlobal,
                                              mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        let system = AudioObjectID(kAudioObjectSystemObject)
        guard AudioObjectGetPropertyDataSize(system, &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
        var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(system, &addr, 0, nil, &size, &ids) == noErr else { return [] }
        return ids
    }

    private static func inputChannels(_ device: AudioDeviceID) -> Int {
        var addr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreamConfiguration,
                                              mScope: kAudioObjectPropertyScopeInput, mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(device, &addr, 0, nil, &size) == noErr, size > 0 else { return 0 }
        let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
        defer { raw.deallocate() }
        guard AudioObjectGetPropertyData(device, &addr, 0, nil, &size, raw) == noErr else { return 0 }
        return UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
            .reduce(0) { $0 + Int($1.mNumberChannels) }
    }

    private static func string(_ device: AudioDeviceID, _ selector: AudioObjectPropertySelector) -> String? {
        var addr = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                              mElement: kAudioObjectPropertyElementMain)
        var value: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        guard AudioObjectGetPropertyData(device, &addr, 0, nil, &size, &value) == noErr else { return nil }
        return value?.takeRetainedValue() as String?
    }
}
