"""The operator's edits of config.env and drive.md (`shaft config`, `shaft drive`, the web UI's
Config and Drive pages): only the named lines change, a stale edit is refused, the instance's set_config and the
operator never lose each other's line, and the edit is committed as the operator's — at once when the instance is
free, by its run otherwise, never undone by the gate as the instance's (kiln, 2026-10-13)."""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from supervisor import core, opconfig
from supervisor.tests import test_config_writers as cw
from supervisor.cloudllm import _effort_listing as real_listing

TEMPLATE = ('# A local server, or "broker".\nLLM_BASE_URL="http://127.0.0.1:8780/v1"   # SHAFT\'s own\n'
            'LLM_MODEL="ternary-bonsai-2-27b-mlx"\nLLM_API_KEY=""\n# How hard I think.\nLLM_REASONING_EFFORT="medium"\n'
            '# How often I wake.\nWAKE_INTERVAL_MINUTES=120\n\nOPERATOR_NAME="the operator"\n')
CONFIG = ('# SHAFT instance configuration\n\n# A local server, or "broker".\nLLM_BASE_URL="broker"\n'
          'LLM_MODEL="openrouter/openai/gpt-6.1-sol"\nLLM_API_KEY="sk-secret-value"\n# How hard I think.\n'
          'LLM_REASONING_EFFORT="medium"\nWAKE_INTERVAL_MINUTES=5   # the operator, 2026-10-17 (floor 5)\n'
          'CUSTOM_KNOB=yes\n\nOPERATOR_NAME="the operator"\n')
AGENT_CONFIG = 'CHOICES = {"LLM_REASONING_EFFORT": ("medium", "xhigh", "low")}\n'
TODAY = date.today().isoformat()
REAL_COMMIT = opconfig._commit


class OpConfigTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="shaft-opcfg-")).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = self.dir / "instances" / "wisp"
        (self.root / "shaft").mkdir(parents=True)
        (self.root / "shaft" / "config.py").write_text(AGENT_CONFIG)
        (self.root / "config.env").write_text(CONFIG)
        (self.dir / "template").mkdir()
        (self.dir / "template" / "config.env").write_text(TEMPLATE)
        self.committed = []
        for p in (mock.patch.object(opconfig, "instance_root", return_value=self.root),
                  mock.patch("supervisor.core.instance_root", return_value=self.root),
                  mock.patch.object(opconfig, "TEMPLATE", self.dir / "template"),
                  mock.patch.object(core, "SUP_DIR", self.dir / "sup"),
                  mock.patch.object(opconfig, "_commit", side_effect=lambda n, r, w: self.committed.append(w) or True),
                  mock.patch("supervisor.cloudllm.CLOUD_MODELS", ["claude-haiku-5-5", "openrouter/xiaomi/mimo-v2.6-pro",
                                                                   "openrouter/openai/gpt-6.1-sol"]),
                  mock.patch("supervisor.selfhosted.SERVERS", {"gpu": {"url": "http://x"}}),
                  mock.patch("supervisor.autonomy.wake_floor", return_value=5),
                  # OpenRouter's /models as probed 2026-11-13: mimo reports no levels
                  mock.patch("supervisor.cloudllm._effort_listing", return_value={
                      "xiaomi/mimo-v2.6-pro": {"levels": [], "default": None},
                      "openai/gpt-6.1-sol": {"levels": ["low", "medium", "high", "xhigh", "max"], "default": "medium"}}),
                  mock.patch.object(opconfig, "log")):
            p.start()
            self.addCleanup(p.stop)

    def text(self) -> str:
        return (self.root / "config.env").read_text()


