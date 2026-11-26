"""The operator's Shortcuts through the broker (supervisor/shortcuts.py, broker shortcut_list / shortcut_run): only a
granted shortcut runs, by identifier, never in a replay, and broker.log never keeps what went in or came out. A fake
`shortcuts` CLI stands in for /usr/bin/shortcuts, so the real subprocess path is what runs."""
from __future__ import annotations

import io
import os
import shutil
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

from supervisor import broker, core, shortcuts
from supervisor.__main__ import main
from supervisor.tests import test_app_api

INSTANCE = "wisp"
ECHO, OTHER, SLOW, IMAGE = ("AAAAAAAA-0000-4000-8000-000000000001", "BBBBBBBB-0000-4000-8000-000000000002",
                            "CCCCCCCC-0000-4000-8000-000000000003", "DDDDDDDD-0000-4000-8000-000000000004")
FAKE = r"""#!/bin/sh
# a stand-in for /usr/bin/shortcuts: list prints $LISTING, run records its id in $RAN and acts by id
if [ "$1" = list ]; then cat "$LISTING"; exit 0; fi
id=$2; shift 2; inp=""; out=""
while [ $# -gt 0 ]; do case $1 in --input-path) inp=$2; shift 2;; --output-path) out=$2; shift 2;; *) shift;; esac; done
echo "$id" >> "$RAN"
case $id in
  AAAAAAAA*) if [ -n "$inp" ]; then cat "$inp" > "$out"; else printf 'no input' > "$out"; fi;;
  CCCCCCCC*) sleep 30;;
  DDDDDDDD*) printf '\211PNG\r\n\032\n\000\000\000\rIHDR' > "$out";;
  *) echo "The operation couldn't be completed." >&2; exit 1;;
esac
"""


