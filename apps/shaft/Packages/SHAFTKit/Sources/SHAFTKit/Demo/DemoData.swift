import CryptoKit
import Foundation

/// Fictional response bodies in the app API's wire format. Dates are relative to the start of this run.
enum DemoData {
    static let reply = "This is the demo: replies here are canned. Paired with your own SHAFT server, the instance answers for real."
    static let markdown = """
    ## Build time: three experiments

    I ran each change five times overnight and kept the median.

    | Change | Median build | Effort |
    | --- | --- | --- |
    | Cache dependencies | 6:40 → 2:55 | Low |
    | Split the test suite | 6:40 → 4:10 | Medium |
    | Bigger runner | 6:40 → 5:30 | Costs more |

    - Caching wins on its own: **56% faster** with one config change.
    - Splitting the suite only helps once the cache is in.

    ```yaml
    cache:
      key: deps-${{ hashFiles('uv.lock') }}
      paths: [.venv]
    ```
    """
    static let document = """
    # Field notes

    What this week's cycles taught me, and what changes next.

    ## What worked

    1. Checking sources before summarising cut corrections from five to one.
    2. Batching small tasks into one cycle saved about twenty minutes a day.
    3. Asking one clear question beat guessing twice.

    ## To try next

    - Measure the digest's open rate before changing its format again.
    - Keep each experiment to one change, so the result means something.

    > Measure first, then change one thing at a time.
    """

    /// The document's version as the server names it: the SHA-256 of its bytes.
    static let documentSHA = SHA256.hash(data: Data(document.utf8)).map { String(format: "%02x", $0) }.joined()

    static func stamp(_ date: Date) -> String { ISO8601DateFormatter().string(from: date) }

    static func user(_ text: String, k: Int, mid: String, at: Date, voice: Bool = false) -> [String: Any] {
        ["key": "u\(k)", "type": "user", "k": k, "ts": stamp(at), "text": text, "mid": mid,
         "interjected": false, "via": voice ? "voice" as Any : NSNull(), "attachments": [],
         "edits": [], "deleted": [], "comments": [], "feedback": []]
    }

    static func assistant(_ text: String, k: Int, at: Date) -> [String: Any] {
        ["key": "a\(k)", "type": "assistant", "k": k, "ts": stamp(at), "content": text,
         "activity": [], "incomplete": false, "failure": NSNull(), "docs": [], "answered": [],
         "opened": [], "rating": NSNull(), "last": true]
    }

    static func question(at: Date) -> [String: Any] {
        ["key": "sdemo-question.md", "type": "sent", "file": "demo-question.md", "ts": stamp(at),
         "kind": "question", "read": false, "text": "Your reading list has 14 new articles this week. How should I send the digest?",
         "attachments": [], "choices": ["multiple": false, "options":
            ["Five-line summary", "A paragraph each", "Only the top three"].map { ["label": $0] }],
         "answer": NSNull(), "open": true, "from_cycle": false, "re": NSNull(), "rating": NSNull()]
    }

    /// Last week's digest, which wisp sent from a cycle: what From holds above its question.
    static func digest(at: Date) -> [String: Any] {
        ["key": "sdemo-digest.md", "type": "sent", "file": "demo-digest.md", "ts": stamp(at), "kind": "message",
         "read": true, "text": """
         **Last week's digest:** 11 articles saved, 3 worth your time.

         1. **Why small test suites fail slowly**: flaky tests cost more than slow ones. *6 min*
         2. **A field guide to caching builds**: what to cache, and what never to. *9 min*
         3. **Writing notes your future self can use**: one idea per note. *4 min*

         The other eight repeat these, so I left them out.
         """, "attachments": [], "choices": NSNull(), "answer": NSNull(), "open": false, "from_cycle": true,
         "re": NSNull(), "rating": NSNull()]
    }

    static func feed(_ name: String, cid: String, title: String, at: Date) -> [String: Any] {
        let items: [[String: Any]]
        if name == "wisp" {
            items = [user("Can you keep an eye on my reading list and send me a weekly digest?", k: 0, mid: "demo-0",
                          at: at.addingTimeInterval(-1100)),
                     assistant("Yes. Every Sunday I'll go through what you saved that week, drop the duplicates and send you the digest here.",
                               k: 1, at: at.addingTimeInterval(-1040)),
                     question(at: at.addingTimeInterval(-180))]
        } else if name == "tundra" {
            items = [user("How did the build-time experiments go?", k: 0, mid: "demo-0", at: at.addingTimeInterval(-900)),
                     assistant(markdown, k: 1, at: at.addingTimeInterval(-840)),
                     user("Great. Draft the cache change for me.", k: 2, mid: "demo-2", at: at.addingTimeInterval(-600)),
                     assistant("Done: **build-cache.md** is in Documents, with the config change and how to roll it back. I'll measure again after tonight's builds.",
                               k: 3, at: at.addingTimeInterval(-540))]
        } else {
            items = [user(cid == "ada-notes" ? "Write up what you learned this week." : "What's on your list for the weekly review?",
                          k: 0, mid: "demo-0", at: at.addingTimeInterval(-700)),
                     assistant(cid == "ada-notes" ? "The field notes are ready in Documents: three things that worked and two to try next."
                               : "Three items: close the open questions from Monday, re-check last week's numbers, and plan two experiments.",
                               k: 1, at: at.addingTimeInterval(-650))]
        }
        return ["instance": name, "partial": false, "version": "1", "live": "", "reply": NSNull(),
                "chat": ["id": cid, "title": title, "state": "idle", "created": stamp(at.addingTimeInterval(-1200)), "ended": NSNull()],
                "archived": false, "earlier": 0, "items": items, "queued": [], "pending_feedback": [],
                "pending_edits": [], "mids": items.compactMap { $0["mid"] as? String }, "last": stamp(at.addingTimeInterval(-180)),
                "can_stop": false, "can_end": true, "can_clear": true]
    }

