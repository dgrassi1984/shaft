import XCTest

/// Regression guard: an everyday build must never offer itself the official download, and a build whose feed or
/// settings cannot be trusted (plain http to another host, no public key, archives unpacked before their signature is
/// checked, an unsigned feed accepted) must not check at all.
final class UpdateFeedTests: XCTestCase {
    private let key = "UL5pMxmGBWOti6qcS1cTtSEHib522ioxVhuK7wJ7TjE="

    private func info(_ feed: String?, key: String? = nil, verify: Bool? = true, signedFeed: Bool? = true)
        -> [String: Any] {
        var info: [String: Any] = [:]
        info["SUFeedURL"] = feed
        info["SUPublicEDKey"] = key ?? self.key
        info["SUVerifyUpdateBeforeExtraction"] = verify
        info["SURequireSignedFeed"] = signedFeed
        return info
    }

    func testNoFeedMeansNoChecks() {
        XCTAssertEqual(UpdateFeed.availability(info: nil), .noFeed)
        XCTAssertEqual(UpdateFeed.availability(info: info(nil)), .noFeed)
        XCTAssertEqual(UpdateFeed.availability(info: info("")), .noFeed)
        XCTAssertEqual(UpdateFeed.availability(info: info("  ")), .noFeed)
        XCTAssertNotNil(UpdateFeed.noFeed.unavailableNote)
    }

    func testHttpsFeedStarts() {
        let feed = "https://example.github.io/shaft-app/appcast.xml"
        XCTAssertEqual(UpdateFeed.availability(info: info(feed)), .on(URL(string: feed)!))
        XCTAssertNil(UpdateFeed.on(URL(string: feed)!).unavailableNote)
    }

    func testPlainHttpOnlyToThisMac() {
        for feed in ["http://127.0.0.1:8765/appcast.xml", "http://localhost/appcast.xml"] {
            XCTAssertEqual(UpdateFeed.availability(info: info(feed)), .on(URL(string: feed)!), feed)
        }
        for feed in ["http://example.com/appcast.xml", "http://127.0.0.1.example.com/appcast.xml",
                     "http://localhost@example.com/appcast.xml", "ftp://127.0.0.1/appcast.xml", "appcast.xml",
                     "https:///appcast.xml"] {
            guard case .refused = UpdateFeed.availability(info: info(feed)) else {
                return XCTFail("\(feed) must be refused")
            }
        }
    }

    func testMissingKeyOrSafetySettingsRefuse() {
        let feed = "https://example.github.io/shaft-app/appcast.xml"
        for bad in [info(feed, key: ""), info(feed, verify: nil), info(feed, verify: false),
                    info(feed, signedFeed: nil), info(feed, signedFeed: false)] {
            guard case .refused(let reason) = UpdateFeed.availability(info: bad) else {
                return XCTFail("\(bad) must be refused")
            }
            XCTAssertFalse(reason.isEmpty)
        }
    }
}
