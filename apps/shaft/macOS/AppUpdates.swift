import Combine
import Foundation
import Sparkle
import SwiftUI

/// The Mac app's self-update (Sparkle), for the Developer ID download only. Started only when `UpdateFeed` allows it:
/// once a day it asks the feed for the newest version (Info.plist: no prompt first, no system profile), shows
/// Sparkle's own dialog when there is one, and installs only when the user says so. Checking by hand is the app
/// menu's "Check for Updates…"; Settings turns the daily check off. A check that fails on its own (offline, the feed
/// unreachable) shows nothing; one asked for by hand says what went wrong.
@MainActor
final class AppUpdates: ObservableObject {
    let feed: UpdateFeed
    /// Sparkle can start a check now (none is running).
    @Published private(set) var canCheck = false
    @Published private(set) var automaticallyChecks = false
    private let controller: SPUStandardUpdaterController?
    private var watching: Set<AnyCancellable> = []

    init(info: [String: Any]? = Bundle.main.infoDictionary) {
        feed = UpdateFeed.availability(info: info)
        guard case .on = feed else {
            controller = nil
            return
        }
        let controller = SPUStandardUpdaterController(startingUpdater: true, updaterDelegate: nil,
                                                      userDriverDelegate: nil)
        self.controller = controller
        controller.updater.publisher(for: \.canCheckForUpdates)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] can in self?.canCheck = can }
            .store(in: &watching)
        controller.updater.publisher(for: \.automaticallyChecksForUpdates)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] on in self?.automaticallyChecks = on }
            .store(in: &watching)
    }

    /// The build updates itself (the menu item and the Settings rows act).
    var available: Bool { controller != nil }

    func checkNow() {
        controller?.checkForUpdates(nil)
    }

    func setAutomaticallyChecks(_ on: Bool) {
        controller?.updater.automaticallyChecksForUpdates = on
    }
}

/// The app menu's "Check for Updates…", after About. Off while a check runs, and in a build that does not update
/// itself (the item then says why it is off).
struct CheckForUpdatesCommand: View {
    @ObservedObject var updates: AppUpdates

    var body: some View {
        if updates.available {
            Button("Check for Updates…") { updates.checkNow() }
                .disabled(!updates.canCheck)
        } else {
            Button("Updates: Not in This Build") {}
                .disabled(true)
                .help(updates.feed.unavailableNote ?? "")
        }
    }
}

/// Settings' updates rows: the daily check and Check Now, or why this build has neither.
struct AppUpdatesRows: View {
    @ObservedObject var updates: AppUpdates

    var body: some View {
        if updates.available {
            Toggle("Check for updates automatically",
                   isOn: Binding(get: { updates.automaticallyChecks }, set: { updates.setAutomaticallyChecks($0) }))
            Button("Check Now") { updates.checkNow() }
                .disabled(!updates.canCheck)
        } else if let note = updates.feed.unavailableNote {
            Text(note).font(.footnote).foregroundStyle(Color.textSecondary)
        }
    }
}

extension EnvironmentValues {
    /// The app's updater, for Settings' updates rows (absent outside the Mac app's windows: no rows).
    @Entry var appUpdates: AppUpdates? = nil
}
