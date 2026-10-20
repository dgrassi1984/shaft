"""Issue #35: dates and full timestamps in every kind of chat turn."""
from __future__ import annotations

import re
import unittest
from unittest import mock

from supervisor import web_views


class ChatDatesTest(unittest.TestCase):
    def feed(self, turns, sent=None, **extra):
        chat = {"id": "web-1", "state": "open", "turns": turns, **extra}
        with mock.patch("time.strftime", return_value="2026-10-20"):
            out = str(web_views.chat_feed("ada", chat, sent or [], archived=True))
        return out

    def test_issue_35_all_turn_kinds_have_full_times_and_day_boundaries(self):
        """Reverting #35 hides dates for user, assistant, sent and running turns across midnight."""
        turns = [{"role": "user", "content": "hello", "ts": "2026-10-19T23:58:01"},
                 {"role": "assistant", "content": "answer", "ts": "2026-10-20T00:01:03"}]
        sent = [{"file": "sent.txt", "text": "a file", "ts": "2026-10-19T23:59:02"}]
        out = self.feed(turns, sent, state="replying", reply={"started": "2026-10-21T00:02:04"})
        self.assertEqual(re.findall(r'data-k="day-([^"]+)"', out),
                         ["2026-10-19", "2026-10-20", "2026-10-21"])
        self.assertIn('data-k="day-2026-10-20">Today</p>', out)
        for ts in [turns[0]["ts"], sent[0]["ts"], turns[1]["ts"], "2026-10-21T00:02:04"]:
            self.assertIn(f'<time datetime="{ts}" title="{ts.replace("T", " ")}">{ts[11:16]}</time>', out)
        keys = re.findall(r'data-k="([^"]+)"', out)
        self.assertEqual(keys, ["day-2026-10-19", "u0", "ssent.txt", "day-2026-10-20", "a1",
                                "day-2026-10-21", "live"])

    def test_issue_35_today_has_only_clock_text_and_no_repeated_separator(self):
        """Reverting #35 removes full timestamp metadata even for today's messages."""
        turns = [{"role": role, "content": "hello", "ts": "2026-10-20T12:34:56"}
                 for role in ("user", "assistant")]
        out = self.feed(turns)
        self.assertNotIn('class="dim chat-day"', out)
        self.assertEqual(out.count('<time datetime="2026-10-20T12:34:56" title="2026-10-20 12:34:56">12:34</time>'), 2)

    def test_issue_35_single_old_day_is_labelled_once_and_missing_live_time_is_safe(self):
        """Reverting #35 leaves a one-day archived chat indistinguishable from today's chat."""
        turns = [{"role": role, "content": "hello", "ts": "2026-10-19T12:34:56"}
                 for role in ("user", "assistant")]
        out = self.feed(turns, state="replying")
        self.assertEqual(out.count('data-k="day-2026-10-19"'), 1)
        self.assertNotIn('datetime=""', out)
        self.assertNotIn('data-k="day-"', out)


if __name__ == "__main__":
    unittest.main()
