#if DEBUG
import Foundation
import QuartzCore
#if os(iOS)
import UIKit
#else
import AppKit
#endif

/// Per-second frame counts for the streaming baseline, enabled by SHAFT_DEV_FRAMES.
@MainActor
final class FrameMeter: NSObject {
    private static var meter: FrameMeter?
    private let file: FileHandle
    private var link: CADisplayLink?
    private var previous: CFTimeInterval?
    private var second: CFTimeInterval?
    private var frames = 0
    private var slow = 0

    static func startIfAsked() {
        guard meter == nil, let path = ProcessInfo.processInfo.environment["SHAFT_DEV_FRAMES"],
              !path.isEmpty else { return }
        do {
            if !FileManager.default.fileExists(atPath: path) {
                guard FileManager.default.createFile(atPath: path, contents: nil) else {
                    throw CocoaError(.fileWriteUnknown)
                }
            }
            let file = try FileHandle(forWritingTo: URL(fileURLWithPath: path))
            try file.seekToEnd()
            let meter = FrameMeter(file: file)
            #if os(iOS)
            let link = CADisplayLink(target: meter, selector: #selector(tick(_:)))
            #else
            guard let screen = NSScreen.main else {
                try file.close()
                return
            }
            let link = screen.displayLink(target: meter, selector: #selector(tick(_:)))
            #endif
            meter.link = link
            Self.meter = meter
            link.add(to: .main, forMode: .common)
        } catch {
            NSLog("FrameMeter: %@", error.localizedDescription)
        }
    }

    private init(file: FileHandle) {
        self.file = file
        super.init()
    }

    @objc private func tick(_ link: CADisplayLink) {
        defer { previous = link.timestamp }
        guard let previous else {
            second = link.timestamp
            return
        }
        let interval = link.targetTimestamp - link.timestamp
        frames += 1
        if interval > 0, link.timestamp - previous > interval * 1.5 { slow += 1 }
        guard let second, link.timestamp - second >= 1 else { return }
        // Monotonic seconds, observed frames, frames over 1.5 times the refresh interval.
        let row = String(format: "%.3f,%d,%d\n", link.timestamp, frames, slow)
        do {
            try file.write(contentsOf: Data(row.utf8))
        } catch {
            NSLog("FrameMeter: %@", error.localizedDescription)
            link.invalidate()
            try? file.close()
        }
        self.second = link.timestamp
        frames = 0
        slow = 0
    }
}
#endif
