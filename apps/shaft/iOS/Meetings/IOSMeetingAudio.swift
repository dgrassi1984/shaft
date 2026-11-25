import AVFoundation
import UIKit

enum IOSMeetingAudioEvent: Sendable {
    case interrupted
    case interruptionEnded(shouldResume: Bool)
    case failed(String)
}

@MainActor
protocol IOSMeetingAudio: AnyObject {
    var onEvent: ((IOSMeetingAudioEvent) -> Void)? { get set }
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
    private var previousCategory: (AVAudioSession.Category, AVAudioSession.Mode, AVAudioSession.CategoryOptions)?

    override init() {
        super.init()
        NotificationCenter.default.addObserver(self, selector: #selector(interruption(_:)),
                                               name: AVAudioSession.interruptionNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(mediaServicesReset(_:)),
                                               name: AVAudioSession.mediaServicesWereResetNotification, object: nil)
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
        let audio = try AVAudioRecorder(url: url, settings: [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: 48_000,
            AVNumberOfChannelsKey: 1,
            AVEncoderBitRateKey: 96_000
        ])
        audio.delegate = self
        recorder = audio
        guard audio.record() else { throw CaptureError.startFailed }
        return session.currentRoute.inputs.first?.portName ?? "Microphone"
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

    @objc private nonisolated func mediaServicesReset(_: Notification) {
        Task { @MainActor [weak self] in self?.onEvent?(.failed("The audio service restarted.")) }
    }

    nonisolated func audioRecorderDidFinishRecording(_ recorder: AVAudioRecorder, successfully flag: Bool) {
        if !flag { reportFailure(recorder, "The microphone stopped recording.") }
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
