"""The supervisor's settings (PLAN-webui-chat.md §4, S0): knob() reads env > .supervisor/settings.env > default,
`shaft settings` writes the file with the knob's own check and compare-and-set, and each service's record says which
saved value it does not hold yet.

The bug this fixes: the launchd services run with PATH alone in their environment, so every knob was its default in
every service — SHAFT_JUDGE_MODELS could not be set anywhere that lasted (Daniele, 2026-10-19)."""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from supervisor import core, settings, version
from supervisor.core import PROJECT, PYTHON

settings.load_all()


class FileCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="shaft-settings-"))
        self.file = self.dir / "settings.env"
        for p in (mock.patch.object(core, "SETTINGS_FILE", self.file), mock.patch.object(core, "SUP_DIR", self.dir),
                  mock.patch.object(settings, "log")):
            p.start()
            self.addCleanup(p.stop)
        core._file_cache.update(stamp=None, values={})
        self.addCleanup(core._file_cache.update, stamp=None, values={})

    def write(self, text: str) -> None:
        self.file.write_text(text)
        core._file_cache.update(stamp=None, values={})


class PrecedenceTest(FileCase):
    K = core.Knob("SHAFT_TEST_X", "5", core.positive_int, "test", "Runs and conversations", ("runs",), True)

    def test_env_then_file_then_default(self):
        self.assertEqual(core.resolve(self.K, env={}), (5, "default", "5"))
        self.write("SHAFT_TEST_X=7\n")
        self.assertEqual(core.resolve(self.K, env={}), (7, "file", "7"))
        self.assertEqual(core.resolve(self.K, env={"SHAFT_TEST_X": "9"}), (9, "env", "9"))

    def test_a_bad_value_is_warned_about_and_the_next_layer_used(self):
        self.write("SHAFT_TEST_X=-1\n")
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(core.resolve(self.K, env={"SHAFT_TEST_X": "lots"}), (5, "default", "5"))
        self.assertIn("SHAFT_TEST_X='lots' in the environment is invalid", err.getvalue())
        self.assertIn("SHAFT_TEST_X='-1'", err.getvalue())

    def test_the_file_value_is_verbatim_and_comments_are_skipped(self):
        k = core.Knob("SHAFT_TEST_J", "{}", core.json_object, "test", "Local model", ("runs",), True)
        self.write('# a comment\nSHAFT_TEST_J={"a": "#not a comment"}\n')
        self.assertEqual(core.resolve(k, env={})[0], {"a": "#not a comment"})

    def test_a_launchd_service_reads_the_file_with_no_environment(self):
        """The fix itself: a process started as launchd starts the services (PATH alone) holds the file's value."""
        sup = self.dir / "sup"
        sup.mkdir()
        (sup / "settings.env").write_text("SHAFT_JUDGE_MODELS=claude-opus-5-5,openrouter/z-ai/glm-5.3-flash\n")
        out = subprocess.run([PYTHON, "-E", "-s", "-c", "from supervisor import judge; print(judge.JUDGE_MODELS, "
                              "judge.JUDGE_SRC)"], cwd=PROJECT, capture_output=True, text=True, timeout=60,
                             env={"PATH": os.environ.get("PATH", ""), "SHAFT_SUPERVISOR_DIR": str(sup)})
        self.assertEqual(out.stdout.strip(), "['claude-opus-5-5', 'openrouter/z-ai/glm-5.3-flash'] file", out.stderr)


