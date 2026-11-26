"""The self-knowledge mechanisms on the supervisor's side: the attention schema scored against the tool log (B3), the kill-test
harness (supervisor/experiments.py: knob changes, target cycles, endpoints), and the
stats.md sections — one that fails never costs the instance the others. The modules written by the relay lanes have
their own tests (test_metacog, test_anchoring, test_commitments, test_provenance, test_tom)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from supervisor import attention, experiments, metrics, trial

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
try:
    from shaft import cards
except ImportError:
    cards = None

# Calls whose kind of action the agent's cards.family and the supervisor's experiments.family must agree on.
FAMILY_CASES = [
    ("write_file", {"path": "self/self.md", "content": "x"}, "write_file self/"),
    ("append_file", {"path": "notes/log/day.md", "content": "x"}, "append_file notes/log/"),
    ("write_file", {"path": "plugins/web_research/main.py"}, "write_file plugins/web_research/"),
    ("run", {"command": "cd plugins && pytest -q"}, "run pytest"),
    ("run", {"command": "FOO=1 python3 x.py"}, "run python3 x.py"),
    ("run", {"command": "cd /a && python3 -m pytest -q"}, "run pytest"),
    ("run", {"command": "VAR=1 env FOO=2 git commit"}, "run git"),
    ("run", {"command": 'bash -lc "cd x && make test"'}, "run make"),
    ("run", {"command": "echo hi"}, None),
    ("run", {"command": "wc -l f"}, None),
    ("run", {"command": "cd /a && grep x f"}, None),
    ("send_message", {"to": "wisp", "text": "hi"}, "send_message → wisp"),
    ("http_request", {"url": "https://api.example.org/v1", "method": "post"}, "http_request POST api.example.org"),
    ("http_request", {"url": "https://api.example.org/v1"}, None),
    ("delegate_coding", {"task": "x"}, "delegate_coding"),
    ("write_file", {"path": "./self/a.md"}, "write_file self/"),
    ("write_file", {"path": "self/x/../a.md"}, "write_file self/"),
    ("write_file", {"path": "../outside.md"}, None),
    ("read_file", {"path": "notes/x.md"}, None),
]


def ep(ts, tools, intent=None, **kw):
    return {"ts": ts, "kind": "live", "intent": intent or {"goal": "g", "origin": "self"}, "finish": {"x": 1},
            "ended_by": "control", "tools": tools, **kw}


def call(tool, args, result="ok", **kw):
    return {"tool": tool, "args": json.dumps(args), "result": result, **kw}


class FamilyTest(unittest.TestCase):
    def test_supervisor_copy_agrees_with_the_agent(self):
        for tool, args, want in FAMILY_CASES:
            with self.subTest(tool=tool, args=args):
                self.assertEqual(experiments.family(tool, json.dumps(args)), want)
                if cards is not None:
                    self.assertEqual(cards.family(tool, json.dumps(args)), want)


class AttentionTest(unittest.TestCase):
    def test_score_against_the_tool_log(self):
        e = ep("2026-10-26T10:00:00", [
            call("declare_intent", {}), call("read_file", {"path": "notes/plan.md"}),
            call("fetch_page", {"url": "https://example.org/a"}), call("search_memory", {"query": "q"}),
            call("send_message", {"to": "operator", "text": "t"}), call("finish_cycle", {})],
            intent={"goal": "g", "attention": [{"source": "notes/"}, {"source": "web"}, {"source": "wisp"},
                                               {"why": "no source"}]})
        s = attention.score(e)
        self.assertEqual(s["calls"], 4)             # the controls are not where it looked
        self.assertEqual(s["on_named"], 2)          # notes/plan.md and the web page
        self.assertEqual(s["untouched"], ["wisp"])
        self.assertEqual(s["unreadable"], 1)
        self.assertIsNone(attention.score(ep("t", [])))   # nothing declared, nothing scored

    def test_synonyms_and_touches(self):
        self.assertTrue(attention.matches("operator", attention.touches(call("send_message", {"to": "operator"}))))
        self.assertTrue(attention.matches("memory", attention.touches(call("write_file", {"path": "memory/people/x.md"}))))
        self.assertTrue(attention.matches("example.org", attention.touches(call("fetch_page", {"url": "https://example.org/a"}))))
        self.assertTrue(attention.matches("code", attention.touches(call("write_file", {"path": "shaft/live.py"}))))
        self.assertTrue(attention.matches("another instance", attention.touches(call("send_message", {"to": "wisp"}))))
        self.assertFalse(attention.matches("wisp", attention.touches(call("send_message", {"to": "tundra"}))))
        self.assertFalse(attention.matches("another instance", attention.touches(call("send_message", {"to": "operator"}))))

    def test_it_does_not_over_match(self):
        heredoc = call("run", {"command": "python3 - <<'PY'\nclass T:\n    def go(self):\n        return self.root\nPY"})
        self.assertFalse(attention.matches("self", attention.touches(heredoc)))             # python's self is no folder
        curl = call("run", {"command": "curl -s https://example.org/notes/today.md"})
        self.assertFalse(attention.matches("notes/", attention.touches(curl)))              # a URL's path is the web
        self.assertTrue(attention.matches("web", attention.touches(call("http_request", {"url": "https://example.org"}))))
        self.assertFalse(attention.matches("plugins/", attention.touches(call("read_file", {"path": "self/goals.md"}))))
        self.assertFalse(attention.matches("record", attention.touches(call("read_file", {"path": "memory/episodes/a.jsonl"}))))
        self.assertFalse(attention.matches("notes/queue.json", attention.touches(call("read_file", {"path": "notes/q.md"}))))

    def test_supervisor_copy_of_the_agent_tools_covers_its_builtins(self):
        try:
            from shaft import commitments, tools
        except ImportError:
            self.skipTest("template/ is not checked out")
        names = {t["function"]["name"] for t in tools.BUILTINS + [tools.VIEW_IMAGE, commitments.COMMIT_TO]}
        self.assertEqual(names - attention.AGENT_TOOLS, set())   # a built-in mistaken for a plugin would touch plugins/


class AttentionMissedTouchesTest(unittest.TestCase):
    """Bug: attention scoring missed touches made through task_queue/record/run/plugins. Tundra's declared sources were
    'never touched' in most cycles (share 0-25%) while its calls went right there: the file a run command reads, a
    plugin's own code, the mail/calendar/drive/notion plugins, '.tmp' read as 'tmp',
    and declarations written as 'notes/…', 'a / b' or 'mail_search/mail_read (a thread)'."""

    def cycle(self, sources, tools):
        return attention.score(ep("2026-10-31T09:00:00", [call("declare_intent", {}), *tools, call("finish_cycle", {})],
                                  intent={"goal": "g", "attention": [{"source": s, "why": "w"} for s in sources]}))

    def test_a_plugin_touches_its_name_and_its_code(self):
        s = self.cycle(["task_queue", "plugins/task_queue/"], [call("task_queue", {"action": "done", "id": 7})] * 3)
        self.assertEqual((s["untouched"], s["share"]), ([], 1.0))

    def test_record_and_history_are_the_record(self):
        s = self.cycle(["record", "history"], [call("record", {"kind": "jobs", "limit": 5}),
                                               call("history", {"path": "shaft/kiln.py"})])
        self.assertEqual((s["untouched"], s["share"]), ([], 1.0))

    def test_run_touches_the_files_its_command_names(self):
        s = self.cycle(["shaft/kiln_report.py", "notes/queue.json", "tests/", ".tmp"], [
            call("run", {"command": "sed -n '40,80p' shaft/kiln_report.py; grep -n 'def ' shaft/kiln_report.py"}),
            call("run", {"command": "python3 - <<'PY'\nimport json\nrows=json.load(open('notes/queue.json'))\nPY"}),
            call("run", {"command": "python3 -m unittest tests.test_kiln_report -v > .tmp/kiln.txt 2>&1"}),
            call("run", {"command": "date"})])
        self.assertEqual(s["untouched"], [])
        self.assertEqual(s["on_named"], 3)              # date names no file
        self.assertNotEqual(s["share"], 0.0)            # the old scorer gave 0% here

    def test_run_of_a_script_the_cycle_wrote(self):
        s = self.cycle(["shaft/kiln_report.py"], [
            call("write_file", {"path": ".tmp/patch_kiln.py", "content": "p='shaft/kiln_report.py'\nopen(p).read()\n"}),
            call("run", {"command": "python3 .tmp/patch_kiln.py"})])
        self.assertEqual((s["untouched"], s["on_named"]), ([], 1))

    def test_plugins_touch_their_code_and_their_source(self):
        s = self.cycle(["plugins/kiln-watch/main.py", "mail", "calendar", "drive"], [
            call("kiln-watch", {"dry_run": True}), call("gmail", {"action": "search", "query": "q"}),
            call("cal-watch", {"job": "x"}), call("gdrive", {"action": "list"})])
        self.assertEqual((s["untouched"], s["share"]), ([], 1.0))
        s = self.cycle(["notion", "plugins/"], [call("http_request", {"url": "https://api.notion.com/v1/pages/x"}),
                                                call("kiln-watch", {})])
        self.assertEqual((s["untouched"], s["share"]), ([], 1.0))

    def test_drive_broker_tools_touch_drive(self):
        for tool in ('drive_search', 'drive_read', 'drive_download'):
            s = self.cycle(["drive"], [call(tool, {"id": "work/file00000000"})])
            self.assertEqual((s["untouched"], s["share"]), ([], 1.0))

    def test_dot_folders_and_save_paths(self):
        s = self.cycle([".tmp/kiln-dump.txt", ".tmp/"], [call("read_file", {"path": ".tmp/kiln-dump.txt"}),
                                                         call("mail_dump", {"id": "t1", "save_to": ".tmp/m.txt"})])
        self.assertEqual((s["untouched"], s["share"]), ([], 1.0))

    def test_declared_free_text_is_normalised(self):
        s = self.cycle(["Notes/…", "shaft/kiln.py / tests/", "mail_search/mail_read (the orchard thread)",
                        "plugins/kiln-watch + skills/"], [
            call("append_file", {"path": "notes/log.md", "content": "x"}), call("read_file", {"path": "tests/test_k.py"}),
            call("mail_read", {"id": "t1"}), call("read_file", {"path": "skills/kiln.md"})])
        self.assertEqual((s["untouched"], s["share"]), ([], 1.0))
        self.assertEqual(self.cycle(["mail_search"], [call("mail_read", {"id": "t1"})])["untouched"], [])  # mail is mail

    def test_a_source_it_cannot_read_is_unreadable_not_untouched(self):
        s = self.cycle(["supervisor source", "notes/"], [call("read_file", {"path": "notes/a.md"})])
        self.assertEqual((s["untouched"], s["unreadable_sources"], s["unreadable"]), ([], ["supervisor source"], 1))
        self.assertEqual(s["share"], 1.0)
        s = self.cycle(["supervisor source / .jobs record"], [call("record", {"kind": "jobs"})])
        self.assertEqual((s["untouched"], s["unreadable_sources"]), ([], []))   # one part it can read is enough

    def test_untouched_still_untouched(self):
        s = self.cycle(["record", "operator", "notes/queue.json"], [
            call("mail_read", {"id": "t1"}), call("calendar_events", {"start": "a", "end": "b"}),
            call("read_file", {"path": "memory/episodes/2026-10-30.jsonl"})])
        self.assertEqual(s["untouched"], ["record", "operator", "notes/queue.json"])
        self.assertEqual(s["share"], 0.0)


    def test_section(self):
        e = ep("2026-10-26T10:00:00", [call("read_file", {"path": "notes/a.md"}), call("web_search", {"query": "q"})],
               intent={"goal": "g", "attention": [{"source": "notes"}, {"source": "record"}]})
        with mock.patch.object(metrics, "live_episodes", return_value=[e]):
            lines = attention.section("x")
        self.assertIn("calls on a source I named: 50% on average", lines[2])
        self.assertIn("Named sources I never touched: 1 of 2", lines[3])
        with mock.patch.object(metrics, "live_episodes", return_value=[ep("t", [])]):
            self.assertEqual(attention.section("x"), [])


class TargetsTest(unittest.TestCase):
    def test_repeat_failures_need_an_earlier_failure_with_no_success_between(self):
        bad = call("write_file", {"path": "self/self.md"}, "ERROR: PermissionError: use update_self_model")
        good = call("write_file", {"path": "self/goals.md"}, "wrote 10 chars")
        eps = [ep("2026-10-20T09:00:00", [bad]), ep("2026-10-21T09:00:00", [bad]),          # repeat
               ep("2026-10-22T09:00:00", [good]), ep("2026-10-23T09:00:00", [bad]),         # after a success: not
               ep("2026-10-24T09:00:00", [call("run", {"command": "pytest"}, "exit 1 · failed", flag="exit")]),
               ep("2026-11-20T09:00:00", [bad])]                                           # weeks later: not
        self.assertEqual(experiments.repeat_failures(eps), {"2026-10-21T09:00:00": "write_file self/"})

    def test_endpoints(self):
        rec = {"episode": ep("t", [call("write_file", {"path": "self/x.md"}, "ERROR: PermissionError")]), "ended": "ok",
               "exit": 0, "completed": True}
        self.assertTrue(experiments.endpoint("cards", rec, "write_file self/"))
        self.assertFalse(experiments.endpoint("cards", rec, "run pytest"))
        held = {"episode": ep("t", [call("write_file", {"path": "self/x.md"}, "(held, not run yet) Before…")]),
                "ended": "ok", "exit": 0, "completed": True}
        self.assertFalse(experiments.endpoint("cards", held, "write_file self/"))   # a hold is not a failure
        # the template's forecast rule (review 2 M3): ran out = no finish, or a forced deadline wrap-up; a steps end
        # that still finished did not run out
        self.assertTrue(experiments.endpoint("forecast", {"episode": ep("t", [], ended_by="steps", finish=None),
                                                          "ended": "ok"}, ""))
        self.assertFalse(experiments.endpoint("forecast", {"episode": ep("t", [], ended_by="steps"), "ended": "ok"}, ""))
        self.assertTrue(experiments.endpoint("forecast", {"episode": ep("t", [], wrapped_up="deadline"), "ended": "ok"},
                                             ""))
        self.assertTrue(experiments.endpoint("forecast", {"episode": None, "ended": "timeout"}, ""))
        self.assertIsNone(experiments.endpoint("cards", None, "x"))
        # Regression (Codex review): an arm that crashed is no evidence — counted as "avoided", broken code would win
        crashed = {"episode": None, "ended": "ok", "exit": 1, "completed": False}
        self.assertIsNone(experiments.endpoint("cards", crashed, "write_file self/"))
        self.assertIsNone(experiments.endpoint("forecast", crashed, ""))
        self.assertIsNone(experiments.endpoint("cards", {**rec, "completed": False}, "write_file self/"))


class KnobChangeTest(unittest.TestCase):
    CONFIG = b'FIELDS = {\n    "OUTCOME_CARDS": (str, "on"),\n    "FORECAST": (str, "on"),\n}\n'
    ENV = b'OUTCOME_CARDS="on"'

    def change(self, env=b""):
        with mock.patch.object(trial, "committed_code", return_value={"shaft/config.py": self.CONFIG,
                                                                     "config.env": env}):
            return experiments.knob_change(Path("/nowhere"), "wisp", "OUTCOME_CARDS", "off")

    def test_flips_only_that_default(self):
        out = self.change()["shaft/config.py"].decode()
        self.assertIn('"OUTCOME_CARDS": (str, "off")', out)
        self.assertIn('"FORECAST": (str, "on")', out)

    def test_refuses_without_the_knob(self):
        with self.assertRaisesRegex(experiments.ExperimentError, "update it to the template"):
            with mock.patch.object(trial, "committed_code", return_value={"shaft/config.py": b"FIELDS = {}"}):
                experiments.knob_change(Path("/nowhere"), "wisp", "OUTCOME_CARDS", "off")

    def test_refuses_config_env_override(self):
        with self.assertRaisesRegex(experiments.ExperimentError, "config.env sets OUTCOME_CARDS"):
            self.change(self.ENV)


class BlindHoldsTest(unittest.TestCase):
    def test_judges_never_read_the_cards(self):
        """Regression (Codex review): a card lists earlier verdicts ("cycle judged no") — a prior on the outcome."""
        card = "(held, not run yet) Before this runs — 3 failed\n- 2026-10-20 09:00 · goal \"g\" · failed · cycle judged no"
        e = {**ep("2026-10-26T10:00:00", [call("write_file", {"path": "self/x.md"}, card)]), "steps": 2}
        case = metrics._case(e)
        self.assertNotIn("cycle judged", case)
        self.assertIn(metrics.HELD_SHOWN, case)
        arm = {"episode": e, "trace": [], "diff": "", "exit": 0, "ended": "ok", "seconds": 1}
        self.assertNotIn("cycle judged", trial._run_text(arm))
        plain = ep("2026-10-26T10:00:00", [call("read_file", {"path": "x"}, "text")])
        self.assertIs(metrics._blind(plain), plain)   # an episode without a hold stays as it was


class SectionsTest(unittest.TestCase):
    def test_a_failing_section_never_costs_the_others(self):
        with mock.patch("supervisor.attention.section", side_effect=RuntimeError("boom")), \
                mock.patch("supervisor.commitments.section", return_value=["## Promises", "- one"]), \
                mock.patch.object(metrics, "log") as log:
            lines = metrics._sections("wisp", ("attention", "commitments"))
        self.assertEqual(lines, ["", "## Promises", "- one"])
        self.assertIn("stats section attention failed", log.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