class EffortChoicesTest(OpConfigTest):
    def test_multiline_choices_are_read_and_effort_is_validated_without_executing_agent_code(self):
        (self.root / "shaft/config.py").write_text(
            'raise RuntimeError("must never run")\n'
            'CHOICES = {\n'
            '    "LLM_REASONING_EFFORT": ("medium", "xhigh", "low"),\n'
            '    "OTHER": ("on", "off"),\n'
            '}\n')
        self.assertEqual(opconfig.effort_choices(self.root, "claude-haiku-5-5"), ("low", "medium", "xhigh"))
        field = next(f for f in opconfig.fields("wisp") if f["key"] == "LLM_REASONING_EFFORT")
        self.assertEqual(field["choices"], ["low", "medium", "xhigh"])
        with self.assertRaisesRegex(opconfig.Refused, "must be one of low, medium, xhigh"):
            opconfig.set_values("wisp", {"LLM_REASONING_EFFORT": "invalid"})
        self.assertEqual(self.text(), CONFIG)

    def test_unreadable_invalid_or_nonliteral_choices_keep_none_fallback(self):
        for text in ('CHOICES = {', 'CHOICES = make_choices()', 'CHOICES = []',
                     'CHOICES = {"LLM_REASONING_EFFORT": "medium"}', 'X = {}'):
            (self.root / "shaft/config.py").write_text(text)
            self.assertIsNone(opconfig.effort_choices(self.root, "claude-haiku-5-5"), text)
        for operation in ("parse", "literal_eval"):
            for failure in (RecursionError, MemoryError):
                with self.subTest(operation=operation, failure=failure.__name__):
                    (self.root / "shaft/config.py").write_text(AGENT_CONFIG)
                    with mock.patch.object(opconfig.ast, operation, side_effect=failure):
                        self.assertIsNone(opconfig.effort_choices(self.root, "claude-haiku-5-5"))
        (self.root / "shaft/config.py").unlink()
        self.assertIsNone(opconfig.effort_choices(self.root, "claude-haiku-5-5"))
        (self.root / "shaft/config.py").symlink_to(self.dir / "template/config.env")
        self.assertIsNone(opconfig.effort_choices(self.root, "claude-haiku-5-5"))

    def test_annotated_choices_and_existing_single_line_choices(self):
        self.assertEqual(opconfig.effort_choices(self.root, "claude-haiku-5-5"), ("low", "medium", "xhigh"))
        (self.root / "shaft/config.py").write_text('CHOICES: dict = {"LLM_REASONING_EFFORT": ["low"]}\n')
        self.assertEqual(opconfig.effort_choices(self.root, "claude-haiku-5-5"), ("low",))


class SetTest(OpConfigTest):
    def test_an_appended_key_starts_its_own_run_and_inherits_no_help_20261110(self):
        from supervisor.broker import Broker
        (self.root / "config.env").write_text('# Set your name here.\nOPERATOR_NAME="x"\n')
        broker = Broker("wisp", self.dir, {}, self.dir / "broker.log")
        self.assertTrue(broker.call("set_config", {"key": "WATCH_GCHAT", "value": "wake"})["ok"])
        self.assertEqual(self.text(), '# Set your name here.\nOPERATOR_NAME="x"\n\nWATCH_GCHAT="wake"\n')
        field = next(f for f in opconfig.fields("wisp") if f["key"] == "WATCH_GCHAT")
        self.assertEqual(field["help"], opconfig.WATCH_HELP["WATCH_GCHAT"])

    def test_replacing_a_key_is_byte_for_byte_and_appending_twice_adds_one_blank_line_each_20261110(self):
        original = '# Shared help.\nX=old  # inline\nY=keep\n'
        self.assertEqual(core.config_with(original, "X", 'X="new"'),
                         '# Shared help.\nX="new"\nY=keep\n')
        first = core.config_with(original, "A", 'A="1"')
        self.assertEqual(core.config_with(first, "B", 'B="2"'),
                         original + '\nA="1"\n\nB="2"\n')
        self.assertEqual(core.config_with("", "A", 'A="1"'), 'A="1"\n')
        for ending in ("\n", " \t\n"):
            with self.subTest(ending=ending):
                self.assertEqual(core.config_with(original + ending, "A", 'A="1"'),
                                 original + ending + '\nA="1"\n')

    def test_an_append_then_its_removal_gives_back_the_same_bytes_20261110(self):
        for text in ("", "X=1\n", "X=1\n\n", "# c\nX=1\n \t\n", "\n", "X=1\n\n\n"):
            with self.subTest(text=text):
                appended = core.config_with(text, "A", 'A="1"')
                self.assertEqual(core.config_with(appended, "A", None), text)

    def test_only_the_named_line_changes_byte_for_byte(self):
        r = opconfig.set_values("wisp", {"LLM_REASONING_EFFORT": "xhigh"})
        self.assertEqual(r["changed"], ["LLM_REASONING_EFFORT"])
        self.assertEqual(self.text(), CONFIG.replace('LLM_REASONING_EFFORT="medium"',
                                                     f'LLM_REASONING_EFFORT="xhigh"   # the operator, {TODAY}'))
        self.assertEqual(self.committed, ["config"])

    def test_a_model_switch_sets_its_base_url_in_the_same_write(self):
        writes = []
        real = core.safe_write
        with mock.patch.object(opconfig, "safe_write", side_effect=lambda *a: writes.append(a) or real(*a)):
            opconfig.set_values("wisp", {"LLM_MODEL": "ternary-bonsai-2-27b-mlx"})
        self.assertEqual(len(writes), 1)
        values = core.config_values(self.text())
        self.assertEqual((values["LLM_MODEL"], values["LLM_BASE_URL"]),
                         ("ternary-bonsai-2-27b-mlx", "http://127.0.0.1:8780/v1"))
        opconfig.set_values("wisp", {"LLM_MODEL": "gpu/bonsai2"})
        self.assertEqual(core.config_values(self.text())["LLM_BASE_URL"], "broker")

    def test_a_stale_edit_is_refused_and_names_the_value_now(self):
        with self.assertRaisesRegex(opconfig.Refused, "it is now 'medium' \\(you saw 'low'\\)"):
            opconfig.set_values("wisp", {"LLM_REASONING_EFFORT": "xhigh"}, {"LLM_REASONING_EFFORT": "low"})
        with self.assertRaisesRegex(opconfig.Refused, "it is now 'yes'"):
            opconfig.set_values("wisp", {"CUSTOM_KNOB": "no"}, {"CUSTOM_KNOB": None})
        self.assertEqual(self.text(), CONFIG)
        self.assertEqual(self.committed, [])

    def test_what_set_config_refuses_is_refused_here(self):
        for key, value, why in (("lower", "1", "UPPER_CASE"), ("X", 'say "hi"', "double quotes"),
                                ("X", "a\nLLM_MODEL=x", "one line"), ("LLM_MODEL", "gpt-9", "not a model"),
                                ("LLM_REASONING_EFFORT", "max", "must be one of low, medium, xhigh"),
                                ("WAKE_INTERVAL_MINUTES", "ten", "whole number"),
                                ("LLM_API_KEY", "sk-new", "looks like a secret"),
                                ("WATCH_GCHAT", "wakeup", "off, inbox or wake"),
                                ("WATCH_MAIL", "on", "off, inbox or wake")):
            with self.assertRaisesRegex(opconfig.Refused, why, msg=key):
                opconfig.set_values("wisp", {key: value})
        self.assertEqual(self.text(), CONFIG)

    def test_unset_removes_the_line_and_nothing_else(self):
        opconfig.set_values("wisp", {"CUSTOM_KNOB": None})
        self.assertEqual(self.text(), CONFIG.replace("CUSTOM_KNOB=yes\n", ""))

    def test_setting_what_is_already_there_writes_nothing(self):
        r = opconfig.set_values("wisp", {"LLM_REASONING_EFFORT": "medium", "NOT_THERE": None})
        self.assertEqual(r["changed"], [])
        self.assertEqual(self.text(), CONFIG)
        self.assertEqual(self.committed, [])

    def test_a_wake_below_the_floor_says_what_will_happen(self):
        # setUp supplies the operator's 5-minute floor.
        r = opconfig.set_values("wisp", {"WAKE_INTERVAL_MINUTES": "3"})
        self.assertIn("wake floor is 5 min (shaft limits): it wakes every 5 min, not 3", r["notes"][0])


