import XCTest

/// The meeting microphone choice: a connected choice is used as is, a missing one falls back to the system default
/// and says so (never silently), and the menu keeps showing a missing choice so the operator sees what is chosen.
final class MeetingMicrophonesTests: XCTestCase {
    private let builtIn = MeetingMicrophone(uid: "BuiltInMicrophoneDevice", name: "MacBook Pro Microphone")
    private let usb = MeetingMicrophone(uid: "AppleUSBAudioEngine:Shure:MV7:1", name: "Shure MV7")

    func testNoChoiceRecordsFromTheSystemDefault() {
        let r = MeetingMicrophones.resolve(saved: nil, savedName: nil, available: [builtIn, usb])
        XCTAssertNil(r.uid)
        XCTAssertNil(r.warning)
    }

    func testConnectedChoiceIsUsed() {
        let r = MeetingMicrophones.resolve(saved: usb.uid, savedName: usb.name, available: [builtIn, usb])
        XCTAssertEqual(r.uid, usb.uid)
        XCTAssertNil(r.warning)
    }

    func testMissingChoiceFallsBackToDefaultAndSaysSo() {
        let r = MeetingMicrophones.resolve(saved: usb.uid, savedName: usb.name, available: [builtIn])
        XCTAssertNil(r.uid, "a missing microphone must not be handed to the capture")
        XCTAssertEqual(r.warning, "“Shure MV7” is not connected: recording from the system default microphone.")
    }

    func testMenuKeepsAMissingChoiceMarked() {
        XCTAssertEqual(MeetingMicrophones.menuEntries(saved: usb.uid, savedName: usb.name, available: [builtIn, usb]),
                       [builtIn, usb])
        let entries = MeetingMicrophones.menuEntries(saved: usb.uid, savedName: usb.name, available: [builtIn])
        XCTAssertEqual(entries.map(\.uid), [builtIn.uid, usb.uid])
        XCTAssertEqual(entries.last?.name, "Shure MV7 (not connected)")
        XCTAssertEqual(MeetingMicrophones.menuEntries(saved: nil, savedName: nil, available: [builtIn]), [builtIn])
    }

    /// On this Mac: every listed input resolves back to a device, and SHAFT's own aggregates are never offered.
    func testConnectedInputsResolveToDevices() {
        let mics = MeetingMicrophones.connected()
        for mic in mics {
            XCTAssertFalse(mic.uid.hasPrefix(MeetingMicrophones.aggregatePrefix))
            XCTAssertNotNil(MeetingMicrophones.device(uid: mic.uid), mic.name)
        }
        XCTAssertNil(MeetingMicrophones.device(uid: "no-such-device-\(UUID().uuidString)"))
    }
}
