import SHAFTKit
import SwiftUI
#if canImport(UIKit)
import UIKit
#else
import AppKit
#endif

// Small things every shared view uses: how times read, the clipboard, announcements, and the platform seams that are
// one line each. The bigger seams (the composer's text view, selectable text) have their own files.

enum Stamp {
    /// "14:05": a message's time (the day is its separator's).
    static func time(_ date: Date) -> String {
        date.formatted(.dateTime.hour().minute())
    }

    /// "14:05" today, "Mon 2 Nov, 14:05" otherwise: a time that stands alone (a rating's "sent", a thread's post).
    static func short(_ date: Date, calendar: Calendar = .current) -> String {
        calendar.isDateInToday(date) ? time(date) : date.formatted(.dateTime.weekday(.abbreviated).day().month().hour().minute())
    }

    /// The full date and time, for VoiceOver and a long press.
    static func full(_ date: Date) -> String {
        date.formatted(date: .complete, time: .shortened)
    }

    /// A day separator: "Today", "Yesterday", or the date ("Monday 2 November 2026").
    static func day(_ date: Date, calendar: Calendar = .current) -> String {
        if calendar.isDateInToday(date) { return String(localized: "Today") }
        if calendar.isDateInYesterday(date) { return String(localized: "Yesterday") }
        return date.formatted(.dateTime.weekday(.wide).day().month(.wide).year())
    }

    /// How long a reply has run: "0:42", "12:05", "1:02:03".
    static func elapsed(since start: Date, now: Date) -> String {
        let seconds = max(0, Int(now.timeIntervalSince(start)))
        let (h, m, s) = (seconds / 3600, seconds / 60 % 60, seconds % 60)
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, s) : String(format: "%d:%02d", m, s)
    }
}

enum Clipboard {
    static func copy(_ text: String) {
        #if canImport(UIKit)
        UIPasteboard.general.string = text
        #else
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        #endif
    }
}

enum Announce {
    /// Say `text` to VoiceOver (a new reply: the PWA's live region).
    static func say(_ text: String) {
        AccessibilityNotification.Announcement(text).post()
    }
}

extension View {
    /// The shared 44 pt hit area.
    func touchTarget() -> some View {
        quietHitArea()
    }

    /// An animation unless Reduce Motion is on.
    func motion<V: Equatable>(_ animation: Animation?, value: V, reduce: Bool) -> some View {
        self.animation(reduce ? nil : animation, value: value)
    }
}

/// A short-lived status line under the composer (an action's `said`, or its failure).
struct StatusLine: View {
    let text: String
    var isError = false
    var dismiss: () -> Void

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Design.Space.s8) {
            Image(systemName: isError ? "exclamationmark.triangle.fill" : "checkmark.circle")
                .foregroundStyle(isError ? Color.danger : Color.textSecondary)
                .accessibilityHidden(true)
            Text(text)
                .font(.footnote)
                .foregroundStyle(isError ? Color.danger : Color.textSecondary)
                .frame(maxWidth: .infinity, alignment: .leading)
            Button(action: dismiss) {
                Image(systemName: "xmark")
                    .font(.footnote)
                    .foregroundStyle(Color.textSecondary)
                    .touchTarget()
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Dismiss")
        }
        .padding(.horizontal, Design.Space.gutter)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(isError ? [] : .updatesFrequently)
    }
}

/// The base URL's web page for a path (`/i/thelos/activity`, `/services`): failure links open in the browser.
extension ServerCredential {
    func webURL(_ path: String) -> URL? {
        URL(string: path, relativeTo: baseURL)?.absoluteURL
    }
}

extension View {
    /// A sheet's size on the Mac, where a sheet is as small as its content allows (a form, a text view or a list
    /// collapses), and its forms grouped as in Settings (the Mac's default form put labels in a column cut at the
    /// sheet's edge); a phone or tablet sizes sheets itself.
    func sheetFrame(minWidth: CGFloat = 520, minHeight: CGFloat = 420) -> some View {
        #if os(macOS)
        formStyle(.grouped)
            .frame(minWidth: minWidth, idealWidth: minWidth + 80, minHeight: minHeight, idealHeight: minHeight + 80)
        #else
        self
        #endif
    }
}

extension View {
    /// A composer's icon menu on the Mac: the icon alone, as on iOS (no bezel, no chevron beside it); a click on a
    /// menu with a primary action runs it, a click and hold opens the menu.
    func iconMenu() -> some View {
        #if os(macOS)
        menuStyle(.button).buttonStyle(.plain).menuIndicator(.hidden).fixedSize()
        #else
        self
        #endif
    }
}

extension ToolbarItemPlacement {
    /// A sheet's own action (↩ Reply, Comment): in its bar on iOS; on the Mac, where a sheet shows only its confirm
    /// and cancel buttons (a primary action was not drawn at all), on the confirm side.
    static var sheetAction: ToolbarItemPlacement {
        #if os(macOS)
        .confirmationAction
        #else
        .primaryAction
        #endif
    }

    /// Done on a sheet that has its own action: confirms on iOS; on the Mac the action takes the confirm side.
    static var sheetDone: ToolbarItemPlacement {
        #if os(macOS)
        .cancellationAction
        #else
        .confirmationAction
        #endif
    }
}