class ShortcutsTest(test_app_api.AppTest):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(core, "secrets_env", return_value={}))
        self.bin = Path(tempfile.mkdtemp(prefix="shaft-fake-shortcuts-")).resolve()
        self.addCleanup(shutil.rmtree, self.bin, True)
        cli = self.bin / "shortcuts"
        cli.write_text(FAKE)
        cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
        self.listing, self.ran = self.bin / "listing", self.bin / "ran"
        self.list_names({ECHO: "Echo", OTHER: "Other", SLOW: "Slow", IMAGE: "Picture"})
        self.ran.write_text("")
        self.enterContext(mock.patch.object(shortcuts, "CLI", str(cli)))
        self.enterContext(mock.patch.dict(os.environ, {"LISTING": str(self.listing), "RAN": str(self.ran)}))
        # the fake reads its files from the environment: let them through the CLI's trimmed one
        self.enterContext(mock.patch.object(shortcuts, "_env", lambda: dict(os.environ)))
        self.broker = broker.Broker(INSTANCE, self.sup, {}, self.sup / "broker.log",
                                    root=self.inst / INSTANCE, share=True)

    def list_names(self, rows: dict[str, str]):
        self.listing.write_text("".join(f"{name} ({i})\n" for i, name in rows.items()))

    def cli(self, *args) -> str:
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["shortcuts", *args]), 0)
        return out.getvalue()

    def offered(self) -> set[str]:
        return {s["function"]["name"] for s in self.broker.tool_specs()}

    def runs(self) -> list[str]:
        return self.ran.read_text().split()

    def test_nothing_granted_offers_nothing_and_runs_nothing(self):
        self.assertFalse(self.offered() & {"shortcut_list", "shortcut_run"})
        r = self.broker.call("shortcut_run", {"shortcut": "Echo", "input": "hi"})
        self.assertEqual(r, {"ok": False, "error": "no shortcut granted to you"})
        self.assertEqual(self.runs(), [])

    def test_a_granted_shortcut_runs_with_its_input_and_the_log_keeps_neither_text(self):
        self.cli(INSTANCE, "grant", "Echo")
        self.assertEqual(core.shortcut_grants(INSTANCE), [{"id": ECHO, "name": "Echo"}])
        self.assertTrue({"shortcut_list", "shortcut_run"} <= self.offered())
        spec = next(s for s in self.broker.tool_specs() if s["function"]["name"] == "shortcut_run")
        self.assertIn("Granted to you: Echo.", spec["function"]["description"])
        r = self.broker.call("shortcut_run", {"shortcut": "Echo", "input": "secret-ish words"})
        self.assertTrue(r["ok"], r)
        self.assertIn("Ran Echo", r["result"])
        self.assertTrue(r["result"].endswith("\nsecret-ish words"), r["result"])
        self.assertEqual(self.runs(), [ECHO])
        log = (self.sup / "broker.log").read_text()
        self.assertIn('shortcut_run {"shortcut": "Echo", "input_chars": 16', log)
        self.assertNotIn("secret-ish", log)

    def test_an_ungranted_shortcut_is_refused_before_it_runs(self):
        self.cli(INSTANCE, "grant", "Echo")
        for what in ("Other", OTHER, "echo2", ""):
            with self.subTest(what=what):
                r = self.broker.call("shortcut_run", {"shortcut": what})
                self.assertFalse(r["ok"])
                self.assertIn("is not granted to you; yours: Echo", r["error"])
        self.assertEqual(self.runs(), [])

    def test_the_grant_follows_the_identifier_through_a_rename(self):
        self.cli(INSTANCE, "grant", ECHO)
        self.list_names({ECHO: "Echo Renamed", OTHER: "Echo"})      # another shortcut takes the old name
        self.assertFalse(self.broker.call("shortcut_run", {"shortcut": "Echo"})["ok"])
        self.assertTrue(self.broker.call("shortcut_run", {"shortcut": "Echo Renamed"})["ok"])
        self.assertEqual(self.runs(), [ECHO])
        self.assertIn("Echo Renamed", self.broker.call("shortcut_list", {})["result"])

    def test_a_deleted_shortcut_says_so(self):
        self.cli(INSTANCE, "grant", "Echo")
        self.list_names({OTHER: "Other"})
        r = self.broker.call("shortcut_run", {"shortcut": "Echo"})
        self.assertIn("no longer on the operator's Mac", r["error"])
        self.assertIn("no longer on the Mac", self.broker.call("shortcut_list", {})["result"])

    def test_revoke_holds_from_the_next_call(self):
        self.cli(INSTANCE, "grant", "Echo")
        self.cli(INSTANCE, "grant", "Other")
        self.cli(INSTANCE, "revoke", "Echo")
        self.assertEqual(core.shortcut_grants(INSTANCE), [{"id": OTHER, "name": "Other"}])
        self.assertFalse(self.broker.call("shortcut_run", {"shortcut": "Echo"})["ok"])
        self.cli(INSTANCE, "off")
        self.assertEqual(core.shortcut_grants(INSTANCE), [])
        self.assertFalse(self.offered() & {"shortcut_list", "shortcut_run"})

    def test_grant_of_a_shortcut_not_on_this_mac_is_refused(self):
        with self.assertRaises(SystemExit):
            main(["shortcuts", INSTANCE, "grant", "Nope"])
        self.assertEqual(core.shortcut_grants(INSTANCE), [])

    def test_a_replay_is_never_offered_a_shortcut_and_never_runs_one(self):
        self.cli(INSTANCE, "grant", "Echo")
        self.broker.as_of = datetime.now()
        self.assertFalse(self.offered() & {"shortcut_list", "shortcut_run"})
        r = self.broker.call("shortcut_run", {"shortcut": "Echo"})
        self.assertFalse(r["ok"])
        self.assertEqual(self.runs(), [])

    def test_a_shortcut_waiting_for_someone_is_stopped(self):
        self.cli(INSTANCE, "grant", "Slow")
        with mock.patch.object(shortcuts, "RUN_TIMEOUT_S", 1):
            r = self.broker.call("shortcut_run", {"shortcut": "Slow"})
        self.assertFalse(r["ok"])
        self.assertIn("stopped after 1 s", r["error"])

    def test_a_failure_says_why(self):
        self.list_names({"EEEEEEEE-0000-4000-8000-000000000005": "Broken"})
        self.cli(INSTANCE, "grant", "Broken")
        r = self.broker.call("shortcut_run", {"shortcut": "Broken"})
        self.assertIn("couldn't be completed", r["error"])

    def test_binary_and_long_output_is_saved_under_tmp_shortcuts(self):
        self.cli(INSTANCE, "grant", "Picture")
        self.cli(INSTANCE, "grant", "Echo")
        r = self.broker.call("shortcut_run", {"shortcut": "Picture"})
        self.assertTrue(r["ok"], r)
        saved = list((self.inst / INSTANCE / ".tmp/shortcuts").glob("*-Picture.png"))
        self.assertEqual(len(saved), 1, r["result"])
        self.assertTrue(saved[0].read_bytes().startswith(b"\x89PNG"))
        long = "x" * (broker.SHORTCUT_TEXT_CHARS + 10)
        r = self.broker.call("shortcut_run", {"shortcut": "Echo", "input": long})
        self.assertIn("saved whole at .tmp/shortcuts/", r["result"])
        self.assertEqual(next((self.inst / INSTANCE / ".tmp/shortcuts").glob("*-Echo.txt")).read_text(), long)

    def test_input_file_stays_inside_the_folder(self):
        self.cli(INSTANCE, "grant", "Echo")
        (self.inst / INSTANCE / "note.txt").write_text("from a file")
        r = self.broker.call("shortcut_run", {"shortcut": "Echo", "input_file": "note.txt"})
        self.assertTrue(r["result"].endswith("\nfrom a file"), r)
        outside = self.bin / "outside.txt"
        outside.write_text("not yours")
        (self.inst / INSTANCE / "link.txt").symlink_to(outside)
        for path in ("../outside.txt", str(outside), "link.txt"):
            with self.subTest(path=path):
                r = self.broker.call("shortcut_run", {"shortcut": "Echo", "input_file": path})
                self.assertFalse(r["ok"], r)
        self.assertEqual(self.runs(), [ECHO])

    def test_its_keys_git_and_logs_are_never_given_to_a_shortcut(self):
        """Review finding 1: secrets/ is inside the instance's folder, and an echoing shortcut would hand its keys back."""
        self.cli(INSTANCE, "grant", "Echo")
        for rel in ("secrets/secrets.json", "Secrets/secrets.json", ".git/config", "logs/run.log"):
            path = self.inst / INSTANCE / rel.replace("Secrets", "secrets")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("KEY=sk-test-placeholder")
            with self.subTest(rel=rel):
                r = self.broker.call("shortcut_run", {"shortcut": "Echo", "input_file": rel})
                self.assertFalse(r["ok"], r)
                self.assertIn("never given to a shortcut", r["error"])
        self.assertEqual(self.runs(), [])

    def test_a_failure_logs_its_class_never_the_shortcuts_words(self):
        """Review finding 4: stderr can quote the input or the output."""
        self.list_names({"EEEEEEEE-0000-4000-8000-000000000005": "Broken"})
        self.cli(INSTANCE, "grant", "Broken")
        r = self.broker.call("shortcut_run", {"shortcut": "Broken", "input": "private words"})
        self.assertIn("couldn't be completed", r["error"])           # the instance hears why
        log = (self.sup / "broker.log").read_text()
        self.assertIn("-> ERROR ShortcutError", log)
        self.assertNotIn("couldn't be completed", log)
        self.assertNotIn("private words", log)

    def test_two_file_results_in_one_second_never_overwrite_each_other(self):
        """Review finding 6."""
        self.cli(INSTANCE, "grant", "Picture")
        fixed = datetime(2026, 11, 26, 22, 0, 0)
        with mock.patch.object(broker, "datetime", wraps=datetime) as clock:
            clock.now.return_value = fixed
            first = self.broker.call("shortcut_run", {"shortcut": "Picture"})["result"]
            second = self.broker.call("shortcut_run", {"shortcut": "Picture"})["result"]
        self.assertNotEqual(first, second)
        self.assertEqual(len(list((self.inst / INSTANCE / ".tmp/shortcuts").glob("*-Picture.png"))), 2)

    def test_an_instance_named_available_gets_its_grants(self):
        """Review finding 5: `available` was the catalog query, so a grant for an instance of that name did nothing."""
        for d in ("shaft", ".git"):   # an instance folder, as the base gives wisp
            (self.inst / "available" / d).mkdir(parents=True)
        self.cli("available", "grant", "Echo")
        self.assertEqual(core.shortcut_grants("available"), [{"id": ECHO, "name": "Echo"}])
        self.assertIn(f"Echo ({ECHO})", self.cli("--available"))

    def test_runs_per_run_are_capped(self):
        self.cli(INSTANCE, "grant", "Echo")
        for _ in range(broker.SHORTCUT_RUNS_PER_RUN):
            self.assertTrue(self.broker.call("shortcut_run", {"shortcut": "Echo"})["ok"])
        r = self.broker.call("shortcut_run", {"shortcut": "Echo"})
        self.assertIn(f"at most {broker.SHORTCUT_RUNS_PER_RUN} shortcut runs", r["error"])
        self.assertEqual(len(self.runs()), broker.SHORTCUT_RUNS_PER_RUN)

    def test_conservative_instances_are_refused_a_run(self):
        from supervisor import actions
        self.cli(INSTANCE, "grant", "Echo")
        with mock.patch.object(actions, "ACTION_INSTANCES", (INSTANCE,)):
            r = self.broker.call("shortcut_run", {"shortcut": "Echo"})
        self.assertFalse(r["ok"])
        self.assertIn("staged", r["error"])
        self.assertEqual(self.runs(), [])

    def test_the_app_lists_and_grants_shortcuts(self):
        got = self.get(f"/app/v1/access/{INSTANCE}")
        self.assertEqual(got["shortcuts"], [])
        self.assertIn({"id": ECHO, "name": "Echo"}, got["shortcuts_available"])
        self.assertIsNone(got["shortcuts_problem"])


