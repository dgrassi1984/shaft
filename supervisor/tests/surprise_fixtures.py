"""The contract between the two copies of the surprise rules (docs/plans/PLAN-surprise.md §1.2, §2).

The template's copy (template/shaft/surprises.py: the in-the-moment note and the open queue) and the supervisor's
(supervisor/surprise.py: measurement) are written apart, because the supervisor never imports agent code and the
agent cannot alter the copy that measures it. Both are tested against these fixtures, so they cannot drift:
test_surprise_rules.py checks the supervisor's, test_surprise.py the template's.
"""
from __future__ import annotations

# (result, expected (flag, detail) or None). The result strings are copied from real episodes (2026-10-12/13).
FLAG_FIXTURES = [
    ("ERROR: PermissionError: self/self.md: use update_self_model", ("error", "PermissionError")),
    ("ERROR: PermissionError: memory/journal.md: the journal is append-only; it is written at the end of each cycle",
     ("error", "PermissionError")),
    ("ERROR: TypeError: call() got multiple values for argument 'name'", ("error", "TypeError")),
    ("ERROR: BrokerError: ValueError: commit must be a commit id (hex) from the log", ("error", "ValueError")),
    ("ERROR: this trait cites no journal entry: '- I ground my planning in reading'", ("error", "")),
    # wren 08:58: the heredoc never applied, the test then passed on unchanged code — exit 0 hides it
    ("exit 0 · 0.1 s\n--- stdout ---\nexit=0\n\n--- stderr ---\nzsh:1: can't create temp file for here document: "
     "operation not permitted\ntest_add_due_done_cycle ... ok", ("stderr_on_success", "not permitted")),
    ("exit 0 · 0.1 s --- stdout --- === ps -ax (count) === 0 --- stderr --- zsh:1: operation not permitted: ps",
     ("stderr_on_success", "not permitted")),
    ("exit 1 · 0.0 s --- stdout --- ls: /Users/example/Development/shaft/instances/: Operation not permitted --- ",
     ("nonzero_exit", "not permitted")),
    ("exit 1 · 0.0 s --- stdout --- --- stderr --- Traceback (most recent call last):", ("nonzero_exit", "traceback")),
    # exit 0 with "Traceback" in stdout only: a file that contains the word, not a failure
    ("exit 0 · 0.0 s --- stdout --- Traceback handling lives in llm.py", None),
    ("exit 0 · 0.2 s --- stdout --- ok   task_queue ok", None),
    ("(empty)", ("empty", "")),
    ("nothing relevant found", ("empty", "")),
    ("(repeat: not run)", ("refused", "")),
    ("HTTP 404 Content-Type: application/json", ("http_error", "404")),
    ("HTTP 200 Content-Length: 19538 Content-Type: application/json", None),
    ("text\n[... truncated, 9000 chars total]", ("truncated", "")),
    ("wrote 3581 chars to notes/ai-misalignment-trend.md", None),
    ("delivered to wren", None),
]


def _live(ts, surprises=(), resolves=(), origin_ref=""):
    return {"ts": ts, "kind": "live",
            "intent": {"goal": f"goal at {ts}", "success_criterion": "c", "confidence": 50, "origin": "self",
                       "origin_ref": origin_ref, "predictions": []},
            "finish": {"what_happened": "", "criterion_met": "partial", "learned": "",
                       "surprises": list(surprises), "resolves": list(resolves)},
            "ended_by": "control", "steps": 3, "tools": []}


def _s(expected, observed, so_what=""):
    return {"expected": expected, "observed": observed, "so_what": so_what}


# The open-surprise fold (PLAN-surprise §2). Episodes in file order; ids are s<MMDD>-<HHMM>-<n> from the episode's ts,
# n counting every id already issued with that prefix. Per live episode: age expiry first (open for more than 7 days
# at this episode's time), then its resolves, then its surprises, then the cap (more than 8 open, visible ones: the
# oldest expires). Depth = 1 + the depth of the surprise the goal came from (origin_ref surprise:<id>); depth 2 or
# more is never visible in the queue and never counts toward the cap. A sleep episode's closed[] closes open ones.
# Malformed entries are skipped and issue no id; a resolve or close of an unknown or no longer open id does nothing.
FOLD_EPISODES = [
    _live("2026-10-01T08:00:00", [_s("the page lists 3 sources", "it lists 5")]),            # expires by age
    _live("2026-10-10T09:00:05", [_s("A", "B", "C"), "not a dict", {}, _s("D", "E")]),       # -1, -2 (2 skipped)
    _live("2026-10-11T10:00:00", [_s("F", "G")],
          [{"id": "s1010-0900-1", "how": "found X"}, {"id": "s9999-0000-1", "how": "unknown"}],
          origin_ref="surprise:s1010-0900-1"),                                                # resolves -1; opens depth 2
    {"ts": "2026-10-12T03:00:00", "kind": "sleep", "ended_by": "answer", "tools": [],
     "closed": [{"id": "s1010-0900-2", "outcome": "not_worth_pursuing", "note": "a one-off"},
                {"id": "s1010-0900-1", "outcome": "answered", "note": "already resolved: no effect"}]},
    _live("2026-10-12T09:00:00", [_s(f"x{i}", f"y{i}") for i in range(1, 10)]),             # 9 new: the oldest of
    _live("2026-10-12T09:00:40", [_s("x10", "y10")]),                                        # them expires; then the next
]
FOLD_NOW = "2026-10-13T12:00:00"
# id -> (status, depth, reason): status open | resolved | closed | expired; reason only for expired (age | cap)
FOLD_EXPECTED = {
    "s1001-0800-1": ("expired", 1, "age"),
    "s1010-0900-1": ("resolved", 1, None),
    "s1010-0900-2": ("closed", 1, None),
    "s1011-1000-1": ("open", 2, None),
    "s1012-0900-1": ("expired", 1, "cap"),
    "s1012-0900-2": ("expired", 1, "cap"),
    **{f"s1012-0900-{i}": ("open", 1, None) for i in range(3, 11)},
}
# What the queue shows (open, visible, oldest first): depth-2 s1011-1000-1 is hidden
FOLD_QUEUE = [f"s1012-0900-{i}" for i in range(3, 11)]
# Ten days later every one of them has aged out
FOLD_LATER_NOW = "2026-10-23T12:00:00"