class CommitFailsTest(OpConfigTest):
    def test_a_written_edit_whose_commit_fails_says_so_and_is_not_refused(self):
        """The file is written before the commit: a failing commit must not read as a refusal (M3 live check, 2026-10-19:
        git_wt raised SystemExit for a worktree the checkout did not know)."""
        import contextlib
        import io
        from supervisor import runner
        with mock.patch.object(runner, "run_lock", return_value=contextlib.nullcontext()), \
                mock.patch.object(runner, "_commit_outside_config", side_effect=SystemExit("not a registered worktree")):
            committed = REAL_COMMIT("wisp", self.root, "config")
        self.assertIn("written, not committed (not a registered worktree)", committed)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(opconfig, "set_values",
                                                                return_value={"changed": ["X"], "committed": committed,
                                                                              "notes": []}):
            code = opconfig.config_command("wisp", "set", ["X=1"], [], [])
        self.assertEqual(code, 0)
        self.assertIn("written, not committed", out.getvalue())


class SecretNamesTest(unittest.TestCase):
    def test_only_names_that_hold_a_secret_are_hidden(self):
        for key in ("LLM_API_KEY", "GITHUB_TOKEN", "KEY", "SMTP_PASSWORD", "MY_SECRET_THING"):
            self.assertTrue(opconfig.SECRET_KEY.search(key), key)
        for key in ("MAX_TOKENS_STEP", "MAX_TOKENS_TALK", "LLM_CONTEXT_TOKENS", "KEYBOARD", "MONKEY_COUNT"):
            self.assertFalse(opconfig.SECRET_KEY.search(key), key)