class ParseTest(unittest.TestCase):
    def test_list_lines_with_parentheses_in_the_name(self):
        rows = shortcuts.parse(f"Plain ({ECHO})\nWith (paren) here ({OTHER})\nno identifier\n")
        self.assertEqual(rows, [{"id": ECHO, "name": "Plain"}, {"id": OTHER, "name": "With (paren) here"}])

    def test_an_identifier_never_resolves_to_a_shortcut_named_like_it(self):
        """Review finding 3: a stale selection (the shortcut deleted) must not grant another one carrying its id as name."""
        rows = [{"id": OTHER, "name": ECHO}]
        self.assertIsNone(shortcuts.resolve(ECHO, rows))
        self.assertEqual(shortcuts.resolve(OTHER.lower(), rows)["id"], OTHER)
        self.assertIsNone(shortcuts.resolve("Same", [{"id": ECHO, "name": "Same"}, {"id": OTHER, "name": "Same"}]))

    def test_resolve_prefers_exact_then_a_single_case_match(self):
        rows = [{"id": ECHO, "name": "Echo"}, {"id": OTHER, "name": "ECHO"}]
        self.assertEqual(shortcuts.resolve("Echo", rows)["id"], ECHO)
        self.assertIsNone(shortcuts.resolve("echo", rows))          # two match in any case: ambiguous
        self.assertEqual(shortcuts.resolve(OTHER, rows)["id"], OTHER)


if __name__ == "__main__":
    unittest.main()
