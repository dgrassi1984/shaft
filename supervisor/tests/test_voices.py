"""Voice assignments and proposals through the store and broker entry points."""
import io
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

from supervisor import broker, core, messages, voices


def voice(identifier, language="it-IT"):
    return {"identifier": identifier, "name": identifier.title(), "language": language, "quality": "premium"}


class VoicesTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.sup = self.root / "state"
        self.enterContext(mock.patch.object(core, "SUP_DIR", self.sup))
        self.enterContext(mock.patch.object(messages, "SUP_DIR", self.sup))
        self.enterContext(mock.patch.object(messages, "instance_root", return_value=self.root / "agent"))
        self.enterContext(mock.patch("supervisor.autonomy.event_wake"))
        voices.report("ios", [voice("first"), voice("second"), voice("english", "en-US")])

    def broker(self, replay=False):
        if replay:
            return broker.ReplayBroker("example", self.root, {}, self.sup / "broker.log",
                                       state_root=self.root / "agent", start_commit="test", as_of=datetime.now(),
                                       cache={}, cache_lock=threading.Lock(), billed="test",
                                       tools_log=self.sup / "tools.log")
        return broker.Broker("example", self.root, {}, self.sup / "broker.log", root=self.root / "agent")

    def test_assignment_exclusivity_is_per_language(self):
        """Guard against two instances sharing one voice in the same language."""
        voices.assign("example", "it", "first", "operator")
        for operation in (lambda: voices.assign("other", "it", "first", "operator"),
                          lambda: voices.propose("other", "it", "first", "reason")):
            with self.assertRaisesRegex(voices.Refused, "taken by instance example for language it"):
                operation()
        voices.clear("example", "it")
        self.assertEqual(voices.get("example"), {"en": None, "it": None})
        self.assertEqual(voices.assign("other", "it", "first", "operator")["by"], "operator")

    def test_voice_must_speak_the_language_it_is_assigned_to(self):
        """Bug (integration review 2026-11-09): an Italian voice could be assigned or proposed as the English one, so
        English replies would be read by an Italian speaker. A voice speaks only the language its device reported."""
        for operation in (lambda: voices.assign("example", "en", "first", "operator"),
                          lambda: voices.propose("example", "en", "first", "why"),
                          lambda: voices.assign("example", "it", "english", "operator")):
            with self.assertRaisesRegex(voices.Refused, "speaks .*, not "):
                operation()
        self.assertEqual(voices.get("example"), {"en": None, "it": None})
        self.assertEqual(voices.assign("example", "en", "english", "operator")["identifier"], "english")

    def test_unknown_inputs_and_replacement(self):
        """Guard against invented identifiers and duplicate open proposals for a language."""
        for operation in (lambda: voices.propose("example", "it", "missing", "why"),
                          lambda: voices.assign("example", "it", "missing", "operator")):
            with self.assertRaisesRegex(voices.Refused, "identifier 'missing'.*not in any reported"):
                operation()
        with self.assertRaisesRegex(voices.Refused, "unknown language 'fr'"):
            voices.propose("example", "fr", "first", "why")
        old = voices.propose("example", "it", "first", "why")
        english = voices.propose("example", "en", "english", "why")
        new = voices.propose("example", "it", "second", "clearer")
        self.assertEqual(voices.proposals("example"), [english, new])
        self.assertNotEqual(old["id"], new["id"])
        self.assertEqual(new["state"], "open")

    def test_decisions_deliver_inbox_and_preserve_rejected_assignment(self):
        """Guard against silent decisions, wrong authorship, and rejection changing the voice."""
        proposal = voices.propose("example", "it", "first", "why")
        self.assertEqual(voices.decide("example", proposal["id"], True)["state"], "accepted")
        assigned = voices.get("example")["it"]
        self.assertEqual((assigned["identifier"], assigned["by"]), ("first", "instance"))
        proposal = voices.propose("example", "it", "second", "why")
        self.assertEqual(voices.decide("example", proposal["id"], False, "too similar")["state"], "rejected")
        self.assertEqual(voices.get("example")["it"], assigned)
        inbox = sorted((self.root / "agent/inbox").glob("*.md"))
        self.assertEqual(len(inbox), 2)
        self.assertIn("accepted: First (first) for it", inbox[0].read_text())
        self.assertIn("rejected: Second (second) for it.\ntoo similar", inbox[1].read_text())
        with self.assertRaisesRegex(voices.Refused, "already rejected"):
            voices.decide("example", proposal["id"], True)

    def test_accept_rechecks_current_ownership(self):
        """Guard against a pending approval stealing a voice assigned since proposal creation."""
        proposal = voices.propose("example", "it", "first", "why")
        voices.assign("other", "it", "first", "operator")
        with self.assertRaisesRegex(voices.Refused, "taken by instance other for language it"):
            voices.decide("example", proposal["id"], True)
        self.assertIsNone(voices.get("example")["it"])
        self.assertEqual(voices.proposals("example")[0]["state"], "open")
        self.assertFalse((self.root / "agent/inbox").exists())

    def test_platform_replacement_and_language_prefix(self):
        """Guard against one device erasing another's report or excluding regional voices."""
        ios = voices.available()["ios"]
        voices.report("macos", [voice("old")])
        replacement = [voice("swiss", "it-CH"), voice("italian"), voice("british", "en-GB")]
        voices.report("macos", replacement)
        self.assertEqual(voices.available(), {"ios": ios, "macos": replacement})
        self.assertEqual(voices.available("it"), {"ios": ios[:2], "macos": replacement[:2]})
        stored = json.loads((self.sup / "voices.json").read_text())
        self.assertIsNotNone(datetime.fromisoformat(stored["platforms"]["macos"]["reported"]))
        for platform, rows, error in [("watch", [], "platform"), ("ios", [{}], "identifier"),
                                      ("ios", [dict(voice("bad"), quality="bad")], "quality")]:
            with self.assertRaisesRegex(voices.Refused, error):
                voices.report(platform, rows)
        self.assertEqual(voices.available()["ios"], ios)

    def test_concurrent_assignments_keep_both_updates(self):
        """Guard against whole-file writers losing another instance's assignment."""
        start = threading.Barrier(2)
        save = voices._save

        def slow_save(data):
            time.sleep(0.02)  # expose a read/write race if the transaction lock is removed
            save(data)

        def assign(pair):
            start.wait(timeout=2)
            return voices.assign(pair[0], "it", pair[1], "operator")

        with mock.patch.object(voices, "_save", side_effect=slow_save), ThreadPoolExecutor(2) as pool:
            list(pool.map(assign, [("example", "first"), ("other", "second")]))
        self.assertEqual(voices.get("example")["it"]["identifier"], "first")
        self.assertEqual(voices.get("other")["it"]["identifier"], "second")

    def test_broker_dispatch_counts_and_marks_ownership(self):
        """Guard against unregistered tools, missing call stats, and offering taken voices as free."""
        voices.assign("other", "it", "first", "operator")
        voices.assign("example", "it", "second", "operator")
        b = self.broker()
        result = b.call("list_voices", {"language": "it"})
        self.assertTrue(result["ok"], result)
        rows = json.loads(result["result"])["ios"]
        self.assertEqual([(r["current"], r["taken_by"]) for r in rows], [(False, "other"), (True, None)])
        result = b.call("propose_voice", {"language": "en", "identifier": "english", "why": "clear"})
        self.assertTrue(result["ok"], result)
        answer = json.loads(result["result"])
        self.assertIn("Settings", answer["message"])
        self.assertEqual(voices.proposals("example"), [answer["proposal"]])
        self.assertEqual(b.counts, {"list_voices": 1, "propose_voice": 1})
        self.assertEqual(b.calls, 2)
        with mock.patch("supervisor.telegram.reachable", return_value=""), \
                mock.patch.object(b, "_orders_line", return_value=""):
            offered = {s["function"]["name"] for s in b.tool_specs()}
        self.assertTrue({"list_voices", "propose_voice"} <= offered)

    def test_cli_switches_refuse_and_hide_tools(self):
        """Guard against voice tools bypassing the operator's shaft tools off switches."""
        from supervisor import __main__ as cli
        with mock.patch.object(cli, "instance_root", return_value=self.root / "agent"), \
                mock.patch.object(cli, "log"), redirect_stdout(io.StringIO()) as output:
            for tool in ("list_voices", "propose_voice"):
                self.assertEqual(cli.cmd_tools("example", "off", tool), 0)
        self.assertIn("list_voices", output.getvalue())
        self.assertIn("propose_voice", output.getvalue())
        b = self.broker()
        for tool, args in [("list_voices", {}),
                           ("propose_voice", {"language": "it", "identifier": "first", "why": "clear"})]:
            result = b.call(tool, args)
            self.assertFalse(result["ok"])
            self.assertIn("switched it off", result["error"])
        self.assertEqual(voices.proposals("example"), [])
        with mock.patch("supervisor.telegram.reachable", return_value=""), \
                mock.patch.object(b, "_orders_line", return_value=""):
            offered = {s["function"]["name"] for s in b.tool_specs()}
        self.assertFalse({"list_voices", "propose_voice"} & offered)

    def test_replay_records_simulation_without_writing_proposal(self):
        """Guard against a trial replay creating a real voice proposal."""
        b = self.broker(replay=True)
        before = (self.sup / "voices.json").read_bytes()
        result = b.call("propose_voice", {"language": "it", "identifier": "first", "why": "clear"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(json.loads(result["result"])["proposal"]["state"], "open")
        self.assertEqual((self.sup / "voices.json").read_bytes(), before)
        self.assertIn("simulated: not proposed", b.trace[-1]["tags"])
        refused = b.call("propose_voice", {"language": "it", "identifier": "missing", "why": "clear"})
        self.assertFalse(refused["ok"])
        self.assertIn("missing", refused["error"])


if __name__ == "__main__":
    unittest.main()