class FieldsTest(OpConfigTest):
    def test_every_key_with_its_help_choices_and_the_secret_masked(self):
        fs = {f["key"]: f for f in opconfig.fields("wisp")}
        self.assertEqual(list(fs)[:3], ["LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"])
        self.assertEqual(fs["LLM_API_KEY"]["value"], "(set, hidden)")
        self.assertNotIn("sk-secret-value", repr(fs))
        self.assertEqual(fs["LLM_REASONING_EFFORT"]["choices"], ["low", "medium", "xhigh"])
        self.assertIn("gpu/bonsai2", fs["LLM_MODEL"]["choices"])
        self.assertIn("ternary-bonsai-2-27b-mlx", fs["LLM_MODEL"]["choices"])
        self.assertEqual(fs["LLM_REASONING_EFFORT"]["help"], "How hard I think.")
        self.assertIn("(the operator, 2026-10-17 (floor 5))", fs["WAKE_INTERVAL_MINUTES"]["help"])
        self.assertTrue(fs["WAKE_INTERVAL_MINUTES"]["numeric"])
        self.assertTrue(fs["CUSTOM_KNOB"]["set"])                        # a key the template does not know

    def test_watch_keys_are_dropdowns_with_help_even_when_unset_20261110(self):
        fs = {f["key"]: f for f in opconfig.fields("wisp")}
        keys = ["WATCH_MAIL", "WATCH_CALENDAR", "WATCH_GCHAT"]
        self.assertEqual(list(fs)[-3:], keys)
        for key in keys:
            with self.subTest(key=key):
                f = fs[key]
                self.assertFalse(f["set"])
                self.assertEqual(f["default"], "off")
                self.assertEqual(f["choices"], ["off", "inbox", "wake"])
                self.assertFalse(f["numeric"])
                for value in ("off", "inbox", "wake"):
                    self.assertIn(value, f["help"])

    def test_an_operator_note_alone_keeps_the_known_help_and_is_shown_after_it_20261110(self):
        for key, value, help_text in (("WATCH_GCHAT", "wake", opconfig.WATCH_HELP["WATCH_GCHAT"]),
                                      ("VOICE_MODE", "on", opconfig.VOICE_HELP["VOICE_MODE"]),
                                      ("WAKE_INTERVAL_MINUTES", "120", "How often I wake.")):
            with self.subTest(key=key):
                (self.root / "config.env").write_text(
                    CONFIG + f'\n\n{key}="{value}"   # Daniele, 2026-11-10\n')
                fs = {f["key"]: f for f in opconfig.fields("wisp")}
                self.assertEqual(fs[key]["help"], help_text + " (Daniele, 2026-11-10)")

    def test_an_own_comment_block_still_wins_20261110(self):
        (self.root / "config.env").write_text('# my words\nWATCH_GCHAT="wake"  # Daniele\n')
        fs = {f["key"]: f for f in opconfig.fields("wisp")}
        self.assertEqual(fs["WATCH_GCHAT"]["help"], "my words (Daniele)")

    def test_a_parenthesised_comment_block_still_wins_20261110(self):
        (self.root / "config.env").write_text('# (my words)\nWATCH_MAIL="off"\n')
        fs = {f["key"]: f for f in opconfig.fields("wisp")}
        self.assertEqual(fs["WATCH_MAIL"]["help"], "(my words)")

    def test_a_set_watch_key_shows_its_value_and_an_unknown_one_stays_visible_20261110(self):
        from supervisor.web_config import _field_form
        (self.root / "config.env").write_text(
            CONFIG + '\nWATCH_GCHAT="wake"\n\n# Keep this help.\nWATCH_MAIL="wakeup"\n')
        fs = {f["key"]: f for f in opconfig.fields("wisp")}
        for key, value in (("WATCH_GCHAT", "wake"), ("WATCH_MAIL", "wakeup")):
            with self.subTest(key=key):
                self.assertEqual(fs[key]["value"], value)
                self.assertTrue(fs[key]["set"])
                self.assertEqual(fs[key]["choices"], ["off", "inbox", "wake"])
                self.assertFalse(fs[key]["numeric"])
        self.assertEqual(fs["WATCH_MAIL"]["help"], "Keep this help.")
        form = str(_field_form("wisp", fs["WATCH_MAIL"]))
        self.assertIn('<select name="value"', form)
        self.assertIn('<option value="wakeup" selected>wakeup</option>', form)
        for value in ("off", "inbox", "wake"):
            self.assertIn(f'<option value="{value}">{value}</option>', form)

    def test_a_comment_block_after_keys_starts_afresh(self):
        fs = {f["key"]: f for f in opconfig.fields("wisp")}
        self.assertNotIn("A local server", fs["LLM_REASONING_EFFORT"]["help"])


class DriveTest(OpConfigTest):
    def test_set_and_remove_with_compare_and_set(self):
        self.assertEqual(opconfig.drive("wisp"), (None, "none"))
        opconfig.set_drive("wisp", "Help the operator with his mail.", "none")
        text, sha = opconfig.drive("wisp")
        self.assertEqual(text, "Help the operator with his mail.\n")
        self.assertEqual(sha, hashlib.sha256(b"Help the operator with his mail.\n").hexdigest())
        with self.assertRaisesRegex(opconfig.Refused, "changed since you looked"):
            opconfig.set_drive("wisp", "Something else.", "none")
        with self.assertRaisesRegex(opconfig.Refused, "empty"):
            opconfig.set_drive("wisp", "  \n", sha)
        opconfig.set_drive("wisp", None, sha)
        self.assertFalse((self.root / "drive.md").exists())
        self.assertEqual(self.committed, ["drive", "drive"])


