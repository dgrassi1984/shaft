"""The self-knowledge mechanisms on the supervisor's side: the attention schema scored against the tool log (B3), the kill-test
harness (supervisor/experiments.py: knob changes, target cycles, endpoints, the workspace's planted probe), and the
stats.md sections — one that fails never costs the instance the others. The modules written by the relay lanes have
their own tests (test_metacog, test_anchoring, test_commitments, test_provenance, test_tom)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
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
    CONFIG = b'FIELDS = {\n    "WORKSPACE": (str, "competitive"),\n    "OUTCOME_CARDS": (str, "on"),\n}\n'
    ENV = b'# model settings\r\nLLM_BASE_URL="broker"\r\nWORKSPACE="off"   # comment\r\nLLM_MODEL="example" # keep'

    def change(self, env=b""):
        with mock.patch.object(trial, "committed_code", return_value={"shaft/config.py": self.CONFIG,
                                                                     "config.env": env}):
            return experiments.knob_change(Path("/nowhere"), "wisp", "WORKSPACE", "relevance")

    def test_flips_only_that_default(self):
        out = self.change()["shaft/config.py"].decode()
        self.assertIn('"WORKSPACE": (str, "relevance")', out)
        self.assertIn('"OUTCOME_CARDS": (str, "on")', out)

    def test_refuses_without_the_knob(self):
        with self.assertRaisesRegex(experiments.ExperimentError, "update it to the template"):
            with mock.patch.object(trial, "committed_code", return_value={"shaft/config.py": b"FIELDS = {}"}):
                experiments.knob_change(Path("/nowhere"), "wisp", "WORKSPACE", "off")

    def test_issue_3_workspace_arm_changes_only_the_knob(self):
        code = {"shaft/config.py": self.CONFIG.replace(b'"competitive"', b'"off"'), "config.env": self.ENV}
        with mock.patch.object(trial, "committed_code", return_value=code):
            change = experiments.knob_change(Path("/nowhere"), "wisp", "WORKSPACE", "competitive", env_ok=True)
        self.assertEqual(change, {"shaft/config.py": self.CONFIG,
                                  "config.env": b'# model settings\r\nLLM_BASE_URL="broker"\r\n'
                                                b'WORKSPACE="competitive"\r\nLLM_MODEL="example" # keep'})
        self.assertEqual(trial.model_of({**code, **change}), trial.model_of(code))

    def test_issue_3_generic_knob_trial_still_refuses_config_env_override(self):
        with self.assertRaisesRegex(experiments.ExperimentError, "config.env sets WORKSPACE"):
            self.change(self.ENV)


def control_rows(n: int, acted: int, fp=None, shown: int | None = None) -> None:
    """n positive-control replays of the workspace experiment, `acted` of them acted on, `shown` with the probe in
    front (all, by default)."""
    for i in range(n):
        experiments._ws_append("wisp", {"ts": f"2026-10-2{i}", "arm": experiments.CONTROL, "completed": True,
                                        "acted": i < acted, "captured": 0, "fingerprint": fp,
                                        "in_front": "inbox" if i < (n if shown is None else shown) else ""})


class ProbeTest(unittest.TestCase):
    def test_plant_is_seeded_and_only_where_retrieval_alone_reaches(self):
        t0 = datetime(2026, 10, 26, 9)
        for seed in ("seed", "2", "x", "y", "z"):
            with tempfile.TemporaryDirectory() as d:
                dest = Path(d)
                (dest / "memory").mkdir()
                (dest / "memory" / "digests.md").write_text("\n## 2026-10-25 22:00 — digest\n\nthe newest digest\n")
                a = experiments.plant(dest, seed, "Daniele", "orchard rows", t0)
                items = a["items"]
                self.assertEqual([it["kind"] for it in items], ["probe", "done", "done", "done"])
                self.assertEqual(len({it["nonce"] for it in items}), 4)
                text = "\n".join(p.read_text() for p in dest.rglob("*.md"))
                for it in items:
                    self.assertIn(f"probe {it['nonce']}", text)
                # Regression (Codex review): a short digests.md is pasted whole into the prompt, probe and all
                self.assertEqual((dest / "memory" / "digests.md").read_text(),
                                 "\n## 2026-10-25 22:00 — digest\n\nthe newest digest\n")
                self.assertTrue(all(it["place"].startswith(("notes/", "memory/facts-archive.md")) for it in items))
            with tempfile.TemporaryDirectory() as d:
                self.assertEqual(experiments.plant(Path(d), seed, "Daniele", "orchard rows", t0), a)   # seeded

    def test_plant_never_follows_a_link_out_of_the_copy(self):
        """Regression (Codex review): a mind may hold notes -> elsewhere; a plain write would follow it out."""
        from supervisor.core import UnsafePath
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
            dest = Path(d)
            (dest / "memory").symlink_to(outside, target_is_directory=True)
            (dest / "notes").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(UnsafePath):
                experiments.plant(dest, "seed", "Daniele", "orchard rows", datetime(2026, 10, 26, 9))
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_report_and_kill_rule(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(experiments, "workspace_path", return_value=Path(d) / "w.jsonl"):
            control_rows(6, 6)
            for i in range(6):
                for arm, acted in (("off", False), ("relevance", i == 0), ("competitive", i < 5)):
                    experiments._ws_append("wisp", {"ts": f"2026-10-2{i}", "arm": arm, "completed": True,
                                                    "acted": acted, "captured": 0, "in_front": "focus" if acted else ""})
            out = experiments.workspace_report("wisp")
        self.assertIn("positive control (probe in the inbox, shipped code): acted 6/6", out)
        self.assertIn("acted on the probe 5/6 (5 of them in front)", out)
        self.assertIn("only competitive acted 4, only relevance 0", out)
        self.assertIn("competitive selection stays", out)

    def test_report_pairs_only_rows_of_the_latest_code_and_model(self):
        """Regression (Codex review): a resumed run on other code or another model was paired with the first."""
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(experiments, "workspace_path", return_value=Path(d) / "w.jsonl"):
            for fp, acted in (("old", True), ("new", False)):
                control_rows(1, 1, fp)
                for arm in experiments.WORKSPACE_ARMS:
                    experiments._ws_append("wisp", {"ts": "2026-10-20", "arm": arm, "completed": True, "acted": acted,
                                                    "captured": 0, "fingerprint": fp})
            out = experiments.workspace_report("wisp")
        self.assertIn("acted on the probe 0/1", out)
        self.assertNotEqual(experiments.fingerprint({"a": b"1"}, "m"), experiments.fingerprint({"a": b"2"}, "m"))
        self.assertNotEqual(experiments.fingerprint({"a": b"1"}, "m"), experiments.fingerprint({"a": b"1"}, "n"))


class WorkspaceInstrumentTest(unittest.TestCase):
    """Regression: workspace kill test could not separate arms: probe acted on 0/19 even in off (Tundra, 2026-10-28/29).
    The probe ranked ~8,500th of ~13,800 chunks for the cycle's search, so no arm ever had it in front; the endpoint
    read a diff cut at 16,000 characters; and with no positive control the report still said "delete it"."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-ws-")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.enterContext(mock.patch.object(experiments, "workspace_path", return_value=self.dir / "w.jsonl"))

    def copy(self) -> tuple[Path, dict, dict]:
        dest = self.dir / f"copy{len(list(self.dir.glob('copy*')))}" / "wisp"
        for folder in ("memory", "notes", "inbox"):
            (dest / folder).mkdir(parents=True)
        (dest / "memory" / "journal.md").write_text("## 2026-10-25 09:00 — orchard\n\nCounted the rows.\n")
        items = experiments.plant(dest, "seed", "Daniele", "orchard rows", datetime(2026, 10, 26, 9))["items"]
        probe = next(it for it in items if it["kind"] == "probe")
        before = experiments._counts(experiments._texts(dest), [it["nonce"] for it in items])
        return dest, probe, {"items": items, "before": before}

    def test_the_endpoint_detects_every_write_path(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
        from shaft.tools import Toolbox

        def toolbox(dest, tool, args):
            Toolbox(dest).run(tool, json.dumps(args))

        def shell(dest, command):
            subprocess.run(["/bin/sh", "-c", command], cwd=dest, check=True)

        writes = {
            "write_file": lambda d, n: toolbox(d, "write_file", {"path": "notes/probes.md", "content": f"probe {n}\n"}),
            "append_file": lambda d, n: toolbox(d, "append_file", {"path": "notes/probes.md", "content": f"probe {n}"}),
            "append_file, as markdown": lambda d, n: toolbox(d, "append_file", {"path": "notes/probes.md",
                                                                                "content": f"- `probe {n}`\n"}),
            "run (the shell)": lambda d, n: shell(d, f"printf 'probe {n}\\n' >> notes/probes.md"),
            "run, another case of the path": lambda d, n: shell(d, f"echo 'probe {n}' >> notes/Probes.md"),
            "run, through ./": lambda d, n: shell(d, f"echo 'probe {n}' >> ./notes/../notes/probes.md"),
        }
        for how, write in writes.items():
            with self.subTest(how):
                dest, probe, planted = self.copy()
                write(dest, probe["nonce"])
                got = experiments.acts(dest, planted["items"], planted["before"])
                self.assertTrue(got[probe["nonce"]]["acted"], how)
                self.assertEqual(sum(v["acted"] for v in got.values()), 1)   # no distractor counted with it

    def test_the_endpoint_reads_whole_files_not_the_judges_capped_diff(self):
        dest, probe, planted = self.copy()
        before = trial._files_now(dest)
        (dest / "memory" / "journal.md").write_text("## long\n\n" + "a line of the cycle's journal\n" * 1000)
        (dest / "notes" / "probes.md").write_text(f"probe {probe['nonce']}\n")
        diff = trial.file_diff(before, trial._files_now(dest))
        # The diff cut in path order once hid this file from judges and from the old endpoint (`_wrote` on the diff);
        # since 2026-11-08 each file keeps a share, but it is still capped: the endpoint reads the files themselves.
        self.assertIn("+++ b/notes/probes.md", diff)
        self.assertIn("this file's diff cut here", diff)
        self.assertTrue(experiments.acts(dest, planted["items"], planted["before"])[probe["nonce"]]["acted"])

    def test_talking_about_it_is_not_doing_it_but_is_recorded(self):
        dest, probe, planted = self.copy()
        with (dest / "memory" / "journal.md").open("a") as f:
            f.write(f"\nI did not append probe {probe['nonce']} to notes/probes.md.\n")
        got = experiments.acts(dest, planted["items"], planted["before"])[probe["nonce"]]
        self.assertFalse(got["acted"])
        self.assertEqual(got["wrote"], ["memory/journal.md"])   # where it went instead: the report can tell
        self.assertFalse(experiments._line_in(f"I did not append probe {probe['nonce']}", probe["nonce"]))

    def arms(self, acted=False, n=4):
        for i in range(n):
            for arm in experiments.WORKSPACE_ARMS:
                experiments._ws_append("wisp", {"ts": f"2026-10-2{i}", "arm": arm, "completed": True, "acted": acted,
                                                "captured": 0, "in_front": ""})

    def test_no_positive_control_gives_no_verdict(self):
        self.arms()   # the 2026-10-28/29 run's shape: three arms, nothing acted on, no control
        out = experiments.workspace_report("wisp")
        self.assertIn("no verdict: no positive control replayed yet", out)
        self.assertNotIn("delete it", out)
        self.assertNotIn("kill rule", out)

    def test_a_failed_positive_control_reports_instrument_failure_not_a_verdict(self):
        control_rows(4, 1)   # acted on once in four: not most
        self.arms()
        out = experiments.workspace_report("wisp")
        self.assertIn("INSTRUMENT FAILED: the probe in the inbox was acted on in only 1/4 control replays", out)
        self.assertNotIn("delete it", out)
        self.assertNotIn("kill rule", out)
        self.assertTrue(experiments.workspace_instrument("wisp")["failed"])

    def test_a_control_whose_probe_was_not_in_front_is_a_broken_plant(self):
        control_rows(4, 4, shown=3)
        out = experiments.workspace_report("wisp")
        self.assertIn("INSTRUMENT FAILED: the probe planted in the inbox was in front of only 3/4", out)

    def test_a_working_control_lets_the_arms_be_judged(self):
        control_rows(4, 3)
        self.arms()
        out = experiments.workspace_report("wisp")
        self.assertNotIn("INSTRUMENT FAILED", out)
        self.assertIn("kill rule:", out)

    def fake_run(self, writes_probe: bool):
        """run_workspace on one past state with every expensive step replaced: replay_arm prepares a real copy (plant,
        `front`), lets the 'cycle' append the probe's line or not, and inspects it as the real one does."""
        calls = []
        shipped_code = {"shaft/config.py": b'"WORKSPACE": (str, "competitive"),\n',
                        "config.env": b'LLM_BASE_URL="broker"\nWORKSPACE="off" # operator\n'}

        def replay(project, name, st, code, *, prepare, inspect, **kw):
            arm = kw["billed"].rsplit(":", 1)[1]   # the arms of a state run at once: a copy each
            calls.append(arm)
            if arm == experiments.CONTROL:
                self.assertEqual(code, shipped_code)
            else:
                self.assertEqual(code["config.env"], f'LLM_BASE_URL="broker"\nWORKSPACE="{arm}"\n'.encode())
            dest = self.dir / f"replay-{arm}" / name
            for folder in ("memory", "notes", "inbox"):
                (dest / folder).mkdir(parents=True)
            prepare(dest)
            if writes_probe:
                for p in dest.rglob("*.md"):   # the cycle acts on the probe wherever it is planted
                    for line in p.read_text().splitlines():
                        if "when I read this" in line:
                            nonce = line.split("`probe ")[1].split("`")[0]
                            with (dest / "notes" / "probes.md").open("a") as f:
                                f.write(f"probe {nonce}\n")
            return {"completed": True, "ended": "ok", "episode": {"tools": [], "steps": 3}, "trace": [], "usd": 0.01,
                    "inspected": inspect(dest)}

        def shown(project, name, dest, state, index=None):
            inbox = "\n".join(p.read_text() for p in (dest / "inbox").glob("*.md"))
            return {"system": "", "inbox": inbox, "modes": {m: {"focus": ""} for m in experiments.WORKSPACE_ARMS}}

        st = {"ts": "2026-10-26T10:00:00", "t0": "2026-10-26T09:00:00", "start_commit": "x", "inbox": []}
        with mock.patch.object(trial, "committed_code", return_value=shipped_code), \
                mock.patch.object(trial, "live_episodes", return_value=[{"ts": st["ts"], "kind": "live"}]), \
                mock.patch.object(trial, "state_of", return_value=st), mock.patch.object(trial, "_starts", return_value=[]), \
                mock.patch.object(trial, "replay_arm", side_effect=replay), \
                mock.patch.object(trial, "LEDGER", self.dir / "none.jsonl"), \
                mock.patch.object(experiments, "_topic", return_value="orchard rows"), \
                mock.patch.object(experiments, "state_hosts", return_value=[]), \
                mock.patch.object(experiments, "front", side_effect=shown), \
                mock.patch("supervisor.core.instance_config", return_value={}):
            result = experiments.run_workspace("wisp", 1, project=self.dir)
        self.assertEqual(result["fingerprint"], experiments.fingerprint(shipped_code, result["model"]))
        return result, calls

    def test_a_run_whose_control_fails_replays_no_arm(self):
        result, calls = self.fake_run(writes_probe=False)
        self.assertEqual(calls, [experiments.CONTROL])   # the three arms were never paid for
        self.assertTrue(result["instrument"]["failed"])
        self.assertIn("INSTRUMENT FAILED", experiments.workspace_report("wisp"))

    def test_a_run_whose_control_works_replays_the_arms(self):
        result, calls = self.fake_run(writes_probe=True)
        self.assertEqual(calls[0], experiments.CONTROL)
        self.assertEqual(sorted(calls[1:]), sorted(experiments.WORKSPACE_ARMS))
        rows = experiments.workspace_rows("wisp")
        self.assertEqual([r["acted"] for r in rows], [True] * 4)
        self.assertEqual(rows[0]["in_front"], "inbox")
        self.assertIn("positive control (probe in the inbox, shipped code): acted 1/1", experiments.workspace_report("wisp"))


    def test_a_run_embeds_the_mind_once_and_hands_the_index_on(self):
        """With no .tmp/memory-index.json in the instance (Tundra, 2026-10-31) every copy embedded all ~14,000 chunks
        again and `shaft front` timed out: the first copy's vectors go to the next."""
        seen = []

        def verb(cmd, cwd, **kw):
            index = Path(cwd) / ".tmp" / "memory-index.json"
            seen.append(index.read_text() if index.exists() else None)
            index.write_text(f'{{"copy": {len(seen)}}}')
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"modes": {}}), "")

        held: dict = {}
        with mock.patch("subprocess.run", side_effect=verb):
            for i in range(3):
                dest = self.dir / f"c{i}" / "wisp"
                dest.mkdir(parents=True)
                experiments.front(self.dir, "wisp", dest, {"t0": "2026-10-26T09:00:00"}, held)
        self.assertEqual(seen, [None, '{"copy": 1}', '{"copy": 2}'])

    def test_the_vector_log_is_handed_on_after_a_copy_migrates(self):
        """Bug: the template's memory search moved its vectors to .tmp/memory-index.bin and deletes the
        JSON after migrating; the handoff carried only the JSON, so every later copy started with no vectors and
        embedded the whole mind again. The next copy gets the log, and no stale JSON."""
        seen = []

        def verb(cmd, cwd, **kw):
            tmp = Path(cwd) / ".tmp"
            seen.append(sorted(p.name for p in tmp.iterdir() if p.name.startswith("memory-index")))
            (tmp / "memory-index.json").unlink(missing_ok=True)       # the copy's code migrated it
            (tmp / "memory-index.bin").write_bytes(b"LOG%d" % len(seen))
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"modes": {}}), "")

        held: dict = {"files": {".tmp/memory-index.json": b"{}"}}
        with mock.patch("subprocess.run", side_effect=verb):
            for i in range(3):
                dest = self.dir / f"m{i}" / "wisp"
                dest.mkdir(parents=True)
                experiments.front(self.dir, "wisp", dest, {"t0": "2026-10-26T09:00:00"}, held)
        self.assertEqual(seen, [["memory-index.json"], ["memory-index.bin"], ["memory-index.bin"]])
        self.assertEqual(held, {"files": {".tmp/memory-index.bin": b"LOG3"}})


