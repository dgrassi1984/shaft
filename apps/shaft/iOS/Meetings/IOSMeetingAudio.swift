import AVFoundation
import UIKit

enum IOSMeetingAudioEvent: Sendable {
    case interrupted
    case interruptionEnded(shouldResume: Bool)
    case routeChanged(input: String, recording: Bool)
    case failed(String)
}

@MainActor
protocol IOSMeetingAudio: AnyObject {
    var onEvent: ((IOSMeetingAudioEvent) -> Void)? { get set }
    var currentTime: TimeInterval { get }
    /// The microphone's level now, 0 (silence or below −60 dBFS) to 1 (full scale).
    var level: Float { get }
    func requestPermission() async -> Bool
    func start(at url: URL) throws -> String
    func pause()
    func resume() throws -> Bool
    func stop()
}

@MainActor
final class SystemMeetingAudio: NSObject, IOSMeetingAudio, AVAudioRecorderDelegate {
    var onEvent: ((IOSMeetingAudioEvent) -> Void)?
    private let session = AVAudioSession.sharedInstance()
    private var recorder: AVAudioRecorder?
    var currentTime: TimeInterval { recorder?.currentTime ?? 0 }
    var level: Float {
        guard let recorder, recorder.isRecording else { return 0 }
        recorder.updateMeters()
        return max(0, min(1, (recorder.averagePower(forChannel: 0) + 60) / 60))
    }
    private var inputName: String { session.currentRoute.inputs.first?.portName ?? "Microphone" }
    private var previousCategory: (AVAudioSession.Category, AVAudioSession.Mode, AVAudioSession.CategoryOptions)?

    override init() {
        super.init()
        NotificationCenter.default.addObserver(self, selector: #selector(interruption(_:)),
                                               name: AVAudioSession.interruptionNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(mediaServicesReset(_:)),
                                               name: AVAudioSession.mediaServicesWereResetNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(routeChanged(_:)),
                                               name: AVAudioSession.routeChangeNotification, object: nil)
    }

    func requestPermission() async -> Bool {
        await AVAudioApplication.requestRecordPermission()
    }

    func start(at url: URL) throws -> String {
        guard UIApplication.shared.applicationState != .background else {
            throw CaptureError.background
        }
        previousCategory = (session.category, session.mode, session.categoryOptions)
        try session.setCategory(.record, mode: .measurement)
        try session.setActive(true)
        // IMA4 in CAF: fixed-size packets and no index written at the end, so a recording cut short by iOS ending the
        // app stays readable up to its last packet. AAC in .m4a is unreadable without the index written on close
        // (2026-11-26: "Recording damaged"). 24 kHz mono, about 0.7 MB a minute; transcription uses 16 kHz.
        let audio = try AVAudioRecorder(url: url, settings: IOSMeetingRecorder.recordingSettings)
        audio.delegate = self
        audio.isMeteringEnabled = true
        recorder = audio
        guard audio.record() else { throw CaptureError.startFailed }
        return inputName
    }

    func pause() { recorder?.pause() }

    func resume() throws -> Bool {
        try session.setActive(true)
        return recorder?.record() == true
    }

    func stop() {
        recorder?.delegate = nil
        recorder?.stop()
        recorder = nil
        if let (category, mode, options) = previousCategory {
            previousCategory = nil
            try? session.setActive(false, options: .notifyOthersOnDeactivation)
            try? session.setCategory(category, mode: mode, options: options)
        }
    }

    @objc private nonisolated func interruption(_ notification: Notification) {
        guard let raw = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
              let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
        let options = notification.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0
        let shouldResume = AVAudioSession.InterruptionOptions(rawValue: options).contains(.shouldResume)
        let event: IOSMeetingAudioEvent
        switch type {
        case .began: event = .interrupted
        case .ended: event = .interruptionEnded(shouldResume: shouldResume)
        @unknown default: return
        }
        Task { @MainActor [weak self] in self?.onEvent?(event) }
    }

    @objc private nonisolated func routeChanged(_ notification: Notification) {
        guard let raw = notification.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt,
              AVAudioSession.RouteChangeReason(rawValue: raw) == .oldDeviceUnavailable else { return }
        Task { @MainActor [weak self] in
            guard let self, let recorder else { return }
            onEvent?(.routeChanged(input: inputName, recording: recorder.isRecording))
        }
    }

    @objc private nonisolated func mediaServicesReset(_: Notification) {
        Task { @MainActor [weak self] in self?.onEvent?(.failed("The audio service restarted.")) }
    }

    nonisolated func audioRecorderDidFinishRecording(_ recorder: AVAudioRecorder, successfully flag: Bool) {
        reportFailure(recorder, flag ? "The microphone stopped recording unexpectedly."
                                    : "The microphone could not finish recording.")
    }

    nonisolated func audioRecorderEncodeErrorDidOccur(_ recorder: AVAudioRecorder, error: (any Error)?) {
        reportFailure(recorder, error?.localizedDescription ?? "The recording could not be encoded.")
    }

    private nonisolated func reportFailure(_ recorder: AVAudioRecorder, _ problem: String) {
        let id = ObjectIdentifier(recorder)
        Task { @MainActor [weak self] in
            guard let self, let current = self.recorder, ObjectIdentifier(current) == id else { return }
            onEvent?(.failed(problem))
        }
    }

    private enum CaptureError: LocalizedError {
        case background
        case startFailed

        var errorDescription: String? {
            switch self {
            case .background: "Bring SHAFT to the foreground to record."
            case .startFailed: "The microphone could not start recording."
            }
        }
    }
}
