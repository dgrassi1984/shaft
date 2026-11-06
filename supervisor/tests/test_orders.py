"""Review I2/I4: the operator's decisions stay decided — standing orders held by the supervisor, shown every run, and
checked on every message to the operator; a correction stays checked until 3 messages in a row pass it."""
from __future__ import annotations

import io
import json
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from unittest import mock

from supervisor import __main__ as cli
from supervisor import broker, core, judge, metrics, orders, pushes, web_actions, web_config
from supervisor.tests import test_m5_pushes as fixtures

NOW = fixtures.NOW


class OrdersTest(unittest.TestCase):
    def setUp(self):
        fixtures.PushTest.setUp(self)
        self.enterContext(mock.patch.object(orders, "CORRECTION_PASSES", 3))
        self.enterContext(mock.patch.object(metrics, "own_models", return_value=["openrouter/own/model"]))
        self.pick = self.enterContext(mock.patch.object(judge, "pick", return_value="openrouter/judge/x"))
        self.verdicts = []
        self.ask = self.enterContext(mock.patch.object(judge, "ask", side_effect=self._answer))
        self.b.root.mkdir(parents=True, exist_ok=True)
        self.b.talk_id, self.b.log_path, self.b.grants_allowed = None, self.sup / "broker.log", False

    def _answer(self, system, prompt, schema, **kw):
        self.prompts = getattr(self, "prompts", []) + [prompt]
        out = self.verdicts.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    def operator_message(self, text="Retire the paper.", at=NOW - timedelta(hours=1)):
        path = self.sup / f"{at:%Y%m%d-%H%M%S-%f}-from-operator.md"
        fixtures.messages.record(self.sup, path, "operator", "m", "note", at, text)
        return path.name

    def correction(self, cid="k1028-1100-1", when=NOW - timedelta(hours=3), closed=None):
        rows = [{"event": "recorded", "id": cid, "what_was_wrong": "re-asked about retiring",
                 "what_operator_said": "I said retire. Stop asking.", "when": when.isoformat(),
                 "conversation": "api-1"}]
        if closed:
            rows.append({"event": "closed", "id": cid, "closed_by": closed, "evidence": "fact 2026-10-28"})
        (self.b.root / "memory").mkdir(exist_ok=True)
        (self.b.root / "memory" / "corrections.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def send(self, text="Shall I continue the paper or retire it?"):
        return self.b.call("send_message", {"to": "operator", "text": text})

    def checks(self):
        return [json.loads(l) for l in orders.checks_path("m").read_text().splitlines()]

    # -- the ledger --------------------------------------------------------------------------------------------------
    def test_orders_are_append_only_numbered_deduplicated_and_dropped_by_id(self):
        first, new = orders.add("m", "  Retire the paper.  ", "operator", "cli", now=NOW)
        self.assertEqual((first["id"], first["text"], new), ("o1", "Retire the paper.", True))
        self.assertEqual(orders.add("m", "retire the paper.", "instance", "x", now=NOW)[1], False)
        orders.add("m", "The paper body has no process blocks.", "instance", "api-1", now=NOW)
        orders.drop("m", "o1", now=NOW + timedelta(hours=1))
        with self.assertRaisesRegex(ValueError, "already dropped"):
            orders.drop("m", "o1")
        with self.assertRaisesRegex(ValueError, "no order 'o9'"):
            orders.drop("m", "o9")
        self.assertEqual([o["id"] for o in orders.listing("m")], ["o2"])
        self.assertEqual([o["id"] for o in orders.listing("m", as_of=NOW + timedelta(minutes=30))], ["o1", "o2"])
        self.assertEqual(orders.listing("m", as_of=NOW - timedelta(minutes=1)), [])
        rows = [json.loads(l) for l in orders.path("m").read_text().splitlines()]
        self.assertEqual(len(rows), 3)   # the drop is a row of its own; nothing rewritten
        self.assertEqual(orders.add("m", "Retire the paper.", "operator", "cli")[0]["id"], "o3")   # ids never reused
        with self.assertRaisesRegex(ValueError, "needs its text"):
            orders.add("m", "   ", "operator", "cli")

    # -- record_order --------------------------------------------------------------------------------------------------
    def test_m8_missing_source_names_recent_operator_files_before_as_of(self):
        files = [self.operator_message(at=NOW - timedelta(minutes=i)) for i in range(1, 5)]
        future = self.operator_message(at=NOW + timedelta(minutes=1))
        path = self.b.root / "inbox" / files[0]
        path.parent.mkdir(exist_ok=True)
        path.write_text("From: operator\n\nRetire the paper.\n")
        self.b.as_of = NOW
        refused = self.b.call("record_order", {"text": "Retire the paper."})
        self.assertFalse(refused["ok"])
        for file in files[:3]:
            self.assertIn(file, refused["error"])
        self.assertNotIn(files[3], refused["error"])
        self.assertNotIn(future, refused["error"])
        self.assertIn("record(kind='runs')", refused["error"])
        self.assertTrue(self.b.call("record_order", {"text": "Retire the paper.", "source": files[0]})["ok"])

    def test_record_order_needs_a_real_source_and_never_drops(self):
        refused = self.b.call("record_order", {"text": "Retire.", "source": "made-up.md"})
        self.assertFalse(refused["ok"])
        self.assertIn("name the file of the operator's message", refused["error"])
        file = self.operator_message()
        ok = self.b.call("record_order", {"text": "Retire.", "source": f"inbox/{file}"})
        self.assertTrue(ok["ok"], ok)
        self.assertIn("recorded standing order o1", ok["result"])
        self.assertEqual((orders.listing("m")[0]["by"], orders.listing("m")[0]["source"]), ("instance", file))
        self.b.talk_id = "api-abc"
        self.assertTrue(self.b.call("record_order", {"text": "No process blocks."})["ok"])
        self.assertEqual(orders.listing("m")[1]["source"], "api-abc")
        self.assertNotIn("drop_order", broker.SPECS)

    def test_orders_shown_verbatim_in_the_tools_every_run_and_with_record_order_off(self):
        orders.add("m", "Retire the paper. Do not ask again.", "operator", "cli", now=NOW)
        self.correction(closed="fact")
        self.b.disabled = {"trial", "delegate_coding"}
        with mock.patch("supervisor.telegram.reachable", return_value=""), \
                mock.patch("supervisor.channel.route", return_value=("web", "test")):
            specs = {s["function"]["name"]: s["function"]["description"] for s in self.b.tool_specs()}
            self.assertIn("o1 (from the operator, 2026-10-28 12:00): Retire the paper. Do not ask again.",
                          specs["record_order"])
            self.assertIn("k1028-1100-1 (0 passed): I said retire. Stop asking.", specs["record_order"])
            self.assertNotIn("Retire the paper", specs["send_message"])
            self.b.disabled = {"record_order"}
            specs = {s["function"]["name"]: s["function"]["description"] for s in self.b.tool_specs()}
            self.assertNotIn("record_order", specs)
            self.assertIn("Retire the paper. Do not ask again.", specs["send_message"])

    # -- the check on send_message ---------------------------------------------------------------------------------
    def test_i2_message_against_an_order_is_refused_quoting_it_and_not_delivered(self):
        """Review I2: shaft re-asked "continue or retire?" four times after "Retire"."""
        orders.add("m", "Retire the paper.", "operator", "cli", now=NOW - timedelta(days=1))
        self.verdicts = [{"violates": True, "order_id": "o1", "reason": "it asks again whether to retire"}]
        result = self.send()
        self.assertFalse(result["ok"])
        self.assertIn('standing order o1 (from the operator, 2026-10-27 12:00): Retire the paper.', result["error"])
        self.assertIn("it asks again whether to retire", result["error"])
        self.assertIn("Rewrite it", result["error"])
        self.assertEqual(pushes.records(self.sup), [])
        self.assertFalse((self.sup / "operator-inbox").exists() and any((self.sup / "operator-inbox").iterdir()))
        self.assertEqual(self.checks()[0]["order_id"], "o1")
        self.pick.assert_called_with(["openrouter/own/model"])     # never the instance's own model
        self.assertIn("[o1] Retire the paper.", self.prompts[0])

    def test_message_keeping_to_orders_is_delivered_and_said_checked(self):
        orders.add("m", "Retire the paper.", "operator", "cli", now=NOW - timedelta(days=1))
        self.verdicts = [{"violates": False, "order_id": "", "reason": "it reports the retirement"}]
        result = self.send("The paper is retired; its folder is archived.")
        self.assertTrue(result["ok"], result)
        self.assertIn("checked against your standing orders: it keeps to them", result["result"])
        self.assertEqual(self.checks()[0]["violates"], False)
        self.assertEqual(self.checks()[0]["message"], pushes.records(self.sup)[0]["file"])

    def test_no_orders_no_judge_call(self):
        self.assertTrue(self.send()["ok"])
        self.ask.assert_not_called()
        self.assertFalse(orders.checks_path("m").exists())

    def test_judge_without_verdict_delivers_and_says_not_checked(self):
        orders.add("m", "Retire the paper.", "operator", "cli", now=NOW - timedelta(days=1))
        self.verdicts = [judge.JudgeError("no OPENROUTER_API_KEY")]
        result = self.send()
        self.assertTrue(result["ok"], result)
        self.assertIn("NOT checked against your standing orders", result["result"])
        self.assertIn("no OPENROUTER_API_KEY", self.checks()[0]["error"])
        self.verdicts = [{"violates": True, "order_id": "o7", "reason": "?"}]   # an id it was not shown
        self.assertIn("NOT checked", self.send()["result"])

    def test_codex_malformed_verdict_is_no_verdict_and_never_a_correction_pass(self):
        """Codex review: `{}` from the judge read as violates=False — "it keeps to them", and a pass on every correction."""
        self.correction(closed="fact")
        bad = [{}, {"violates": "false", "order_id": "", "reason": ""}, {"violates": False, "reason": ""},
               {"violates": False, "order_id": None, "reason": ""}, {"violates": False, "order_id": "", "reason": 3},
               ["violates", False]]
        self.verdicts = list(bad)
        for _ in bad:
            result = self.send("A status note.")
            self.assertTrue(result["ok"], result)
            self.assertIn("NOT checked against your standing orders", result["result"])
            self.assertIn("no verdict fitting the schema", result["result"])
        self.assertTrue(all(row.get("error") for row in self.checks()))
        self.assertEqual(orders.corrections("m", self.b.root)[0]["passes"], 0)    # the bug gave 3+: correction gone

    def test_slow_judge_does_not_outlast_the_instances_tool_call(self):
        """The template's broker client gives a tool call 120 s: a judge still thinking delivers it unchecked."""
        orders.add("m", "Retire the paper.", "operator", "cli", now=NOW - timedelta(days=1))
        release = threading.Event()
        self.ask.side_effect = lambda *a, **k: release.wait(5) and {"violates": True, "order_id": "o1", "reason": ""}
        with mock.patch.object(orders, "CHECK_TIMEOUT_S", 0.05):
            result = self.send()
        release.set()
        self.assertTrue(result["ok"], result)
        self.assertIn("gave no verdict within 0.05 s", result["result"])
        self.assertLess(orders.CHECK_TIMEOUT_S, 120)

    def test_i4_correction_closed_by_a_fact_stays_checked_until_three_messages_pass(self):
        """Review I4: corrections closed when the instance wrote a fact, and the behaviour came back."""
        self.correction(closed="fact")
        self.verdicts = [{"violates": False, "order_id": "", "reason": ""}] * 2 + [
            {"violates": True, "order_id": "k1028-1100-1", "reason": "asks again"}] + [
            {"violates": False, "order_id": "", "reason": ""}] * 3
        for _ in range(2):
            self.assertTrue(self.send("A status note.")["ok"])
        self.assertEqual(orders.corrections("m", self.b.root)[0]["passes"], 2)
        refused = self.send()
        self.assertIn("correction k1028-1100-1 (what was wrong: re-asked about retiring", refused["error"])
        self.assertEqual(orders.corrections("m", self.b.root)[0]["passes"], 0)   # the count starts again
        for _ in range(3):
            self.assertTrue(self.send("A status note.")["ok"])
        self.assertEqual(orders.corrections("m", self.b.root), [])
        calls = self.ask.call_count
        self.assertTrue(self.send("Another note.")["ok"])
        self.assertEqual(self.ask.call_count, calls)                 # no longer checked: no judge call

    def test_codex_checks_after_a_replay_cutoff_do_not_clear_its_corrections(self):
        """Codex review: three passing checks dated after a replay's as_of cleared the correction in that replay."""
        self.correction(closed="fact")
        later = NOW + timedelta(days=4)
        orders.checks_path("m").parent.mkdir(parents=True, exist_ok=True)
        orders.checks_path("m").write_text("".join(json.dumps(
            {"ts": (later + timedelta(minutes=i)).isoformat(timespec="seconds"), "message": f"x{i}.md",
             "orders": [], "corrections": ["k1028-1100-1"], "violates": False, "order_id": "", "reason": "",
             "judge": "j"}) + "\n" for i in range(3)))
        self.assertEqual(orders.corrections("m", self.b.root), [])                       # live: cleared
        held = orders.corrections("m", self.b.root, as_of=NOW)                          # replayed before them: not
        self.assertEqual([(c["id"], c["passes"]) for c in held], [("k1028-1100-1", 0)])
        self.assertEqual(orders.corrections("m", self.b.root, as_of=later + timedelta(minutes=1))[0]["passes"], 2)

    def test_not_accepted_and_later_corrections_are_not_checked(self):
        self.correction(closed="not_accepted")
        self.assertEqual(orders.corrections("m", self.b.root), [])
        self.correction(when=NOW + timedelta(hours=1))
        self.assertEqual(orders.corrections("m", self.b.root, as_of=NOW), [])
        with mock.patch.object(orders, "CORRECTION_PASSES", 0):
            self.correction()
            self.assertEqual(orders.corrections("m", self.b.root), [])

    def replay(self):
        replay = object.__new__(broker.ReplayBroker)
        replay.name, replay.project, replay.as_of, replay.operator_sent = "m", self.project, NOW, 0
        replay.root, replay.counts, replay._tags, replay.talk_id = self.b.root, self.b.counts, threading.local(), None
        replay.sim_orders, replay.sim_checks = [], []
        return replay

    def test_replay_checks_against_orders_then_and_records_nothing(self):
        orders.add("m", "Retire the paper.", "operator", "cli", now=NOW - timedelta(days=1))
        orders.add("m", "Added after the replayed cycle.", "operator", "cli", now=NOW + timedelta(days=1))
        replay = self.replay()
        self.verdicts = [{"violates": True, "order_id": "o1", "reason": "asks again"}]
        with self.assertRaisesRegex(PermissionError, "Retire the paper"):
            replay._send_message("operator", "Continue or retire?")
        self.assertNotIn("Added after", self.prompts[0])
        self.assertFalse(orders.checks_path("m").exists())
        file = self.operator_message(at=NOW - timedelta(minutes=5))
        self.assertIn("recorded standing order", replay._record_order("Keep it short.", file))
        self.assertEqual(len(orders.listing("m", dropped=True)), 2)

    def test_codex_replay_sees_the_order_it_recorded_and_its_own_checks_writing_nothing(self):
        """Codex review: a replayed record_order said "recorded" but the replay's later checks never saw the order, so it
        accepted a message the same live sequence would refuse."""
        orders.add("m", "Added after the replayed cycle.", "operator", "cli", now=NOW + timedelta(days=1))
        replay = self.replay()
        file = self.operator_message(at=NOW - timedelta(minutes=5))
        self.assertIn("recorded standing order o1:", replay._record_order("Retire the paper.", file))
        self.assertIn("already standing order o1", replay._record_order("retire the paper.", file))
        self.assertIn("o1 (heard from the operator, recorded by you", replay._orders_line())
        self.verdicts = [{"violates": True, "order_id": "o1", "reason": "asks again"}]
        with self.assertRaisesRegex(PermissionError, "standing order o1 .*Retire the paper"):
            replay._send_message("operator", "Continue or retire?")
        self.assertIn("[o1] Retire the paper.", self.prompts[0])
        self.assertEqual([o["text"] for o in orders.listing("m", dropped=True)], ["Added after the replayed cycle."])
        self.assertFalse(orders.checks_path("m").exists())
        # its own checks count toward a correction's passes within the replay, as a live cycle's would
        self.correction(when=NOW - timedelta(hours=3), closed="fact")
        self.verdicts = [{"violates": False, "order_id": "", "reason": ""}] * 3
        for _ in range(3):
            replay._send_message("operator", "A status note.")
        self.assertEqual(replay._corrections_held(), [])
        self.assertNotEqual(orders.corrections("m", self.b.root, as_of=NOW), [])        # nothing written
        self.assertFalse(orders.checks_path("m").exists())

    # -- the operator's verbs -----------------------------------------------------------------------------------------
    def test_cli_adds_lists_and_drops(self):
        self.enterContext(mock.patch.object(cli, "instance_root", return_value=self.b.root))
        self.enterContext(mock.patch.object(cli, "log"))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["orders", "m", "add", "Retire", "the", "paper."]), 0)
            self.assertEqual(cli.main(["orders", "m"]), 0)
            self.assertEqual(cli.main(["orders", "m", "drop", "o1"]), 0)
        text = out.getvalue()
        self.assertIn("added o1 (from the operator", text)
        self.assertIn("o1    in force", text)
        self.assertIn("dropped o1: Retire the paper.", text)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(cli.main(["orders", "m", "drop", "o1"]), 1)
        self.assertIn("refused: order o1 of m was already dropped", err.getvalue())

    def test_web_config_runs_the_cli_and_redraws_the_panel(self):
        self.enterContext(mock.patch.object(core, "INSTANCES", self.project))
        with mock.patch.object(web_config, "_ran", return_value=(200, "added o1")) as ran:
            self.assertEqual(web_config.run("orders-add", "m", {"text": "Retire.\r\nNow."}), (200, "added o1"))
            ran.assert_called_with(["orders", "m", "add", "--via", "web", "--", "Retire.\nNow."])
            web_config.run("orders-drop", "m", {"id": "o1"})
            ran.assert_called_with(["orders", "m", "drop", "--via", "web", "--", "o1"])
        with self.assertRaises(web_actions.Refused):
            web_config.run("orders-add", "m", {"text": "  "})
        orders.add("m", "Retire <the> paper.", "operator", "web", now=NOW)
        self.correction()
        panel = str(web_actions.saved_row("orders-add", {"name": "m"}))
        self.assertIn('data-row id="orders"', panel)
        self.assertIn("Retire &lt;the&gt; paper.", panel)
        self.assertIn('data-post="/action/orders-drop"', panel)
        self.assertIn("0 of 3 messages passed", panel)


if __name__ == "__main__":
    unittest.main()