@unittest.skipUnless(os.path.exists("/usr/bin/sandbox-exec"), "macOS sandbox")
class WorkspaceDryRunTest(unittest.TestCase):
    """The dry run on a fixture, through the template's real `shaft front` in the agent's sandbox: per state, where
    the probe is in front of each arm and of the control — no model call, no replay."""

    def check_retrievability_table(self, shipped_mode, override=b""):
        template = Path(__file__).resolve().parents[2] / "template"
        tmp = tempfile.TemporaryDirectory(prefix="shaft-ws-dry-")
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        mind = base / "mind"
        shutil.copytree(template / "shaft", mind / "shaft", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(template / "config.env", mind / "config.env")
        if override:
            with (mind / "config.env").open("ab") as f:
                f.write(override)
        for folder in ("self", "memory", "notes", "inbox"):
            (mind / folder).mkdir()
        (mind / "self" / "goals.md").write_text("# Goals\n\n- Survey the orchard rows and count the apple trees\n")
        (mind / "notes" / "orchard.md").write_text("# Orchard\n\nThe orchard rows: apple trees counted by row, "
                                                   "survey of the north field.\n")
        for i in range(6):
            (mind / "notes" / f"other{i}.md").write_text(f"# Other {i}\n\nUnrelated: train timetables and invoices.\n")

        def reconstruct(project, name, state, code, dest):
            shutil.copytree(mind, dest)
            return dest

        st = {"ts": "2026-10-26T10:00:00", "t0": "2026-10-26T09:00:00", "start_commit": "x", "inbox": []}
        code = {"shaft/config.py": (template / "shaft" / "config.py").read_bytes(),
                "config.env": (mind / "config.env").read_bytes()}
        with tempfile.TemporaryDirectory() as sup, \
                mock.patch.object(experiments, "workspace_path", return_value=Path(sup) / "w.jsonl"), \
                mock.patch.object(trial, "committed_code", return_value=code), \
                mock.patch.object(trial, "live_episodes", return_value=[{"ts": st["ts"], "kind": "live"}]), \
                mock.patch.object(trial, "state_of", return_value=st), mock.patch.object(trial, "_starts", return_value=[]), \
                mock.patch.object(trial, "reconstruct", side_effect=reconstruct), \
                mock.patch.object(trial, "link_env"), mock.patch.object(trial, "LEDGER", Path(sup) / "none.jsonl"), \
                mock.patch.object(trial, "replay_arm", side_effect=AssertionError("a dry run replays nothing")), \
                mock.patch.object(experiments, "_topic", return_value="orchard rows"), \
                mock.patch("supervisor.core.instance_config", return_value={}), \
                mock.patch("supervisor.core.EMBED_PORT", 9):   # no embedding service: words only, deterministic
            r = experiments.run_workspace("wisp", 1, dry_run=True, project=base)
        self.assertEqual(r["replays"], 4)   # the control and three arms, none run
        self.assertEqual(r["fingerprint"], experiments.fingerprint(code, r["model"]))
        row = r["table"][0]
        self.assertEqual(row["place"], "notes/orchard.md")   # the one host the cycle's search ranks first
        self.assertEqual((row["rank"], row["rank_after"]), (0, 0))
        self.assertEqual(row["control"], "inbox")
        self.assertEqual(row["off"], "focus")          # automatic recall shows the top hit
        self.assertEqual(row["relevance"], "focus")    # the memory codelet's top hit keeps its rank: relevance 1.0
        text = experiments.workspace_dry_run_text("wisp", r)
        self.assertIn("notes/orchard.md (#1→#1)", text)
        self.assertIn(f"in front: control 1/1 (shipped mode {shipped_mode}), off 1/1, relevance 1/1", text)
        self.assertIn("memory search ran on words only here", text)
        self.assertIn("cost: unknown — no priced replay of wisp", text)

    def test_the_retrievability_table(self):
        self.check_retrievability_table("competitive")

    def test_issue_3_retrievability_table_with_workspace_set_in_config_env(self):
        self.check_retrievability_table("off", b'\nWORKSPACE="off"   # comment\n')


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
