import AppKit

/// An `NSMenuItem` that runs a closure: the text views' own menu items.
final class MenuAction: NSObject {
    private let body: () -> Void

    private init(_ body: @escaping () -> Void) { self.body = body }

    // Not `perform(_:)`: that is NSObject's performSelector:, which took the menu item for a selector and aborted.
    @objc private func runAction(_ sender: Any?) { body() }

    static func item(_ title: String, symbol: String? = nil, _ run: @escaping () -> Void) -> NSMenuItem {
        let action = MenuAction(run)
        let item = NSMenuItem(title: title, action: #selector(runAction(_:)), keyEquivalent: "")
        item.target = action
        item.representedObject = action            // the item keeps its target alive
        if let symbol { item.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil) }
        return item
    }
}

/// The right-click menu of read-only text (a message, Select text).
enum ReadOnlyTextMenu {
    /// The text view's menu without what read-only text cannot use: Cut, Paste, Font, Spelling, Substitutions.
    static func trim(_ menu: NSMenu) {
        let editing: Set<Selector> = [
            #selector(NSText.cut(_:)), #selector(NSText.paste(_:)), #selector(NSTextView.pasteAsPlainText(_:)),
            #selector(NSTextView.pasteAsRichText(_:)), #selector(NSFontManager.addFontTrait(_:)),
            #selector(NSFontManager.modifyFont(_:)), #selector(NSFontManager.orderFrontFontPanel(_:)),
            #selector(NSText.underline(_:)), #selector(NSText.copyFont(_:)), #selector(NSText.pasteFont(_:)),
            #selector(NSApplication.orderFrontColorPanel(_:)), #selector(NSText.showGuessPanel(_:)),
            #selector(NSTextView.orderFrontSubstitutionsPanel(_:)),
        ]
        for item in menu.items.reversed() {
            let inSubmenu = item.submenu?.items.contains { $0.action.map(editing.contains) ?? false } ?? false
            if (item.action.map(editing.contains) ?? false) || inSubmenu { menu.removeItem(item) }
        }
        // No separator first, last or twice.
        var previousSeparator = true
        for item in menu.items {
            if item.isSeparatorItem && previousSeparator {
                menu.removeItem(item)
            } else {
                previousSeparator = item.isSeparatorItem
            }
        }
        if let last = menu.items.last, last.isSeparatorItem { menu.removeItem(last) }
    }
}