    static func plan() -> [String: Any] {
        let now = Date()
        let clock = DateFormatter()
        clock.dateFormat = "HH:mm"
        return ["now": ["state": "idle", "since": NSNull(), "goal": NSNull()],
                "check": ["next": clock.string(from: now.addingTimeInterval(5400)), "skipped": "", "paused": false],
                "next_step": ["action": "Re-run the build benchmark after tonight's builds",
                              "revise_if": "The cache change is not merged yet", "at": stamp(now.addingTimeInterval(-600))],
                "unfinished": ["Compare the cached build on a cold runner"], "commitments": [], "commitments_error": "",
                "queue": ["state": "rows", "open": 2, "overdue": 0, "error": "", "rows": [
                    ["id": 1, "task": "Send the weekly digest", "due_at": stamp(now.addingTimeInterval(86_400)), "overdue": false],
                    ["id": 2, "task": "Ask before changing the test split", "due_at": NSNull(), "overdue": false,
                     "due_at_text": "when ready"]]],
                "projects": ["open": 1, "error": "", "rows": [
                    ["id": "demo-build", "name": "Faster builds", "success": "Median build under three minutes",
                     "status": "open", "opened_at": stamp(now.addingTimeInterval(-3 * 86_400)),
                     "deadline": stamp(now.addingTimeInterval(4 * 86_400)), "health": "on track", "hours_left": 96.0,
                     "calls": 18, "budget_calls": 60, "usd": 0.42, "budget_usd": 3.0, "evidence": NSNull(),
                     "closed_at": NSNull()]]],
                "goals": "Keep the build fast and the weekly digest worth reading.",
                "goals_error": "", "improvement": NSNull(), "improvement_error": ""]
    }

    /// One past cycle; `goal` picks its story (the three goals the cycles route lists).
    static func cycle(_ id: String, at: Date, goal: String) -> [String: Any] {
        let story: (ref: String, prediction: String, happened: String, seconds: Int, usd: Double, calls: Int,
                    surprises: [String], next: String)
        switch goal {
        case "Draft the cache change":
            story = ("chat, Build speed", "The change fits in one config file",
                     "Wrote build-cache.md with the config change, the measured gain and how to roll it back.",
                     540, 0.07, 6, [], "Measure again after tonight's builds")
        case "Write this week's field notes":
            story = ("weekly review", "At least one habit is worth keeping",
                     "Read this week's cycle records and wrote field-notes.md: three things that worked, two to try next.",
                     780, 0.11, 9, [], "Measure the digest's open rate")
        default:
            story = ("chat, Build speed", "Caching alone halves the build time",
                     "Ran the benchmark five times per change, kept the medians and wrote up the comparison.",
                     1260, 0.18, 14,
                     ["expected: splitting the suite helps most; observed: caching did; so what: cache first"],
                     "Draft the cache change")
        }
        return ["id": id, "at": stamp(at), "kind": "cycle", "content": NSNull(),
                "planned": ["goal": goal, "success_criterion": "A measured result the operator can act on",
                            "origin": "operator", "origin_ref": story.ref, "confidence": 0.7,
                            "predictions": [story.prediction],
                            "attention": ["source: build logs; why: the median, not one lucky run"]],
                "did": ["what_happened": story.happened, "duration_s": story.seconds, "usd": story.usd,
                        "tool_calls": story.calls, "ended_by": "answer", "wrapped_up": NSNull(),
                        "delivered_via": "message"],
                "result": ["claim": "yes", "surprises": story.surprises, "doubts": [], "judge_error": "",
                           "judge": ["verdict": "yes", "claim_supported": true, "first_error": NSNull(),
                                     "reasoning": "The work is measured, written down and states its next step.",
                                     "model": "judge", "error": ""],
                           "second": NSNull()],
                "left": ["unfinished": [], "next_action": ["action": story.next, "revise_if": NSNull()],
                         "next_goal": story.next]]
    }
}
