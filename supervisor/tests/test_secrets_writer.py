"""core.set_secret_env, the one atomic writer of the project .env, and `shaft secrets`."""
from __future__ import annotations

import io
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from supervisor import core, opsecrets


class SecretsWriterTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="shaft-secrets-")).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env = self.dir / ".env"
        for p in (mock.patch.object(core, "PROJECT", self.dir), mock.patch.object(core, "SUP_DIR", self.dir / ".supervisor")):
            p.start()
            self.addCleanup(p.stop)

    def run_cli(self, action, name=None, stdin="", as_json=True):
        with mock.patch("sys.stdin", io.StringIO(stdin)), mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = opsecrets.command(action, name, as_json)
        return code, out.getvalue(), err.getvalue()

    def test_set_keeps_other_lines_and_position(self):
        self.env.write_text("# keys\nFOO=bar\nANTHROPIC_API_KEY=old\n\nOTHER = 'x y'\nSHAFT_API_KEY=a")
        core.set_secret_env("ANTHROPIC_API_KEY", "new")
        self.assertEqual(self.env.read_text(),
                         "# keys\nFOO=bar\nANTHROPIC_API_KEY=new\n\nOTHER = 'x y'\nSHAFT_API_KEY=a")
        core.set_secret_env("OPENROUTER_API_KEY", "k")                   # new key: last
        self.assertTrue(self.env.read_text().endswith("SHAFT_API_KEY=a\nOPENROUTER_API_KEY=k\n"))
        core.set_secret_env("OPENROUTER_API_KEY", None)
        self.assertNotIn("OPENROUTER", self.env.read_text())
        self.assertEqual(core.secrets_env()["ANTHROPIC_API_KEY"], "new")

    def test_spaced_and_duplicated_lines_are_matched_as_the_reader_parses_them(self):
        self.env.write_text("A=1\n  SHAFT_API_KEY = old1\n# SHAFT_API_KEY=comment\nB=2\nSHAFT_API_KEY=old2\n")
        core.set_secret_env("SHAFT_API_KEY", "new")
        self.assertEqual(self.env.read_text(), "A=1\nSHAFT_API_KEY=new\n# SHAFT_API_KEY=comment\nB=2\n")
        self.env.write_text("A=1\nSHAFT_API_KEY = old1\nSHAFT_API_KEY=old2\n")
        core.set_secret_env("SHAFT_API_KEY", None)
        self.assertNotIn("SHAFT_API_KEY", core.secrets_env())
        self.assertEqual(self.env.read_text(), "A=1\n")
        self.assertEqual(self.run_cli("unset", "SHAFT_API_KEY\n")[0], 1)

    def test_list_read_failure_is_a_json_error(self):
        with mock.patch.object(core, "secrets_env", side_effect=PermissionError("denied")):
            code, out, _ = self.run_cli("list")
        self.assertEqual(code, 1)
        self.assertIn("denied", json.loads(out)["error"])

    def test_registered_parser_dispatches_set(self):
        from supervisor import __main__ as m
        with mock.patch("sys.stdin", io.StringIO("sk-ant-Z\n")), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = m.main(["secrets", "set", "ANTHROPIC_API_KEY", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()), {"ok": True, "name": "ANTHROPIC_API_KEY"})
        self.assertEqual(core.secrets_env()["ANTHROPIC_API_KEY"], "sk-ant-Z")

    def test_concurrent_writers_lose_nothing(self):
        def writer(tag):
            for i in range(50):
                core.set_secret_env(f"TELEGRAM_TOKEN_{tag}{i}", f"v{tag}{i}")
        ts = [threading.Thread(target=writer, args=(t,)) for t in "AB"]
        [t.start() for t in ts]
        [t.join() for t in ts]
        env = core.secrets_env()
        self.assertEqual(len(env), 100)
        self.assertEqual(env["TELEGRAM_TOKEN_B49"], "vB49")

    def test_mode_is_0600(self):
        self.env.write_text("A=1\n")
        self.env.chmod(0o644)
        core.set_secret_env("SHAFT_API_KEY", "k")
        self.assertEqual(oct(self.env.stat().st_mode & 0o777), "0o600")
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.startswith(".env.")], [])   # no temp left

    def test_a_name_outside_the_list_is_refused(self):
        for bad in ("PATH", "telegram_token_x", "TELEGRAM_TOKEN_", "SHAFT_API_KEY\nX", "SHAFT_API_KEY\n", "GMAIL_X_CLIENT_TOKEN"):
            with self.assertRaises(ValueError, msg=bad):
                core.set_secret_env(bad, "v")
        with self.assertRaises(ValueError):
            core.set_secret_env("SHAFT_API_KEY", "two\nlines")
        self.assertFalse(self.env.exists())
        code, out, _ = self.run_cli("set", "PATH", "v\n")
        self.assertEqual(code, 1)
        self.assertIn("PATH", json.loads(out)["error"])

    def test_value_is_read_from_stdin_and_never_echoed(self):
        code, out, err = self.run_cli("set", "OPENROUTER_API_KEY", "sk-or-SECRETVALUE\n")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"ok": True, "name": "OPENROUTER_API_KEY"})
        self.assertNotIn("SECRETVALUE", out + err)
        self.assertEqual(core.secrets_env()["OPENROUTER_API_KEY"], "sk-or-SECRETVALUE")
        code, out, _ = self.run_cli("set", "OPENROUTER_API_KEY", "")
        self.assertEqual(code, 1)
        self.assertIn("no value", json.loads(out)["error"])
        code, out, _ = self.run_cli("unset", "OPENROUTER_API_KEY")
        self.assertEqual(code, 0)
        self.assertNotIn("OPENROUTER_API_KEY", core.secrets_env())
        self.assertEqual(self.run_cli("set", None)[0], 2)

    def test_list_prints_names_only(self):
        self.env.write_text("ANTHROPIC_API_KEY=sk-ant-HIDDEN\nUNRELATED=HIDDEN2\nTELEGRAM_TOKEN_WISP=123:HIDDEN3\n")
        code, out, err = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"ok": True, "names": ["ANTHROPIC_API_KEY", "TELEGRAM_TOKEN_WISP"]})
        self.assertNotIn("HIDDEN", out + err)


if __name__ == "__main__":
    unittest.main()
