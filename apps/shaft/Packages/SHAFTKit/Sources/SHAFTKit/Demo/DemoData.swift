import Foundation

/// Fictional response bodies in the app API's wire format. Dates are relative to the start of this run.
enum DemoData {
    static let reply = "This is the demo: replies here are canned. Paired with your own SHAFT server, the instance answers for real."
    static let markdown = """
    ## A little room to think

    Here is a sample plan for a quieter week.

    - Leave space for focused work.
    - Collect loose ideas in one place.
    - Review what changed before choosing the next step.

    | Part | Next step |
    | --- | --- |
    | Explore | Gather three ideas |
    | Make | Try one small draft |
    | Reflect | Keep what helped |

    ```python
    ideas = ["explore", "make", "reflect"]
    for idea in ideas:
        print(idea)
    ```
    """
    static let document = """
    # Field notes

    A sample document, kept entirely on this device during the demo.

    ## A useful rhythm

    1. Notice an idea.
    2. Give it a small experiment.
    3. Write down what changed.

    > Small steps leave room for discovery.
    """

    static let documentSHA = "05a84536793c9e03b569208c89cab522cc3d64e0f0c3558dfee6c75f7006e1a9"

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
         "kind": "question", "read": false, "text": "Which summary format should I use for the weekly digest?",
         "attachments": [], "choices": ["multiple": false, "options":
            ["Bullet points", "Short paragraph", "Table"].map { ["label": $0] }],
         "answer": NSNull(), "open": true, "from_cycle": false, "re": NSNull(), "rating": NSNull()]
    }

    static func feed(_ name: String, cid: String, title: String, at: Date) -> [String: Any] {
        let items: [[String: Any]]
        if name == "wisp" {
            items = [question(at: at.addingTimeInterval(-180))]
        } else if name == "tundra" {
            items = [user("Help me sketch a quieter week.", k: 0, mid: "demo-0", at: at.addingTimeInterval(-900)),
                     assistant(markdown, k: 1, at: at.addingTimeInterval(-840)),
                     user("Keep it small enough to try tomorrow.", k: 2, mid: "demo-2", at: at.addingTimeInterval(-600)),
                     assistant("Start with one idea and a twenty-minute draft. We can review what you learn afterward.",
                               k: 3, at: at.addingTimeInterval(-540))]
        } else {
            items = [user("Let's collect a few ideas here.", k: 0, mid: "demo-0", at: at.addingTimeInterval(-700)),
                     assistant(cid == "ada-notes" ? "The field notes are ready in Documents." : "A blank page, a short walk, a new perspective. Which shall we explore?",
                               k: 1, at: at.addingTimeInterval(-650))]
        }
        return ["instance": name, "partial": false, "version": "1", "live": "", "reply": NSNull(),
                "chat": ["id": cid, "title": title, "state": "idle", "created": stamp(at.addingTimeInterval(-1200)), "ended": NSNull()],
                "archived": false, "earlier": 0, "items": items, "queued": [], "pending_feedback": [],
                "pending_edits": [], "mids": items.compactMap { $0["mid"] as? String }, "last": stamp(at.addingTimeInterval(-180)),
                "can_stop": false, "can_end": true, "can_clear": true]
    }

    static func plan() -> [String: Any] {
        ["now": ["state": "idle", "since": NSNull(), "goal": NSNull()],
         "check": ["next": "", "skipped": "Sample schedule", "paused": false],
         "unfinished": ["Collect the next set of ideas"], "commitments": [], "commitments_error": "",
         "queue": ["state": "rows", "open": 0, "overdue": 0, "error": "", "rows": []],
         "projects": ["open": 0, "rows": [], "error": ""], "goals": "Explore, draft, reflect.",
         "goals_error": "", "improvement_error": ""]
    }

    static func cycle(_ id: String, at: Date, goal: String) -> [String: Any] {
        ["id": id, "at": stamp(at), "kind": "cycle",
         "planned": ["goal": goal, "success_criterion": "A clear sample note", "origin": "operator",
                     "predictions": ["One small next step will emerge"], "attention": []],
         "did": ["what_happened": "Prepared a sample note and reviewed the next step.", "duration_s": 42,
                 "tool_calls": 2, "delivered_via": "message"],
         "result": ["claim": "yes", "surprises": [], "doubts": [], "judge_error": "",
                    "judge": ["verdict": "yes", "reasoning": "The sample note includes a clear next step.", "error": ""]],
         "left": ["unfinished": [], "next_goal": "Review the next idea"]]
    }
}
