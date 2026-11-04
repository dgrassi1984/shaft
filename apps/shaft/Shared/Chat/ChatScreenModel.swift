import SHAFTKit
import SwiftUI

/// What one chat screen shows over its store: the sheets (Select text, a rating's reason, documents, earlier chats),
/// focus requests for the composer and its quote cards, and the actions the feed's items call. One per chat screen;
/// the feed and the composer read it from the environment.
@MainActor
@Observable
final class ChatScreenModel {
    /// Text the Select text sheet shows: a message's (rendered) text, or a document's.
    struct Selecting: Identifiable, Equatable {
        let id = UUID()
        let text: String
        /// The document it comes from (`""`: the chat): a quote from a document says so (§6).
        var doc = ""
    }

    /// A thumb that needs its reason: down (What was wrong?) or Send… on a kept one.
    struct RatingTarget: Identifiable, Equatable {
        let id = UUID()
        var k: Int?
        var file: String?
        var thumb: String
        /// The rating to send (Send…), nil for a new thumbs down.
        var ratingID: String?
        var reason: String
    }

    let chat: ChatStore
    let instance: String
    /// Quick Look for the chat's files and the documents' downloads.
    let preview = FilePreview()

    var selecting: Selecting?
    var rating: RatingTarget?
    /// The documents sheet (or inspector) and what it should open.
    var showDocuments = false
    var openDocument: String?
    /// The document on show in the panel: what opens again when the system reopens a panel it dropped in a turn of the
    /// device.
    var shownDocument: String?
    var openThread: String?
    /// The documents cover the chat (a sheet on a phone) rather than sit beside it: then they close to show the
    /// composer after ↩ Reply, and the chat is not watched while they are open (`watch=0`, §4).
    var documentsCover = false
    var showEarlierChats = false
    /// A Retry that asks first (`failure.retry.confirm`).
    var confirmRetry: FailureRetry?
    /// The composer's main field should take the keyboard.
    var focusComposerRequest = 0
    /// A quote card's answer should take the keyboard (a message's own Reply).
    var focusQuoteRequest: Int?
    /// A send asked for from outside the composer (the Mac's menu: Queue after reply, Next cycle): the composer sends
    /// its draft in that mode, through its own send.
    var sendRequest: SendMode?
    /// New chat asks first (the header's button, the Mac's menu).
    var confirmingNewChat = false
    var confirmingEnd = false

    init(chat: ChatStore) {
        self.chat = chat
        instance = chat.instance
    }

    var composerCovered = false
    var dismissRequest = 0

    /// Return to the chat for notification and link navigation.
    func dismissPresentations() {
        selecting = nil
        rating = nil
        showDocuments = false
        showEarlierChats = false
        confirmRetry = nil
        confirmingNewChat = false
        confirmingEnd = false
        preview.url = nil
        preview.error = nil
        dismissRequest += 1
    }

    var archived: Bool { chat.cid != nil || chat.content?.archived == true }

    // MARK: Message actions

    /// Reply on a whole message: its Markdown source as a quote card, its answer field focused.
    func reply(with source: String, doc: String = "") {
        guard !archived else { return }
        let index = chat.addQuote(source.trimmingCharacters(in: .whitespacesAndNewlines), doc: doc)
        focusQuoteRequest = index
    }

    /// ↩ Reply or + Quote on a selection: a card. On a computer the new card's answer takes the keyboard; a phone or
    /// tablet keeps the keyboard as it is (down until an answer field is tapped).
    func quote(_ selection: String, doc: String = "") {
        guard !archived else { return }
        let text = selection.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        let index = chat.addQuote(text, doc: doc)
        #if os(macOS)
        focusQuoteRequest = index
        #else
        _ = index
        #endif
    }

    func select(_ text: String, doc: String = "") {
        selecting = Selecting(text: text, doc: doc)
    }

    func openDocument(_ path: String, thread: String? = nil) {
        openDocument = path
        openThread = thread
        showDocuments = true
    }

    // MARK: Ratings (§9)

    /// Thumbs up saves at once; thumbs down asks why first (What was wrong?). A lit thumb tapped again clears it.
    func thumb(_ thumb: String, k: Int? = nil, file: String? = nil, current: Rating?) {
        if let current, current.thumb == thumb {
            Task { await chat.clearRating(current.id) }
            return
        }
        if thumb == "up" {
            Task { await chat.rate(k: k, file: file, thumb: "up") }
        } else {
            rating = RatingTarget(k: k, file: file, thumb: "down", ratingID: nil, reason: "")
        }
    }

    /// Send… on a kept rating: the sheet with its reason filled in.
    func sendRating(_ current: Rating, k: Int? = nil, file: String? = nil) {
        rating = RatingTarget(k: k, file: file, thumb: current.thumb, ratingID: current.id, reason: current.reason)
    }

    /// The reason sheet's answer: `send` delivers it, else it is kept.
    func finishRating(_ target: RatingTarget, reason: String, send: Bool) async {
        let trimmed = reason.trimmingCharacters(in: .whitespacesAndNewlines)
        var id = target.ratingID
        if id == nil {
            id = await chat.rate(k: target.k, file: target.file, thumb: target.thumb, reason: trimmed.isEmpty ? nil : trimmed)
        }
        if send, let id {
            await chat.sendRating(id, reason: target.ratingID != nil && trimmed != target.reason ? trimmed : nil)
        }
        rating = nil
    }

    // MARK: Retry (§4.4)

    func retry(_ retry: FailureRetry) {
        if retry.confirm != nil {
            confirmRetry = retry
        } else {
            Task { await chat.retryReply(retry) }
        }
    }
}