class SetTest(FileCase):
    def test_set_writes_one_line_and_keeps_the_rest(self):
        self.write("# mine\nSHAFT_IDLE_MINUTES=20\n")
        r = settings.set_values({"SHAFT_JUDGE_MODELS": "claude-sonnet-5-5", "SHAFT_IDLE_MINUTES": "45"},
                                {"SHAFT_IDLE_MINUTES": "20", "SHAFT_JUDGE_MODELS": None})
        self.assertEqual(r["changed"], ["SHAFT_IDLE_MINUTES", "SHAFT_JUDGE_MODELS"])
        self.assertEqual(self.file.read_text(), "# mine\nSHAFT_IDLE_MINUTES=45\nSHAFT_JUDGE_MODELS=claude-sonnet-5-5\n")
        settings.set_values({"SHAFT_IDLE_MINUTES": None})
        self.assertEqual(self.file.read_text(), "# mine\nSHAFT_JUDGE_MODELS=claude-sonnet-5-5\n")

    def test_a_bad_value_is_refused_with_the_reason_and_nothing_is_written(self):
        for key, value, why in (("SHAFT_JUDGE_MODELS", "gpt-5", "claude-… or openrouter/…"),
                                ("SHAFT_JUDGE_MODELS", "", "at least one model"),
                                ("SHAFT_IDLE_MINUTES", "0", "above 0"),
                                ("SHAFT_CLOUD_DAILY_USD", "lots", "lots"),
                                ("SHAFT_OPENROUTER_PROVIDER", "[1]", "JSON object"),
                                ("SHAFT_JUDGE2_MODEL", "claude-sonnet-5-5", "openrouter/"),
                                ("SHAFT_CODER_MODEL", "claude-unknown-9", "no price"),
                                ("SHAFT_NO_SUCH", "1", "not a setting"),
                                ("SHAFT_IDLE_MINUTES", "1\nSHAFT_X=2", "one line")):
            with self.subTest(key=key, value=value):
                with self.assertRaises(settings.Refused) as e:
                    settings.set_values({"SHAFT_IDLE_MINUTES": "40", key: value})
                self.assertIn(why, str(e.exception))
                self.assertFalse(self.file.exists())       # not even the good one: one write or none

    def test_expect_refuses_a_stale_value_and_writes_nothing(self):
        self.write("SHAFT_JUDGE_MODELS=claude-sonnet-5-5\n")
        for saw in ("claude-opus-5-5", None):
            with self.subTest(saw=saw), self.assertRaises(settings.Refused) as e:
                settings.set_values({"SHAFT_JUDGE_MODELS": "claude-haiku-5-5"}, {"SHAFT_JUDGE_MODELS": saw})
            self.assertIn("changed since you looked: it is now 'claude-sonnet-5-5'", str(e.exception))
        self.assertEqual(self.file.read_text(), "SHAFT_JUDGE_MODELS=claude-sonnet-5-5\n")

    def test_the_web_cannot_set_ports_paths_or_endpoints(self):
        for key in ("SHAFT_WEB_PORT", "SHAFT_API_PORT", "SHAFT_LLM_PORTS", "SHAFT_TELEGRAM_API", "SHAFT_FT_PYTHON",
                    "SHAFT_WEB_HOSTS"):
            with self.subTest(key=key), self.assertRaises(settings.Refused) as e:
                settings.set_values({key: core.KNOBS[key].default}, from_web=True)
            self.assertIn("terminal only", str(e.exception))
        settings.set_values({"SHAFT_WEB_PORT": "8786"})                 # from a terminal it may
        self.assertEqual(settings.file_values(), {"SHAFT_WEB_PORT": "8786"})

    def test_the_cli_verb(self):
        def run(*argv):
            with redirect_stdout(io.StringIO()) as out:
                code = settings.command(*argv)
            return code, out.getvalue()
        code, out = run("set", ["SHAFT_JUDGE_MODELS=claude-sonnet-5-5"], [], ["SHAFT_JUDGE_MODELS"])
        self.assertEqual(code, 0, out)
        self.assertIn("saved SHAFT_JUDGE_MODELS", out)
        code, out = run("set", ["SHAFT_JUDGE_MODELS=claude-opus-5-5"], [], ["SHAFT_JUDGE_MODELS"])
        self.assertEqual((code, out.split(":")[0]), (1, "refused"))
        code, out = run(None, [], [], [], True)
        listed = {f["name"]: f for f in json.loads(out)["settings"]}
        self.assertEqual((listed["SHAFT_JUDGE_MODELS"]["value"], listed["SHAFT_JUDGE_MODELS"]["source"]),
                         ("claude-sonnet-5-5", "file"))
        self.assertEqual(listed["SHAFT_JUDGE_MODELS"]["kind"], "models")
        self.assertIn("openrouter/xiaomi/mimo-v2.6-pro", listed["SHAFT_JUDGE_MODELS"]["choices"])
        self.assertFalse(listed["SHAFT_WEB_PORT"]["web"])
        code, out = run("unset", ["SHAFT_JUDGE_MODELS"], ["SHAFT_JUDGE_MODELS=claude-sonnet-5-5"], [])
        self.assertEqual(code, 0, out)
        self.assertNotIn("SHAFT_JUDGE_MODELS", settings.file_values())


