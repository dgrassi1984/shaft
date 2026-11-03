import AppKit
import Testing

/// The text views' own menu items (macOS/MenuAction.swift) and the read-only text's right-click menu.
@MainActor
struct MenuActionTests {
    /// Regression (driven 2026-11-03): the item's action was `perform(_:)`, which is NSObject's performSelector:, so
    /// picking ↩ Reply, Copy message or a thumb in a message's right-click menu took the item for a selector and the
    /// app aborted ("NSForwarding: … does not match selector known to Objective C runtime").
    @Test func pickingAnItemRunsItsClosure() throws {
        var ran = 0
        let item = MenuAction.item("Reply to message", symbol: "arrowshape.turn.up.left") { ran += 1 }
        let action = try #require(item.action)
        #expect(action != NSSelectorFromString("performSelector:"))
        #expect(NSApplication.shared.sendAction(action, to: item.target, from: item))
        #expect(ran == 1)
    }

    @Test func theItemKeepsItsTargetAlive() throws {
        var ran = false
        let item = MenuAction.item("Copy message") { ran = true }
        weak var target = item.target as AnyObject?
        #expect(target != nil)
        _ = NSApplication.shared.sendAction(try #require(item.action), to: item.target, from: item)
        #expect(ran)
    }

    @Test func readOnlyTextKeepsCopyAndLosesEditing() {
        let menu = NSMenu()
        menu.addItem(withTitle: "Look Up", action: NSSelectorFromString("_lookUpDefiniteRangeInDictionaryFromMenu:"),
                     keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "")
        menu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "")
        menu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "")
        menu.addItem(.separator())
        let font = NSMenuItem(title: "Font", action: nil, keyEquivalent: "")
        font.submenu = NSMenu()
        font.submenu?.addItem(withTitle: "Show Fonts", action: #selector(NSFontManager.orderFrontFontPanel(_:)),
                              keyEquivalent: "")
        menu.addItem(font)
        menu.addItem(.separator())
        ReadOnlyTextMenu.trim(menu)
        #expect(menu.items.map { $0.isSeparatorItem ? "|" : $0.title } == ["Look Up", "|", "Copy"])
    }
}
