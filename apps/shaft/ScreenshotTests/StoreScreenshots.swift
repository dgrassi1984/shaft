import ImageIO
import XCTest

/// The App Store screenshots, taken from the built-in demo (AppStore/README.md, "Screenshots"). Run per simulator:
///   TEST_RUNNER_SHAFT_SHOTS=<dir> xcodebuild test -scheme SHAFT-Screenshots -destination 'id=<udid>' …
/// Each screen is written as <dir>/<NN>-<name>.png at the device's full resolution.
@MainActor
final class StoreScreenshots: XCTestCase {
    private var app: XCUIApplication!
    private var directory: URL!

    override func setUp() async throws {
        continueAfterFailure = false
        let path = try XCTUnwrap(ProcessInfo.processInfo.environment["SHAFT_SHOTS"], "Set TEST_RUNNER_SHAFT_SHOTS")
        directory = URL(fileURLWithPath: path)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        app = XCUIApplication()
        // An iPad shows sidebar, chat and documents side by side only in landscape (2752 × 2064 on a 13" iPad).
        XCUIDevice.shared.orientation = UIDevice.current.userInterfaceIdiom == .pad ? .landscapeLeft : .portrait
        app.launch()
        allowNotificationsIfAsked()
    }

    func testStoreScreens() throws {
        let demo = app.buttons["Try the demo"]
        XCTAssertTrue(demo.waitForExistence(timeout: 10), "The pairing screen offers Try the demo")
        demo.tap()
        // The app reopens the conversation last shown, so open tundra's on purpose.
        openSwitcher()
        conversation("Build speed").tap()
        XCTAssertTrue(app.staticTexts["Build time: three experiments"].waitForExistence(timeout: 10))
        sleep(2)
        shot("01-chat")

        openSwitcher()
        sleep(1)
        shot("02-conversations")

        conversation("wisp").tap()
        XCTAssertTrue(app.buttons["Five-line summary"].waitForExistence(timeout: 10), "The question shows its choices")
        sleep(2)
        shot("03-question")

        openSwitcher()
        conversation("Field notes").tap()
        XCTAssertTrue(app.staticTexts["Write up what you learned this week."].waitForExistence(timeout: 10))
        app.buttons["Documents"].firstMatch.tap()
        let row = app.staticTexts["field-notes.md"].firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 10), "The documents panel lists the sample document")
        row.tap()
        XCTAssertTrue(app.staticTexts["What worked"].waitForExistence(timeout: 10))
        sleep(2)
        shot("04-document")

        app.buttons["Cycles"].firstMatch.tap()
        XCTAssertTrue(app.staticTexts["Next"].waitForExistence(timeout: 10), "The cycles panel opens")
        sleep(2)
        shot("05-plan")
        // The first cycle row in view, after scrolling the phone's panel down to the list.
        let goals = ["Write this week's field notes", "Draft the cache change", "Benchmark three build changes"]
        func visibleCycle() -> XCUIElement? {
            goals.map { app.staticTexts[$0].firstMatch }.first { $0.exists && $0.isHittable }
        }
        let panel = app.collectionViews.containing(.staticText, identifier: "Next").firstMatch
        for _ in 0..<4 where visibleCycle() == nil { panel.swipeUp() }
        if visibleCycle() == nil { dump("05-cycles") }
        let cycle = try XCTUnwrap(visibleCycle(), "The cycles panel lists the sample cycles")
        cycle.tap()
        sleep(3)
        shot("06-cycle")
    }

    /// What the review notes tell App Review: the demo's Settings say so, link the privacy policy, and Leave the demo
    /// goes back to the pairing screen. No screenshot.
    func testSettingsLeaveTheDemo() throws {
        let demo = app.buttons["Try the demo"]
        XCTAssertTrue(demo.waitForExistence(timeout: 10))
        demo.tap()
        openSwitcher()
        let settings = app.buttons["Settings"].firstMatch
        XCTAssertTrue(settings.waitForExistence(timeout: 5))
        settings.tap()
        XCTAssertTrue(app.staticTexts["Demo (sample data)"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["Privacy policy"].exists || app.links["Privacy policy"].exists)
        // Leave the demo is the form's last row: below the fold on a smaller phone, where XCUITest still calls it
        // hittable, so scroll the form to its end first.
        let form = app.collectionViews.containing(.staticText, identifier: "Demo (sample data)").firstMatch
        form.swipeUp()
        form.swipeUp()
        let leave = app.buttons["Leave the demo"]
        leave.tap()
        if !app.buttons["Try the demo"].waitForExistence(timeout: 10) { dump("leave-after"); shot("leave-after") }
        XCTAssertTrue(app.buttons["Try the demo"].exists, "Leaving returns to pairing")
    }

    /// The phone's conversation switcher; on iPad the sidebar is already there.
    private func openSwitcher() {
        guard app.windows.firstMatch.frame.width < 700 else { expandThreads(); return }
        let header = app.buttons.matching(NSPredicate(format: "label IN %@", ["tundra", "wisp", "ada", "Field notes", "Weekly review", "Build speed", "Weekly digest"])).firstMatch
        XCTAssertTrue(header.waitForExistence(timeout: 5))
        header.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).tap()
        XCTAssertTrue(app.buttons["Close"].waitForExistence(timeout: 5), "The header opens the conversation switcher")
        sleep(1)
        expandThreads()
    }

    /// Show tundra's and ada's threads in the list, as the screenshots of the conversations do.
    private func expandThreads() {
        for name in ["tundra", "ada"] where app.buttons["Expand threads for \(name)"].exists {
            app.buttons["Expand threads for \(name)"].tap()
        }
    }

    private func conversation(_ label: String) -> XCUIElement {
        let cell = app.cells.containing(.staticText, identifier: label).firstMatch
        XCTAssertTrue(cell.waitForExistence(timeout: 5), "A conversation named \(label)")
        return cell
    }

    private func dump(_ name: String) {
        try? app.debugDescription.write(to: directory.appending(path: "\(name).txt"), atomically: true, encoding: .utf8)
    }

    private func allowNotificationsIfAsked() {
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        let allow = springboard.alerts.buttons["Allow"]
        if allow.waitForExistence(timeout: 5) { allow.tap() }
    }

    /// The screen as App Store Connect takes it: upright pixels (a landscape iPad's capture is portrait pixels with a
    /// rotation flag) in an RGB PNG with no alpha channel.
    private func shot(_ name: String) {
        let image = XCUIScreen.main.screenshot().image
        let upright = UIGraphicsImageRenderer(size: image.size, format: {
            let format = UIGraphicsImageRendererFormat()
            format.scale = image.scale
            return format
        }()).image { _ in image.draw(in: CGRect(origin: .zero, size: image.size)) }
        guard let pixels = upright.cgImage,
              let context = CGContext(data: nil, width: pixels.width, height: pixels.height, bitsPerComponent: 8,
                                      bytesPerRow: 0, space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                      bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else {
            return XCTFail("Could not draw \(name)")
        }
        context.draw(pixels, in: CGRect(x: 0, y: 0, width: pixels.width, height: pixels.height))
        let url = directory.appending(path: "\(name).png") as CFURL
        guard let opaque = context.makeImage(),
              let file = CGImageDestinationCreateWithURL(url, "public.png" as CFString, 1, nil) else {
            return XCTFail("Could not write \(name)")
        }
        CGImageDestinationAddImage(file, opaque, nil)
        if !CGImageDestinationFinalize(file) { XCTFail("Could not write \(name)") }
    }
}
