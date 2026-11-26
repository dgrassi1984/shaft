import Foundation
import Testing

/// What keeps the chat screen quick (measured on a Release build against `fixture_server.py act rich`, 2026-11-26):
/// each pin names the stall that came back without it.
struct ChatPerformancePinTests {
    private func source(_ path: String) throws -> String {
        let repo = URL(filePath: #filePath)
            .deletingLastPathComponent()  // SHAFTKitTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // SHAFTKit
            .deletingLastPathComponent()  // Packages
            .deletingLastPathComponent()  // shaft
            .deletingLastPathComponent()  // apps
            .deletingLastPathComponent()  // repository
        return try String(contentsOf: repo.appending(path: "apps/shaft/" + path), encoding: .utf8)
    }

    /// An eager VStack built, rendered and measured all 80 turns at every chat switch: the main thread was held
    /// 650–690 ms per switch, and a streaming reply stalled it 100–290 ms at a time.
    @Test func theFeedBuildsOnlyTheRowsOnScreen() throws {
        let feed = try source("Shared/Feed/FeedView.swift")
        #expect(feed.contains("LazyVStack(alignment: .leading, spacing: Design.Space.turn)"))
        #expect(!feed.contains("            VStack(alignment: .leading, spacing: Design.Space.turn)"))
    }

    /// Regression (v0.80.0, chat blank until scrolled): with the lazy feed, a scroll to the bottom edge goes to the end
    /// of an estimated height (19,004 pt for a feed that measured 7,805), past the last row, and nothing is built
    /// there until a wheel event moves it: reopening a long chat showed an empty feed 3 times in 5. A scroll to the
    /// foot by id builds and places the last rows; the foot carries the bottom margin, so its end is the feed's end.
    @Test func theFeedLandsOnItsEndByIdNotOnAnEstimatedEdge() throws {
        let feed = try source("Shared/Feed/FeedView.swift")
        #expect(!feed.contains("scrollTo(edge: .bottom)"))
        #expect(feed.contains("@State private var position = ScrollPosition(id: Self.bottomID, anchor: .bottom)"))
        #expect(feed.contains("withTransaction(transaction) { position.scrollTo(id: Self.bottomID, anchor: .bottom) }"))
        #expect(feed.contains("Color.clear.frame(height: metrics.fade - Design.Space.turn).id(Self.bottomID)"))
        #expect(!feed.contains(".padding(.bottom, metrics.fade - Design.Space.turn)"))
    }

    /// The distance to the end in points was view state: every wheel event re-evaluated the whole feed (28–34 fps
    /// scrolling a long chat at 75 Hz). Only a change of band may update the feed.
    @Test func scrollingUpdatesTheFeedOnlyWhenTheDistanceBandChanges() throws {
        let feed = try source("Shared/Feed/FeedView.swift")
        #expect(feed.contains("@State private var distance = Distance.atEnd"))
        #expect(feed.contains(".onScrollGeometryChange(for: Distance.self)"))
        #expect(!feed.contains(".onScrollGeometryChange(for: CGFloat.self) { geometry in\n            geometry.contentSize.height + geometry.contentInsets.bottom"))
    }

    /// A message's view is built again each time it scrolls into sight or its chat opens: it takes its rendering from
    /// the cache instead of parsing the Markdown again.
    @Test func theMacMessageTextComesFromTheRenderCache() throws {
        let text = try source("macOS/MessageTextView.swift")
        #expect(text.contains("view.show(RenderCache.rendered(key) {"))
    }

    /// Stalls are logged in every build, so a slow click on the operator's own device can be read back.
    @Test func bothAppsWatchTheMainThread() throws {
        #expect(try source("macOS/SHAFTApp.swift").contains("HitchMonitor.start()"))
        #expect(try source("iOS/SHAFTApp.swift").contains("HitchMonitor.start()"))
    }

    /// A dev run's recorder sent the operator's real pending recordings to the fixture server it was paired with.
    @Test func aDevRunKeepsItsRecordingsInItsOwnFolder() throws {
        #expect(try source("macOS/SHAFTApp.swift").contains("folder: MacDevSession.directory?.appending(path: \"Meetings\""))
    }
}
