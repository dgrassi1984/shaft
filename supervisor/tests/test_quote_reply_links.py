"""#14: conversation quote-replies were never linked."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import core, feedback, metrics, outcomes, pushes

NOW = datetime(2026, 11, 25, 12)
SENT = NOW - timedelta(days=3)
BODY = "Should I rewrite the parser with a simpler grammar?"


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def message(file="offer.md", text=BODY, at=SENT, **fields):
    return {"file": file, "from": "m", "to": "operator", "ts": at.isoformat(), "text": text, **fields}


def turn(content, at=SENT + timedelta(hours=1), **fields):
    return {"kind": "talk", "role": "user", "ts": at.isoformat(), "talk_id": "chat-1",
            "via": "app", "content": content, **fields}


class QuoteReplyLinksTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2])))
        self.sup = root / "supervisor"
        roots = root / "agents"
        self.episodes = roots / "m" / "memory" / "episodes" / "2026-11-22.jsonl"
        self.enterContext(mock.patch.object(core, "SUP_DIR", self.sup))
        self.enterContext(mock.patch.object(core, "INSTANCES", roots))
        self.enterContext(mock.patch.object(metrics, "INSTANCES", roots))
        self.enterContext(mock.patch.object(feedback, "FILE", self.sup / "feedback.jsonl"))

    def test_links_conversation_quote(self):
        """#14: conversation quote-replies were never linked."""
        write_rows(self.sup / "messages.jsonl", [message()])
        reply = turn("  > Should I rewrite the parser\n\t> with a simpler grammar?\n\nYes.")
        write_rows(self.episodes, [reply])

        row, = outcomes.records("m", NOW)
        assert row["outcome"] != "no_link"
        assert row["outcome"] == "followed_up"
        assert row["evidence"] == f"quote:talk:chat-1@{reply['ts']}"

    def test_links_quote_copied_without_markdown(self):
        """#14: conversation quote-replies were never linked."""
        write_rows(self.sup / "messages.jsonl", [message(text="**Bold** text and [a link](https://x)")])
        write_rows(self.episodes, [turn("> Bold text and a link")])

        row, = outcomes.records("m", NOW)
        assert row["outcome"] == "followed_up"

    def test_links_truncated_quote(self):
        """#14: conversation quote-replies were never linked."""
        write_rows(self.sup / "messages.jsonl", [message()])
        for ellipsis in ("…", "..."):
            write_rows(self.episodes, [turn("> Should I rewrite the parser" + ellipsis)])
            row, = outcomes.records("m", NOW)
            assert row["outcome"] == "followed_up"

    def test_ambiguous_quote_links_neither_delivery(self):
        """#14: conversation quote-replies were never linked."""
        write_rows(self.sup / "messages.jsonl", [message(), message(file="another.md")])
        write_rows(self.episodes, [turn("> " + BODY)])

        assert [row["outcome"] for row in outcomes.records("m", NOW)] == ["no_link", "no_link"]

    def test_quote_of_chat_answer_stays_unlinked(self):
        """#14: conversation quote-replies were never linked."""
        answer = "In chat, I suggested leaving it alone."
        write_rows(self.sup / "messages.jsonl", [message()])
        write_rows(self.episodes, [turn(answer, role="assistant"), turn("> " + answer)])

        row, = outcomes.records("m", NOW)
        assert row["outcome"] == "no_link"

    def test_earliest_quote_wins_across_sources(self):
        """#14: conversation quote-replies were never linked."""
        text = "> " + BODY
        reply = turn(text)
        write_rows(self.sup / "messages.jsonl", [
            message(),
            message("reply.md", text, SENT + timedelta(hours=2), **{"from": "operator", "to": "m"}),
        ])
        write_rows(self.episodes, [reply])
        row, = outcomes.records("m", NOW)
        assert row["evidence"] == f"quote:talk:chat-1@{reply['ts']}"

        write_rows(self.episodes, [turn(text, SENT + timedelta(hours=3))])
        row, = outcomes.records("m", NOW)
        assert row["evidence"] == "quote:message:reply.md"

    def test_weekly_offers_count_only_judged_self_origin_cycles(self):
        """#14: conversation quote-replies were never linked."""
        self_end = SENT + timedelta(minutes=5)
        operator_end = SENT + timedelta(hours=8)
        write_rows(self.sup / "messages.jsonl", [
            message(),
            message("unanswered.md", "Should I clarify the grammar notes?", self_end),
            message("requested.md", "Here are the edits you asked for.", operator_end),
            message("bad text.md", None, operator_end),
        ])
        write_rows(self.episodes, [
            {"kind": "live", "run_started": SENT.isoformat(), "ts": self_end.isoformat()},
            {"kind": "live", "run_started": operator_end.isoformat(), "ts": operator_end.isoformat()},
            {"kind": "live", "run_started": [], "ts": self_end.isoformat()},
            {"kind": "live", "run_started": SENT.isoformat(), "ts": {}},
            turn("> " + BODY),
            {**turn("> " + BODY), "ts": []},
        ])
        assert outcomes.offer_lines("m", NOW) == []
        judgments = self.sup / "judgments" / "m.jsonl"
        write_rows(judgments, [
            {"episode_ts": self_end.isoformat(), "origin": "self"},
            {"episode_ts": operator_end.isoformat(), "origin": "operator"},
        ])

        assert outcomes.offer_lines("m", NOW, weeks=2) == [
            "unprompted offers, week to 11-25 (self-origin cycles): 2 — replied by quote 1, other link (reply, thumb or comment) 0, no reply 1, open 0",
            "unprompted offers, week to 11-18 (self-origin cycles): 0 — replied by quote 0, other link (reply, thumb or comment) 0, no reply 0, open 0",
        ]

    def test_push_review_survives_offer_read_failure(self):
        """#14: conversation quote-replies were never linked."""
        def fail(*args):
            raise OSError("bad store")

        self.enterContext(mock.patch.object(outcomes, "offer_lines", fail))
        lines = pushes.lines("m", [], NOW)
        assert "Unprompted offers could not be read: OSError: bad store" in lines
        assert any(line.startswith("pushes to the operator today: 0") for line in lines)

    def test_guest_quote_is_not_an_operator_reply(self):
        """#14 review: a guest's turn (`speaker` set) quoting a delivery is not the operator's reply."""
        write_rows(self.sup / "messages.jsonl", [message()])
        write_rows(self.episodes, [turn("> " + BODY, speaker="Guest")])
        row, = outcomes.records("m", NOW)
        assert row["outcome"] == "no_link"
