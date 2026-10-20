"""`shaft top`'s one-line recap per instance: what it is doing now and what it did in the last hour, written on
demand by a small model (supervisor/recap.py; the operator, 2026-10-16)."""
from __future__ import annotations

import io
import json
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import cloudllm, panel, recap

try:
    from rich.console import Console

    from supervisor import top
except ModuleNotFoundError:   # rich lives in .venv-ui
    top = None

NOW = time.time()


def _at(minutes_ago: float, sep: str = "T") -> str:
    return (datetime.fromtimestamp(NOW) - timedelta(minutes=minutes_ago)).strftime(f"%Y-%m-%d{sep}%H:%M:%S")


def _inst(name="wren", **kw) -> dict:
    return {"name": name, "state": "in a cycle", "since": "14:35", "doing": "thinking (step 2) · 4s",
            "goal": "Prepare the planning task brief", **kw}


def _reply(now: str, hour: str, stop: str = "end_turn", usage=None):
    body = {"model": recap.MODEL, "stop_reason": stop, "usage": usage or {"input_tokens": 1000, "output_tokens": 50},
            "content": [{"type": "thinking", "thinking": ""},
                        {"type": "text", "text": json.dumps({"now": now, "last_hour": hour})}]}
    resp = mock.MagicMock()
    resp.__enter__.return_value = io.BytesIO(json.dumps(body).encode())
    return resp


class RecapCase(unittest.TestCase):
    def setUp(self):
        self.sup = Path(tempfile.mkdtemp(prefix="shaft-recap-")).resolve()
        self.addCleanup(shutil.rmtree, self.sup, True)
        self.transcripts, self.history = [], []
        for p in (mock.patch.object(recap, "SUP_DIR", self.sup),
                  mock.patch.object(recap, "STORE", self.sup / "recaps.json"),
                  mock.patch.object(recap, "USAGE", self.sup / "recap_usage.jsonl"),
                  mock.patch.object(recap, "secrets_env", return_value={"ANTHROPIC_API_KEY": "k"}),
                  mock.patch.object(panel, "transcripts", lambda name: self.transcripts),
                  mock.patch("supervisor.messages.history", lambda: self.history)):
            p.start()
            self.addCleanup(p.stop)


class Context(RecapCase):
    def test_reads_the_last_hour_oldest_first_and_nothing_older(self):
        self.transcripts = [   # newest first, as panel.transcripts returns them
            {"kind": "cycle", "ts": _at(10), "record": {
                "intent": {"goal": "Post the TOC on sample-project #42"}, "ended_by": "control",
                "tools": [{"tool": "run"}, {"tool": "run"}, {"tool": "github_app"}],
                "finish": {"criterion_met": "yes", "what_happened": "Posted the comment"}}},
            {"kind": "conversation", "ts": _at(80), "via": "telegram", "turns": [
                {"ts": _at(80), "role": "user", "content": "too old to show"},
                {"ts": _at(30), "role": "user", "speaker": "the operator", "content": "do not work on sample-project"}]},
            {"kind": "cycle", "ts": _at(90), "record": {"intent": {"goal": "an old cycle"}, "finish": {}}}]
        self.history = [{"ts": _at(5), "from": "wren", "to": "operator", "text": "planning brief ready", "kind": "note"},
                        {"ts": _at(5), "from": "shaft", "to": "kant", "text": "not wren's", "kind": "note"},
                        {"ts": _at(70), "from": "wren", "to": "operator", "text": "too old", "kind": "note"}]
        c = recap.context(_inst(), NOW)
        self.assertIn("State now: in a cycle since 14:35", c)
        self.assertIn("Current cycle goal: Prepare the planning task brief", c)
        self.assertIn("the operator: do not work on sample-project", c)
        self.assertIn("tools run×2, github_app", c)
        self.assertIn("criterion met: yes", c)
        self.assertIn("wren → operator: planning brief ready", c)
        for gone in ("too old to show", "an old cycle", "not wren's", "too old"):
            self.assertNotIn(gone, c)
        # oldest first, and each entry's details under its own header (2026-10-16: a whole-list reversal put
        # "what happened" above the cycle it belonged to)
        self.assertLess(c.index("conversation via telegram"), c.index("the operator: do not work"))
        self.assertLess(c.index("the operator: do not work"), c.index("cycle ended"))
        self.assertLess(c.index("cycle ended"), c.index("what happened: Posted the comment"))

    def test_broker_calls_cover_a_running_cycle_without_model_calls_or_other_instances(self):
        (self.sup / "broker.log").write_text("\n".join([
            "[header line, cut by the tail read]",
            f"[{_at(90, ' ')}] wren calendar_events {{}} -> ok (5 ms)",
            f"[{_at(3, ' ')}] wren calendar_events {{\"query\": \"planning\"}} -> ok (700 ms)",
            f"[{_at(3, ' ')}] wren llm openrouter/xiaomi/mimo -> tool_calls (in 4000, out 100)",
            f"[{_at(2, ' ')}] wren@trial:t-1:1A web_search {{}} -> ok (5 ms)",
            f"[{_at(2, ' ')}] shaft fetch_page {{}} -> ok (5 ms)"]) + "\n")
        lines = recap._broker("wren", _at(60))
        self.assertEqual(len(lines), 1)
        self.assertIn('calendar_events {"query": "planning"} -> ok', lines[0])

    def test_credentials_in_logged_calls_never_reach_the_model(self):
        """2026-10-16: wren's http_request lines carry the start of GitHub bearer tokens and JWTs."""
        (self.sup / "broker.log").write_text(
            "[x]\n"
            f'[{_at(3, " ")}] wren http_request {{"url": "https://api.github.com/x", "headers": {{"Authorization": '
            f'"Bearer ghs_test_only -> ok (300 ms)\n'
            f'[{_at(2, " ")}] wren http_request {{"api_key": "sk-FAKE000000000000", "note": "eyJFAKEFAKEFAKEFAKE.x"}} '
            f"-> ok (3 ms)\n")
        c = recap.context(_inst(), NOW)
        for secret in ("ghs_test_only", "eyJFAKE", "sk-FAKE000000000000", "Bearer ghs"):
            self.assertNotIn(secret, c)
        self.assertIn("[redacted]", c)

    def test_a_long_record_keeps_the_head_and_the_newest_lines(self):
        self.transcripts = [{"kind": "cycle", "ts": _at(m), "record": {
            "intent": {"goal": f"goal {m:02d} " + "x" * 280}, "finish": {"what_happened": "y" * 590}}}
            for m in range(1, 59)]
        c = recap.context(_inst(), NOW)
        self.assertLessEqual(len(c), recap.MAX_CONTEXT)
        self.assertIn("State now: in a cycle", c)
        self.assertIn("goal 01 ", c)                   # the newest cycle survives the cut
        self.assertNotIn("goal 58 ", c)


