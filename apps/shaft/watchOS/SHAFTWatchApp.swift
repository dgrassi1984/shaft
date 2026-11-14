import SwiftUI

/// The Apple Watch app: talk to an instance from the wrist. It reaches the server only through the iPhone app
/// (iOS/WatchRelay.swift), which holds the pairing.
@main
struct SHAFTWatchApp: App {
    @State private var model = WatchModel()

    var body: some Scene {
        WindowGroup {
            InstanceListView()
                .environment(model)
                .task { model.activate() }
        }
    }
}
