import SwiftUI

/// Device-local choices. Store raw strings so an unknown value can fall back to System.
enum AppTheme: String, CaseIterable {
    case system, light, dark

    static let storageKey = "appearance.theme"
    static func stored(_ raw: String) -> Self { Self(rawValue: raw) ?? .system }

    var colorScheme: ColorScheme? {
        switch self {
        case .system: nil
        case .light: .light
        case .dark: .dark
        }
    }

    var title: LocalizedStringKey {
        switch self {
        case .system: "System"
        case .light: "Light"
        case .dark: "Dark"
        }
    }
}

enum ChatTextSize: String, CaseIterable {
    case system, small, large, extraLarge, huge

    static let storageKey = "appearance.textSize"
    static func stored(_ raw: String) -> Self { Self(rawValue: raw) ?? .system }

    var scale: CGFloat {
        switch self {
        case .system: 1
        case .small: 0.9
        case .large: 1.12
        case .extraLarge: 1.25
        case .huge: 1.4
        }
    }

    var title: LocalizedStringKey {
        switch self {
        case .system: "System"
        case .small: "Small"
        case .large: "Large"
        case .extraLarge: "Extra large"
        case .huge: "Huge"
        }
    }

    #if os(iOS)
    var dynamicTypeSize: DynamicTypeSize? {
        switch self {
        case .system: nil
        case .small: .small
        case .large: .xLarge
        case .extraLarge: .xxLarge
        case .huge: .xxxLarge
        }
    }
    #endif
}

extension View {
    /// Applied only to reading content. System leaves inherited accessibility sizes intact.
    func readingTextSize() -> some View { modifier(ReadingTextSize()) }
}

private struct ReadingTextSize: ViewModifier {
    @Environment(\.dynamicTypeSize) private var inheritedSize
    @AppStorage(ChatTextSize.storageKey) private var rawSize = ChatTextSize.system.rawValue

    @ViewBuilder func body(content: Content) -> some View {
        #if os(iOS)
        if !inheritedSize.isAccessibilitySize, let size = ChatTextSize.stored(rawSize).dynamicTypeSize {
            content.dynamicTypeSize(size)
        } else {
            content
        }
        #else
        content
        #endif
    }
}

/// Kept separate from server settings so it also works before pairing.
struct AppearanceSection: View {
    @AppStorage(AppTheme.storageKey) private var rawTheme = AppTheme.system.rawValue
    @AppStorage(ChatTextSize.storageKey) private var rawSize = ChatTextSize.system.rawValue

    var body: some View {
        Section("Appearance") {
            Picker("Theme", selection: Binding(get: { AppTheme.stored(rawTheme) }, set: { rawTheme = $0.rawValue })) {
                ForEach(AppTheme.allCases, id: \.self) { theme in
                    Text(theme.title).tag(theme)
                }
            }
            Picker("Chat and document text", selection: Binding(get: { ChatTextSize.stored(rawSize) }, set: { rawSize = $0.rawValue })) {
                ForEach(ChatTextSize.allCases, id: \.self) { size in
                    Text(size.title).tag(size)
                }
            }
        }
        .listRowBackground(Color.elevated)
    }
}