class RaceTest(unittest.TestCase):
    """The instance's set_config reads config.env, changes one line and writes it back; an operator edit landing in
    between was lost (the broker wrote its stale copy). Both now take core.config_lock."""

    def setUp(self):
        from supervisor.broker import Broker
        self.dir = Path(tempfile.mkdtemp(prefix="shaft-race-")).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = self.dir / "instances" / "wisp"
        (self.root / "shaft").mkdir(parents=True)
        (self.root / "config.env").write_text('LLM_REASONING_EFFORT="medium"\nWAKE_INTERVAL_MINUTES=120\n')
        for p in (mock.patch.object(core, "SUP_DIR", self.dir / "sup"),
                  mock.patch.object(opconfig, "instance_root", return_value=self.root),
                  mock.patch("supervisor.core.instance_root", return_value=self.root),
                  mock.patch.object(opconfig, "TEMPLATE", self.dir / "none"),
                  mock.patch.object(opconfig, "_commit", return_value=True), mock.patch.object(opconfig, "log")):
            p.start()
            self.addCleanup(p.stop)
        self.broker = Broker("wisp", self.dir, {}, self.dir / "broker.log")

    def test_an_operator_edit_during_set_config_is_kept(self):
        real = core.config_with
        operator = threading.Thread(target=lambda: opconfig.set_values("wisp", {"LLM_REASONING_EFFORT": "low"}))
        started = []

        def slow_config_with(text, key, line):            # set_config, between its read and its write
            if key == "WAKE_INTERVAL_MINUTES" and not started:
                started.append(1)
                operator.start()
                time.sleep(0.5)                            # without the lock the operator writes now
            return real(text, key, line)
        with mock.patch.object(core, "config_with", side_effect=slow_config_with):
            r = self.broker.call("set_config", {"key": "WAKE_INTERVAL_MINUTES", "value": "15"})
        operator.join(10)
        self.assertTrue(r["ok"], r)
        values = core.config_values((self.root / "config.env").read_text())
        self.assertEqual(values["WAKE_INTERVAL_MINUTES"], "15")          # the instance's line
        self.assertEqual(values["LLM_REASONING_EFFORT"], "low")          # and the operator's


class ThroughARealCycleTest(unittest.TestCase):
    """test_config_writers' real cycle (its harness, borrowed), with the operator editing through `shaft config`."""
    setUp = cw.ConfigWritersTest.setUp
    cycle = cw.ConfigWritersTest.cycle
    commits = cw.ConfigWritersTest.commits
    journal_entry = cw.ConfigWritersTest.journal_entry

    def edit(self, **changes):
        sup = self.project / ".supervisor"
        with mock.patch.object(opconfig, "instance_root", return_value=self.root), \
                mock.patch("supervisor.core.instance_root", return_value=self.root), \
                mock.patch("supervisor.runner.instance_root", return_value=self.root), \
                mock.patch("supervisor.runner.SUP_DIR", sup), mock.patch.object(core, "SUP_DIR", sup), \
                mock.patch.object(core, "PROJECT", self.project), mock.patch("supervisor.runner.PROJECT", self.project), \
                mock.patch("supervisor.cloudllm.CLOUD_MODELS", ["claude-opus-5-5"]), \
                mock.patch("supervisor.runner.log"), mock.patch.object(opconfig, "log"):
            return opconfig.set_values("kiln", changes)

    def test_an_edit_while_it_runs_is_its_runs_to_commit_as_the_operators_and_survives_a_failed_gate(self):
        result = {}
        self.cycle(cw.FAILING, lambda broker: result.update(self.edit(LLM_MODEL="claude-opus-5-5")))
        self.assertFalse(result["committed"])                                    # it was running
        config = (self.root / "config.env").read_text()
        self.assertIn('LLM_MODEL="claude-opus-5-5"', config)                     # not undone with its failed change
        self.assertNotIn('LLM_MODEL="bonsai"', config)
        self.assertFalse((self.root / "plugins" / "text_stats" / "main.py").exists())
        self.assertNotIn("config.env", self.journal_entry("Supervisor: code change reverted"))
        outside = [c for c in self.commits() if "config.env changed from outside the instance" in c]
        self.assertEqual(len(outside), 1)
        self.assertIn("LLM_MODEL: bonsai -> claude-opus-5-5", outside[0])
        self.assertEqual(cw.git(self.root, "status", "--porcelain", "--", "config.env"), "")

    def test_an_edit_while_it_is_free_is_committed_at_once_and_update_is_not_blocked(self):
        r = self.edit(LLM_MODEL="claude-opus-5-5")
        self.assertTrue(r["committed"])
        self.assertEqual(cw.git(self.root, "status", "--porcelain", "--untracked-files=no"), "")   # cmd_update's test
        head = cw.git(self.root, "log", "-1", "--format=%an|%B")
        self.assertTrue(head.startswith("SHAFT supervisor|kiln: config.env changed from outside the instance"))
        self.assertIn("LLM_MODEL: bonsai -> claude-opus-5-5", head)
        self.assertIn("LLM_BASE_URL", cw.git(self.root, "show", "HEAD:config.env"))