class Ask(RecapCase):
    def test_one_line_from_two_parts_and_its_cost_logged(self):
        with mock.patch("urllib.request.urlopen", return_value=_reply("Preparing the planning brief.", "Posted on sample-project")) as u:
            self.assertEqual(recap.ask("records"), "Preparing the planning brief · last hour: Posted on sample-project")
        body = json.loads(u.call_args[0][0].data)
        self.assertEqual(body["model"], recap.MODEL)
        self.assertEqual(body["output_config"]["format"]["schema"], recap.SCHEMA)
        rows = [json.loads(l) for l in (self.sup / "recap_usage.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["usd"], cloudllm.usd(recap.MODEL, {"input_tokens": 1000, "output_tokens": 50}))
        self.assertGreater(rows[0]["usd"], 0)

    def test_a_long_first_answer_is_shortened_once_never_cut(self):
        """2026-10-16: on a busy hour Haiku wrote 40-57 words against a 25-word instruction."""
        long_hour = " ".join(["word"] * 40)
        with mock.patch("urllib.request.urlopen", side_effect=[_reply("Busy", long_hour),
                                                               _reply("Busy", "Posted the TOC")]) as u:
            self.assertEqual(recap.ask("records"), "Busy · last hour: Posted the TOC")
        second = json.loads(u.call_args_list[1][0][0].data)
        self.assertEqual(second["system"], recap.SHORTEN)
        self.assertIn(long_hour, second["messages"][0]["content"])
        self.assertNotIn("records", second["messages"][0]["content"])   # the shortener sees only the draft

    def test_no_answer_yet_is_an_error_not_a_recap(self):
        with mock.patch("urllib.request.urlopen", return_value=_reply("half", "a sent", stop="max_tokens")):
            with self.assertRaisesRegex(recap.RecapError, "max_tokens"):
                recap.ask("records")
        self.assertEqual(json.loads((self.sup / "recap_usage.jsonl").read_text())["error"], "max_tokens")

    def test_a_missing_key_names_the_key(self):
        with mock.patch.object(recap, "secrets_env", return_value={}):
            with self.assertRaisesRegex(recap.RecapError, "ANTHROPIC_API_KEY"):
                recap.ask("records")


class Store(RecapCase):
    def test_a_failure_is_stored_with_its_reason_beside_the_previous_recap(self):
        with mock.patch("urllib.request.urlopen", return_value=_reply("Idle", "Nothing recorded")):
            first = recap.write(_inst("kant", state="idle"), NOW)
        self.assertEqual(first["text"], "Idle · last hour: Nothing recorded")
        with mock.patch("urllib.request.urlopen", side_effect=OSError("no route")):
            second = recap.write(_inst("kant", state="idle"), NOW + 60)
        self.assertEqual(second["text"], first["text"])
        self.assertIn("no route", second["error"])
        self.assertEqual(recap.load()["kant"], second)

    def test_start_writes_every_instance_in_the_background_once(self):
        done = []
        with mock.patch.object(recap, "write", side_effect=lambda inst: done.append(inst["name"]) or time.sleep(0.2)):
            said = recap.start([_inst("wren"), _inst("shaft")])
            self.assertTrue(recap.running("wren"))
            self.assertEqual(recap.start([_inst("wren")]), "a recap is already being written")
            for _ in range(50):
                if not (recap.running("wren") or recap.running("shaft")):
                    break
                time.sleep(0.05)
        self.assertIn("wren, shaft", said)
        self.assertEqual(sorted(done), ["shaft", "wren"])
        self.assertFalse(recap.running("wren"))


class Costs(RecapCase):
    def test_recap_spend_is_its_own_component_of_the_day(self):
        today = datetime.now().strftime("%Y-%m-%dT10:00:00")
        (self.sup / "recap_usage.jsonl").write_text(json.dumps({"ts": today, "usd": 0.0006}) + "\n")
        with mock.patch.object(panel, "SUP_DIR", self.sup), mock.patch.object(panel, "instance_names", lambda: []), \
                mock.patch("supervisor.trial.LEDGER", self.sup / "trial_usage.jsonl"), \
                mock.patch.object(cloudllm, "USAGE", self.sup / "cloud_usage.jsonl"):
            panel._memo.clear()
            t = panel.costs(days=1)["today"]
        self.assertAlmostEqual(t["recaps"], 0.0006)
        self.assertAlmostEqual(t["total"], 0.0006)


@unittest.skipIf(top is None, "needs rich")
class Panel(RecapCase):
    def _snap(self, rec=None):
        inst = {"name": "wren", "state": "in a cycle", "since": "14:35", "doing": "", "goal": "", "model": "m",
                "every": "10 min", "next_wake": "due", "conversations": 0, "calls_today": 0, "tokens_today": 0,
                "usd_today": 0.0, "judged": 0, "success": None, "achieved": None, "tools_off": [], "recap": rec}
        return {"at": "15:00:00", "services": [], "instances": [inst], "events": [], "messages": [], "alerts": []}

    def text(self, snap) -> str:
        c = Console(record=True, width=140, height=50, file=io.StringIO())
        c.print(top.render(snap, top.UI(), 140, 50))
        return c.export_text()

    def test_the_overview_shows_the_recap_and_how_old_it_is(self):
        # stamped now, not at import (NOW): a full run reaches this test minutes later
        five_ago = (datetime.now() - timedelta(minutes=5, seconds=10)).strftime("%Y-%m-%dT%H:%M:%S")
        out = self.text(self._snap({"ts": five_ago, "text": "Preparing the planning brief · last hour: Posted on sample-project",
                                    "running": False}))
        self.assertIn("recap  Preparing the planning brief · last hour: Posted on sample-project", out)
        self.assertIn("(5 min ago)", out)
        self.assertIsNone(top.recap_line({"running": False}))      # no row before the first `a`
        self.assertNotIn("recap  ", self.text(self._snap({"running": False})))

    def test_a_failed_attempt_shows_why_and_a_running_one_says_so(self):
        failed = self.text(self._snap({"ts": _at(30), "text": "Old line · last hour: x", "error": "RecapError: HTTP 529",
                                       "error_ts": _at(1), "running": False}))
        self.assertIn("Old line", failed)
        self.assertIn("recap failed", failed)
        self.assertIn("HTTP 529", failed)
        self.assertIn("writing a recap…", self.text(self._snap({"running": True})))

    def test_a_starts_the_recaps_and_extras_attaches_them(self):
        snap = self._snap()
        with mock.patch.object(recap, "start", return_value="recapping wren with claude-haiku-5-5…") as start:
            top.dispatch(top.UI(), "a", snap, 10, None)
        start.assert_called_once_with(snap["instances"])
        (self.sup / "recaps.json").write_text(json.dumps({"wren": {"ts": _at(1), "text": "t"}}))
        with mock.patch.object(panel, "costs", return_value={"today": {"total": 0.0}}), \
                mock.patch.object(panel, "alerts", return_value=[]), mock.patch.object(panel, "dismissed", return_value=set()):
            out = top.extras(self._snap())
        self.assertEqual(out["instances"][0]["recap"], {"ts": _at(1), "text": "t", "running": False})


if __name__ == "__main__":
    unittest.main()