class NotLiveTest(FileCase):
    def test_a_service_holding_another_value_must_restart(self):
        self.write("SHAFT_JUDGE_MODELS=claude-sonnet-5-5\nSHAFT_IDLE_MINUTES=45\n")
        held = {"SHAFT_JUDGE_MODELS": {"raw": core.KNOBS["SHAFT_JUDGE_MODELS"].default, "source": "default"},
                "SHAFT_IDLE_MINUTES": {"raw": "45", "source": "file"},
                "SHAFT_CYCLE_TIMEOUT_S": {"raw": "999", "source": "env"}}       # its own environment: not ours
        late = settings.not_live({"api": {"knobs": held}, "scheduler": None, "web": {"knobs": {}}})
        self.assertEqual(late, {"SHAFT_JUDGE_MODELS": ["api"]})

    def test_the_same_value_written_differently_is_live(self):
        self.write("SHAFT_CLOUD_DAILY_USD=8.0\n")
        held = {"SHAFT_CLOUD_DAILY_USD": {"raw": "8", "source": "default"}}
        self.assertEqual(settings.not_live({"web": {"knobs": held}}), {})

    def test_a_service_started_before_records_held_knobs_holds_the_defaults_it_reads(self):
        self.write("SHAFT_JUDGE_MODELS=claude-sonnet-5-5\nSHAFT_JUDGE_ALERT_USD=20\n")
        late = settings.not_live({"api": {"pid": 1}, "web": {"pid": 2}, "telegram": {"pid": 3}})
        self.assertEqual(late, {"SHAFT_JUDGE_MODELS": ["api", "web"], "SHAFT_JUDGE_ALERT_USD": ["web"]})

    def test_a_service_records_the_knobs_it_reads_even_after_start_up(self):
        with mock.patch.object(version, "SERVICES", self.dir / "services"), \
                mock.patch.object(version, "supervisor_commit", return_value="abc"), \
                mock.patch.dict(core.HELD, {}, clear=True), mock.patch.object(core, "_on_knob", None):
            core.knob("SHAFT_TEST_A", "1", int)
            version.record_service("api", {})
            self.assertEqual(self._knobs(), {"SHAFT_TEST_A": {"raw": "1", "source": "default"}})
            self.write("SHAFT_TEST_B=3\n")
            core.knob("SHAFT_TEST_B", "2", int)                            # a module imported on first use
            self.assertEqual(self._knobs()["SHAFT_TEST_B"], {"raw": "3", "source": "file"})

    def _knobs(self) -> dict:
        return json.loads((self.dir / "services" / "api.json").read_text())["knobs"]


class RegistryTest(unittest.TestCase):
    """Every knob() in the code is registered with what it does, where it shows and who reads it."""

    def calls(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for path in (PROJECT / "supervisor").glob("*.py"):
            for name in re.findall(r'\bknob\(\s*(?:#[^\n]*\n\s*)?"(SHAFT_[A-Z0-9_]+)"', path.read_text()):
                out.setdefault(name, set()).add(path.stem)
        return out

    def test_every_knob_call_is_registered_with_a_description(self):
        calls = self.calls()
        self.assertGreater(len(calls), 50)
        for name, modules in calls.items():
            with self.subTest(knob=name):
                self.assertTrue(modules <= set(settings.MODULES), f"{name} in {modules}: add it to settings.MODULES")
                k = core.KNOBS.get(name)
                self.assertIsNotNone(k, f"{name} is not registered")
                self.assertGreater(len(k.help), 15, f"{name} has no description")
                self.assertIn(k.group, core.KNOB_GROUPS)
                self.assertTrue(k.services and set(k.services) <= {"runs", *core.SERVICE_NAMES}, k.services)
        self.assertEqual(set(core.KNOBS), set(calls), "a registered knob no code reads")

    def test_every_default_passes_its_own_check(self):
        for k in core.KNOBS.values():
            with self.subTest(knob=k.name):
                k.parse(k.default)

    def test_no_setting_is_read_around_knob(self):
        """A SHAFT_ variable read straight from the environment is one the settings file cannot set."""
        allowed = {"SHAFT_SUPERVISOR_DIR", "SHAFT_API_LOGINS"}      # where state lives; a login list kept in .env
        for path in (PROJECT / "supervisor").glob("*.py"):
            for name in re.findall(r'environ(?:\.get\(|\[)\s*"(SHAFT_[A-Z0-9_]+)"', path.read_text()):
                self.assertIn(name, allowed, f"{path.name} reads {name} from the environment: make it a knob()")

    def test_terminal_only_settings(self):
        cli_only = {k.name for k in core.KNOBS.values() if not k.web}
        self.assertTrue({"SHAFT_WEB_PORT", "SHAFT_WEB_HTTPS_PORT", "SHAFT_API_PORT", "SHAFT_API_HTTPS_PORT",
                         "SHAFT_LLM_PORTS", "SHAFT_EMBED_PORT", "SHAFT_TELEGRAM_API", "SHAFT_FT_PYTHON",
                         "SHAFT_BONSAI_PYTHON", "SHAFT_WEB_HOSTS"} <= cli_only)
        self.assertNotIn("SHAFT_JUDGE_MODELS", cli_only)


if __name__ == "__main__":
    unittest.main()