if __name__ == "__main__":
    unittest.main()


class LinkedWorktreeTest(unittest.TestCase):
    def test_the_supervisor_run_from_a_linked_worktree_finds_the_instances(self):
        """Live, 2026-10-19: run from a branch's worktree, every git_wt call raised "not a registered worktree" —
        the supervisor's .git is a file there, and the instances are registered in the main repository."""
        base = Path(tempfile.mkdtemp(prefix="shaft-linked-")).resolve()
        self.addCleanup(shutil.rmtree, base, True)
        main = base / "main"
        main.mkdir()
        cw.git(main, "init", "-q")
        cw.git(main, "commit", "-q", "--allow-empty", "-m", "main")
        branch = main / ".claude" / "worktrees" / "b"
        cw.git(main, "worktree", "add", "-q", "-b", "b", str(branch))
        inst = main / "instances" / "kiln"
        cw.git(main, "worktree", "add", "-q", "--orphan", "-b", "instance/kiln", str(inst))
        self.assertTrue((branch / ".git").is_file())
        self.assertEqual(core._git_common_dir(branch), (main / ".git").resolve())
        self.assertEqual(core._git_common_dir(main), main / ".git")
        admin = core._worktree_admin_dir(inst, branch)                 # it raised SystemExit before
        self.assertEqual(admin, core._worktree_admin_dir(inst, main))


class ModelEffortsTest(OpConfigTest):
    def test_sol_was_offered_only_medium_xhigh_low(self):
        from supervisor import codexcli
        levels = ("low", "medium", "high", "xhigh", "max", "ultra")
        with mock.patch.object(codexcli, "_effort_listing", return_value={
                "gpt-6.1-sol": {"levels": list(levels), "default": "low"}}) as listing:
            (self.root / "shaft/config.py").write_text(f'CHOICES = {{"LLM_REASONING_EFFORT": {levels!r}}}')
            self.assertEqual(opconfig.effort_choices(self.root, "codex/gpt-6.1-sol"), levels)
            self.assertEqual(opconfig.model_efforts("codex/gpt-6.1-sol"), (levels, "low"))
            listing.assert_called_once_with()
            self.assertEqual(opconfig.effort_choices(self.root, "ternary-bonsai-2-27b-mlx"),
                             ("low", "medium", "xhigh"))
            self.assertEqual(opconfig.model_efforts("gpu/bonsai2"), (("low", "medium", "xhigh"), "xhigh"))
            (self.root / "shaft/config.py").write_text(AGENT_CONFIG)
            self.assertEqual(opconfig.effort_choices(self.root, "codex/gpt-6.1-sol"), ("low", "medium", "xhigh"))

    def test_openrouter_no_levels_canonical_order_and_forbidden_levels(self):
        import io
        from supervisor import cloudllm, effortcatalog
        (self.root / "shaft/config.py").write_text(
            'CHOICES = {"LLM_REASONING_EFFORT": ("low", "high", "max", "none", "minimal")}')
        payload = {"data": [{"id": "xiaomi/mimo-v2.6-pro"},
                            {"id": "z-ai/glm", "reasoning": {"supported_efforts":
                             ["max", "high", "low", "none", "minimal"], "default_effort": "high"}}]}
        # Remove the fixture's synthetic listing: exercise the HTTP response parser.
        with mock.patch.object(cloudllm, "_effort_listing", side_effect=lambda: real_listing()), \
                mock.patch.object(cloudllm.urllib.request, "urlopen", return_value=io.StringIO(json.dumps(payload))) as get:
            self.assertEqual(opconfig.effort_choices(self.root, "openrouter/xiaomi/mimo-v2.6-pro"), ())
            self.assertEqual(opconfig.effort_choices(self.root, "openrouter/z-ai/glm"), ("low", "high", "max"))
            self.assertEqual(opconfig.model_efforts("openrouter/z-ai/glm"), (("low", "high", "max"), "high"))
            get.assert_called_once_with("https://openrouter.ai/api/v1/models", timeout=10)
        self.assertEqual(effortcatalog.entry(["minimal", "none"], "none"), {"levels": [], "default": None})

    def test_live_cache_fresh_stale_failure_and_missing_failure(self):
        from datetime import datetime, timezone
        from supervisor import codexcli, effortcatalog
        path = core.SUP_DIR / "effort_catalog.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        catalog = {"codex": {"fetched_at": datetime.now(timezone.utc).isoformat(),
                             "models": {"gpt-6.1-sol": {"levels": ["low", "max"], "default": "low"}}}}
        path.write_text(json.dumps(catalog))
        with mock.patch.object(codexcli, "_effort_listing", side_effect=OSError("offline")) as fetch, \
                mock.patch.object(core, "log") as log:
            self.assertEqual(opconfig.model_efforts("codex/gpt-6.1-sol"), (("low", "max"), "low"))
            fetch.assert_not_called()
            catalog["codex"]["fetched_at"] = "2000-01-01T00:00:00+00:00"
            path.write_text(json.dumps(catalog))
            self.assertEqual(opconfig.model_efforts("codex/gpt-6.1-sol"), (("low", "max"), "low"))
            self.assertEqual(json.loads(path.read_text()), catalog)
            self.assertEqual(opconfig.model_efforts("codex/gpt-6.1-sol"), (("low", "max"), "low"))
            fetch.assert_called_once_with()
            log.assert_called_once()
            path.unlink()
            effortcatalog._failures.clear()
            self.assertIsNone(opconfig.model_efforts("codex/gpt-6.1-sol"))

    def test_fixed_provider_tables_and_unknown_models(self):
        expected = {
            "claude-haiku-5-5": (("low", "medium", "high", "xhigh", "max"), None),
            "claudecode/claude-opus-5-5": (("low", "medium", "high", "xhigh", "max"), None),
            "antigravity/gemini-4-argon": (("low", "medium", "high"), "medium"),
            "grok/grok-4.7": (("low", "medium", "high", "xhigh"), None)}
        for model, result in expected.items():
            with self.subTest(model=model):
                self.assertEqual(opconfig.model_efforts(model), result)
        self.assertIsNone(opconfig.model_efforts("unknown/model"))
        self.assertIsNone(opconfig.effort_choices(self.root, None))

    def test_refresh_preserves_other_provider_catalog_and_missing_model_is_unknown(self):
        from supervisor import codexcli
        path = core.SUP_DIR / "effort_catalog.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        other = {"fetched_at": "2000-01-01T00:00:00+00:00", "models": {}}
        path.write_text(json.dumps({"openrouter": other}))
        with mock.patch.object(codexcli, "_effort_listing", return_value={
                "gpt-6.1-sol": {"levels": ["low", "max"], "default": "low"}}) as fetch:
            self.assertEqual(opconfig.model_efforts("codex/gpt-6.1-sol"), (("low", "max"), "low"))
            self.assertIsNone(opconfig.model_efforts("codex/not-listed"))
            fetch.assert_called_once_with()
        saved = json.loads(path.read_text())
        self.assertEqual(saved["openrouter"], other)
        self.assertEqual(saved["codex"]["models"]["gpt-6.1-sol"], {"levels": ["low", "max"], "default": "low"})
        self.assertNotEqual(saved["codex"]["fetched_at"], other["fetched_at"])

    def test_set_validates_both_efforts_for_the_effective_model(self):
        from supervisor import codexcli
        (self.root / "shaft/config.py").write_text(
            'CHOICES = {"LLM_REASONING_EFFORT": ("low", "medium", "high", "xhigh", "max", "ultra")}')
        (self.root / "config.env").write_text('LLM_MODEL="ternary-bonsai-2-27b-mlx"\n')
        for key in ("LLM_REASONING_EFFORT", "VOICE_REASONING_EFFORT"):
            with self.assertRaisesRegex(opconfig.Refused, "shaft/config.py and ternary-bonsai"):
                opconfig.set_values("wisp", {key: "high"})
        with mock.patch.object(codexcli, "_effort_listing", return_value={
                "gpt-6.1-sol": {"levels": ["low", "high", "max", "ultra"], "default": "low"}}):
            opconfig.set_values("wisp", {"LLM_REASONING_EFFORT": "ultra", "VOICE_REASONING_EFFORT": "max",
                                         "LLM_MODEL": "codex/gpt-6.1-sol"})
        self.assertIn('LLM_REASONING_EFFORT="ultra"', self.text())
        self.assertIn('VOICE_REASONING_EFFORT="max"', self.text())

    def test_bakeoff_uses_each_arms_model_intersection(self):
        from supervisor import bakeoff, codexcli
        (self.root / "shaft/config.py").write_text(
            'CHOICES = {"LLM_REASONING_EFFORT": ("low", "medium", "high", "xhigh", "max", "ultra")}')
        with mock.patch.object(codexcli, "_effort_listing", return_value={
                "gpt-6.1-sol": {"levels": ["low", "medium", "high", "xhigh", "max", "ultra"], "default": "low"}}):
            self.assertEqual(bakeoff.parse_arm("codex/gpt-6.1-sol@max", opconfig.effort_choices(
                self.root, "codex/gpt-6.1-sol"))["env"], {"LLM_REASONING_EFFORT": "max"})
            with self.assertRaisesRegex(bakeoff.BakeoffError, "claude-haiku-5-5"):
                bakeoff.parse_arm("claude-haiku-5-5@ultra", opconfig.effort_choices(
                    self.root, "claude-haiku-5-5"))
        # Exercise create's per-arm lookup before any replay or state preparation.
        with mock.patch.object(opconfig, "effort_choices", side_effect=[("max",), ("low",)]) as choices:
            with self.assertRaises(bakeoff.BakeoffError):
                bakeoff.create("wisp", [" codex/gpt-6.1-sol @max", "claude-haiku-5-5@ultra"], [])
            self.assertEqual([call.args[1] for call in choices.call_args_list],
                             ["codex/gpt-6.1-sol", "claude-haiku-5-5"])

    def test_codex_catalog_reuses_launch_and_initialize_without_weekly_rate_check(self):
        from supervisor import codexcli
        home = self.dir / "codex-home"
        home.mkdir()
        (home / "auth.json").write_text("{}")
        channel = codexcli._CatalogChannel("effort-catalog", "effort-catalog", lambda message: None)
        channel.deadline = time.monotonic() + 15
        with mock.patch.object(codexcli, "HOME", home), \
                mock.patch.object(codexcli, "_prune_logs"), \
                mock.patch.object(codexcli, "_seatbelt", side_effect=lambda argv, *args: argv), \
                mock.patch.object(codexcli.subprocess, "Popen") as launch, \
                mock.patch.object(channel, "_start_workers"), \
                mock.patch.object(channel, "_request", return_value={}) as request, \
                mock.patch.object(channel, "_send") as send:
            channel._start_server()
            self.addCleanup(shutil.rmtree, channel._folder, True)
            self.assertEqual(launch.call_args.args[0][0], codexcli.BIN)
            self.assertEqual(launch.call_args.args[0][-1], "app-server")
            self.assertEqual(launch.call_args.kwargs["env"]["CODEX_HOME"], str(home))
            request.assert_called_once_with("initialize", {"clientInfo": {"name": "shaft", "version": "1"},
                                                          "capabilities": {"experimentalApi": True}})
            send.assert_called_once_with({"method": "initialized"})

    def test_codex_listing_initializes_paginates_and_closes(self):
        from supervisor import codexcli
        responses = [{"data": [{"model": "gpt-6.1-sol", "supportedReasoningEfforts": [
            {"reasoningEffort": level, "description": "effort"} for level in ("ultra", "low", "minimal")],
            "defaultReasoningEffort": "low"}], "nextCursor": "page2"},
            {"data": [{"model": "gpt-6-luna", "supportedReasoningEfforts": [
                {"reasoningEffort": "medium", "description": "effort"}], "defaultReasoningEffort": "medium"}]}]
        with mock.patch.object(codexcli._CatalogChannel, "_start_server") as start, \
                mock.patch.object(codexcli._CatalogChannel, "_request", side_effect=responses) as request, \
                mock.patch.object(codexcli._CatalogChannel, "close") as close:
            self.assertEqual(codexcli._effort_listing(), {
                "gpt-6.1-sol": {"levels": ["low", "ultra"], "default": "low"},
                "gpt-6-luna": {"levels": ["medium"], "default": "medium"}})
            start.assert_called_once_with()
            self.assertEqual(request.call_args_list, [mock.call("model/list", {"includeHidden": True}),
                mock.call("model/list", {"includeHidden": True, "cursor": "page2"})])
            close.assert_called_once_with()
        with mock.patch.object(codexcli._CatalogChannel, "_start_server", side_effect=OSError("offline")), \
                mock.patch.object(codexcli._CatalogChannel, "close") as close:
            with self.assertRaises(OSError):
                codexcli._effort_listing()
            close.assert_called_once_with()

    def test_known_empty_intersection_is_exposed_and_refuses_explicit_effort(self):
        with mock.patch.object(opconfig, "model_efforts", return_value=((), None)):
            fields = {f["key"]: f for f in opconfig.fields("wisp")}
            self.assertEqual(fields["LLM_REASONING_EFFORT"]["choices"], [])
            self.assertEqual(fields["VOICE_REASONING_EFFORT"]["choices"], [""])
            with self.assertRaises(opconfig.Refused):
                opconfig.set_values("wisp", {"LLM_REASONING_EFFORT": "low"})
        self.assertIsNone(opconfig.model_efforts("unknown/model"))

    def test_a_model_with_no_levels_says_so_instead_of_an_empty_list(self):
        """Bug guarded: OpenRouter mimo reports no levels, and the refusal read "must be one of  (…)"."""
        mimo = "openrouter/xiaomi/mimo-v2.6-pro"
        (self.root / "config.env").write_text(CONFIG.replace("openrouter/openai/gpt-6.1-sol", mimo))
        self.assertEqual(opconfig.effort_choices(self.root, mimo), ())
        for key in ("LLM_REASONING_EFFORT", "VOICE_REASONING_EFFORT"):
            with self.assertRaisesRegex(opconfig.Refused, f"{mimo} takes no reasoning effort .*: unset {key}"):
                opconfig.set_values("wisp", {key: "medium"})
        opconfig.set_values("wisp", {"LLM_REASONING_EFFORT": None})   # unsetting is what it offers
        self.assertNotIn("LLM_REASONING_EFFORT=", self.text())
