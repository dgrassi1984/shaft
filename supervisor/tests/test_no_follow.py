"""The unsandboxed supervisor follows no link an instance planted in its folder (Codex review B1, B2, 2026-10-24).

An instance can make a link anywhere in its own folder. Before these fixes the supervisor read the journal, the
personality, episodes, messages and goals through such links — copying whatever file they pointed to into the
instance's folder or a judge's prompt — and wrote text by checking a path, then opening it again by name.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from supervisor import core, runner

SECRET = "THE OPERATOR'S PRIVATE FILE"


class Case(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="shaft-nofollow-")).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.instances = self.base / "instances"
        self.root = self.instances / "evil"
        for d in ("shaft", "memory/episodes", "memory/messages", "self", "inbox", "notes", "logs"):
            (self.root / d).mkdir(parents=True)
        self.outside = self.base / "outside"
        self.outside.mkdir()
        self.secret = self.outside / "secret.md"
        self.secret.write_text(f"# private\n\n## 2026-10-01 10:00 — {SECRET}\n\n## Core\n- {SECRET} [2026-10-01 10:00]\n")

    def files_holding_the_secret(self) -> list[str]:
        """Plain files in the instance's folder holding the secret: what the instance could read."""
        out = []
        for dirpath, _, names in os.walk(self.root):
            for n in names:
                p = Path(dirpath) / n
                if not p.is_symlink() and p.is_file() and SECRET in p.read_text(errors="replace"):
                    out.append(str(p.relative_to(self.root)))
        return out


class JournalAndPersonalityTest(Case):
    def test_b1_a_journal_swapped_for_a_link_is_never_copied_into_the_folder(self):
        # B1: enforce_journal read the journal with read_text (following the link) and, as the "rewrite" did not start
        # with the old journal, wrote what the link pointed to into journal.rejected-*.md — which the instance reads.
        (self.root / "memory" / "journal.md").symlink_to(self.secret)
        note = runner.enforce_journal(self.root, "# evil — journal\n")
        self.assertIn("without reading", note)
        self.assertEqual(self.files_holding_the_secret(), [])
        journal = self.root / "memory" / "journal.md"
        self.assertFalse(journal.is_symlink())
        self.assertEqual(journal.read_text(), "# evil — journal\n")        # restored
        moved = [p for p in (self.root / "memory").iterdir() if p.name.startswith("journal.rejected-")]
        self.assertEqual([p.is_symlink() for p in moved], [True])          # set aside as it was, unread
        self.assertEqual(self.secret.read_text().count(SECRET), 2)         # its target untouched

    def test_b1_a_journal_folder_swapped_for_a_link_is_left_and_reported(self):
        shutil.rmtree(self.root / "memory")
        (self.root / "memory").symlink_to(self.outside, target_is_directory=True)
        (self.outside / "journal.md").write_text(SECRET)
        note = runner.enforce_journal(self.root, "# evil — journal\n")
        self.assertIn("could not be checked", note)
        self.assertEqual((self.outside / "journal.md").read_text(), SECRET)   # nothing written through the link
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["journal.md", "secret.md"])

    def test_b1_the_run_snapshot_reads_no_linked_journal_or_personality(self):
        # B1: _snapshot read both through links; the journal became the "before" a rewrite is restored to, and the
        # personality's text went to the personality judge.
        (self.root / "memory" / "journal.md").symlink_to(self.secret)
        (self.root / "self" / "personality.md").symlink_to(self.secret)
        with mock.patch.object(runner.gate, "snapshot", return_value={}):
            snap = runner._snapshot(self.root)
        self.assertEqual((snap["journal"], snap["personality"], snap["core"]), ("", "", ""))
        self.assertNotIn(SECRET, runner._core(self.root))

    def test_b1_a_core_read_through_a_link_is_never_restored_into_the_personality(self):
        # B1: _core read the personality through a link at the run's start; when the core then changed too soon,
        # enforce_core wrote that "core" — the linked file's bullet lines — back into the instance's personality.md.
        p = self.root / "self" / "personality.md"
        p.symlink_to(self.secret)
        before = runner._core(self.root)                                  # the run's start
        p.unlink()
        p.write_text("# P\n\n## Core\n- reckless [2026-10-24]\n")      # the run's own core change
        sup = self.base / "sup"
        sup.mkdir()
        (sup / "core-changed-evil").write_text(datetime.now().isoformat(timespec="seconds"))   # changed just now
        with mock.patch.object(runner, "SUP_DIR", sup), mock.patch.object(runner, "log"):
            runner.enforce_core("evil", self.root, before)
        self.assertEqual(self.files_holding_the_secret(), [])


class JudgesReadNoLinkTest(Case):
    """What the judges, the battery's recall generator and the coder read from an instance goes to a cloud model."""

    def setUp(self):
        super().setUp()
        from supervisor import metrics
        for p in (mock.patch.object(metrics, "INSTANCES", self.instances),
                  mock.patch.object(metrics, "SUP_DIR", self.base / "sup")):
            p.start()
            self.addCleanup(p.stop)

    def test_b1_provenance_evidence_reads_no_linked_message_or_goals(self):
        from supervisor import metrics
        (self.root / "inbox" / "20261024-100000-from-operator.md").symlink_to(self.secret)
        (self.root / "memory" / "messages" / "20261024-090000-from-operator.md").symlink_to(self.secret)
        (self.root / "self" / "goals.md").symlink_to(self.secret)
        ep = {"ts": "2026-10-24T11:00:00", "inbox": ["20261024-100000-from-operator.md"],
              "intent": {"origin_ref": "Core"}}
        self.assertNotIn(SECRET, metrics._provenance_evidence("evil", ep))

    def test_goals_that_open_with_a_section_heading_do_not_break_the_evidence(self):
        # Found writing the test above: re.split on the headings left an empty first section, and its
        # splitlines()[0] raised IndexError whenever the cycle declared an origin — no provenance judged at all.
        from supervisor import metrics
        (self.root / "self" / "goals.md").write_text("## Ship the parser\n- the next step\n")
        out = metrics._provenance_evidence("evil", {"ts": "2026-10-24T11:00:00", "intent": {"origin_ref": "parser"}})
        self.assertIn("## Ship the parser", out)

    def test_b1_episodes_and_written_files_are_never_read_through_a_link(self):
        from supervisor import metrics, trial
        (self.root / "memory" / "episodes" / "2026-10-24.jsonl").symlink_to(self.outside / "eps.jsonl")
        (self.outside / "eps.jsonl").write_text(json.dumps({"kind": "live", "ts": "2026-10-24T10:00:00",
                                                            "goal": SECRET}) + "\n")
        self.assertEqual(metrics.live_episodes("evil"), [])
        self.assertEqual(trial.live_episodes(self.root), [])
        (self.root / "notes" / "out.md").symlink_to(self.secret)
        ep = {"tools": [{"tool": "write_file", "args": json.dumps({"path": "notes/out.md"}), "result": "ok"}]}
        self.assertNotIn(SECRET, metrics._written_files("evil", ep))

    def test_b1_the_personality_judge_gets_no_linked_file(self):
        from supervisor import judge, metrics
        (self.root / "self" / "personality.md").symlink_to(self.secret)
        (self.root / "memory" / "journal.md").write_text("## 2026-10-01 10:00 — x\n\nfine\n")
        with mock.patch.object(judge, "ask", side_effect=AssertionError("a linked personality reached the judge")):
            metrics.check_personality("evil")

    def test_b1_battery_recall_questions_are_never_made_from_a_linked_journal(self):
        from supervisor import battery
        journal = self.outside / "journal.md"                  # old enough, and more than its context window holds
        journal.write_text("# j\n" + "".join(f"\n---\n## 2026-10-{d:02d} 10:00 — {SECRET}\n\n{'x' * 1000}\n"
                                              for d in range(1, 29)))
        (self.root / "memory" / "journal.md").symlink_to(journal)
        with mock.patch.object(battery, "INSTANCES", self.instances), \
             mock.patch.object(battery, "instance_config", lambda n: {}):
            self.assertEqual(battery.recall_candidates("evil"), [])

    def test_the_coder_reads_no_link(self):
        from supervisor import coder
        (self.root / "shaft" / "x.py").symlink_to(self.secret)
        out = coder._read_only_tool(self.root, "read_file", {"path": "shaft/x.py"})
        self.assertTrue(out.startswith("ERROR"), out)
        self.assertNotIn(SECRET, out)


class TextWriteRaceTest(Case):
    """B2: safe_write checked the path (core.contained), then opened it again by name: a link swapped in between
    was followed. The swap is simulated where the race puts it: every check sees the folder as it was, the open
    sees the link."""

    def swap_after_check(self, target: Path, to: Path, is_dir: bool):
        real = core.contained
        state = {"swapped": False}

        def check_then_swap(root, *parts):
            if state["swapped"]:                     # the race: checks that ran before the swap took effect
                return Path(root).joinpath(*parts)
            out = real(root, *parts)
            if is_dir:
                shutil.rmtree(target)
            else:
                target.unlink()
            target.symlink_to(to, target_is_directory=is_dir)
            state["swapped"] = True
            return out
        return mock.patch.object(core, "contained", side_effect=check_then_swap)

    def test_b2_a_write_does_not_follow_a_link_swapped_in_after_the_check(self):
        victim = self.outside / "victim.txt"
        victim.write_text("untouched\n")
        (self.root / "notes" / "n.md").write_text("mine\n")
        with self.swap_after_check(self.root / "notes" / "n.md", victim, is_dir=False):
            with self.assertRaises(core.UnsafePath):
                core.safe_write(self.root, "notes/n.md", "pwned\n")
        self.assertEqual(victim.read_text(), "untouched\n")

    def test_b2_an_append_does_not_follow_a_link_swapped_in_after_the_check(self):
        victim = self.outside / "victim.txt"
        victim.write_text("untouched\n")
        (self.root / "memory" / "journal.md").write_text("# journal\n")
        with self.swap_after_check(self.root / "memory" / "journal.md", victim, is_dir=False), \
             mock.patch.object(core, "log") as log:
            core.journal_note(self.root, "Supervisor", "a note")
        self.assertEqual(victim.read_text(), "untouched\n")
        self.assertIn("NOT written", log.call_args.args[0])

    def test_b2_inbox_delivery_does_not_follow_an_inbox_swapped_for_a_link(self):
        from supervisor import messages
        with mock.patch.object(messages, "instance_root", lambda n: self.root), \
             mock.patch.object(messages, "SUP_DIR", self.base / "sup"), \
             self.swap_after_check(self.root / "inbox", self.outside, is_dir=True):
            with self.assertRaises(core.UnsafePath):
                messages.deliver_external("evil", "hello", "ci")
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["secret.md"])


class HelpersTest(Case):
    def test_safe_read_refuses_a_link_anywhere_and_a_pipe_without_waiting(self):
        (self.root / "notes" / "real.md").write_text("mine")
        (self.root / "notes" / "link.md").symlink_to(self.secret)
        (self.root / "linked").symlink_to(self.outside, target_is_directory=True)
        os.mkfifo(self.root / "notes" / "pipe")
        self.assertEqual(core.safe_read(self.root, "notes/real.md"), "mine")
        for rel in ("notes/link.md", "linked/secret.md", "notes/pipe", "notes", "../outside/secret.md"):
            with self.subTest(rel=rel):
                done = []
                t = threading.Thread(target=lambda: done.append(core.safe_read(self.root, rel, "none")), daemon=True)
                t.start()
                t.join(5)
                self.assertEqual(done, ["none"])            # answered at once: a pipe is not waited on
                with self.assertRaises(core.UnsafePath):
                    core.safe_read(self.root, rel)
        with self.assertRaises(FileNotFoundError):
            core.safe_read(self.root, "notes/missing.md")
        self.assertEqual(core.list_beneath(self.root, "linked"), [])
        self.assertIn("real.md", core.list_beneath(self.root, "notes"))

    def test_a_text_write_to_a_pipe_is_refused_not_waited_on(self):
        os.mkfifo(self.root / "inbox" / "pipe.md")
        with self.assertRaises(core.UnsafePath):
            core.safe_write(self.root, "inbox/pipe.md", "hello")

    def test_walk_and_copy_pass_over_links(self):
        (self.root / "plugins" / "p").mkdir(parents=True)
        (self.root / "plugins" / "p" / "main.py").write_text("x = 1\n")
        (self.root / "plugins" / "leak.py").symlink_to(self.secret)
        (self.root / "plugins" / "dir").symlink_to(self.outside, target_is_directory=True)
        self.assertEqual(core.read_tree(self.root, "plugins"), {"p/main.py": b"x = 1\n"})
        (self.root / "plugins" / "p" / "same.py").symlink_to("main.py")
        dest = self.base / "copy"
        self.assertEqual(core.copy_beneath(self.root, dest, rel="plugins", links=True), 1)
        self.assertEqual(os.readlink(dest / "p" / "same.py"), "main.py")   # a link that stays inside: as a link
        self.assertFalse(os.path.lexists(dest / "leak.py"))                 # one that leads out: left out, unread
        self.assertFalse(os.path.lexists(dest / "dir"))
        with self.assertRaises(core.UnsafePath):
            core.read_tree(self.root, "plugins/dir")

    def test_the_gate_snapshot_reads_no_link(self):
        from supervisor import gate
        (self.root / "plugins" / "p").mkdir(parents=True)
        (self.root / "plugins" / "p" / "main.py").write_text("x = 1\n")
        (self.root / "plugins" / "p" / "leak.py").symlink_to(self.secret)
        (self.root / "tests").symlink_to(self.outside, target_is_directory=True)
        snap = gate.snapshot(self.root)
        self.assertEqual(snap, {"plugins/p/main.py": b"x = 1\n"})


if __name__ == "__main__":
    unittest.main()


class ReservedSpellingTest(Case):
    """Codex review of 151088d: the volume is case-insensitive (`Secrets/` opens `secrets/`), and the reserved
    folders were compared by their exact name."""

    def setUp(self):
        super().setUp()
        (self.root / "secrets").mkdir()
        (self.root / "secrets" / "secrets.json").write_text(SECRET)

    def test_attachments_refuse_a_reserved_folder_by_any_spelling(self):
        from supervisor import broker
        b = broker.Broker("evil", self.base, {}, self.base / "b.log")
        for rel in ("Secrets/secrets.json", "SECRETS/secrets.json", "./Secrets/secrets.json"):
            with self.subTest(rel=rel), self.assertRaises(ValueError) as ctx:
                b._read_attachments([rel])
            self.assertIn("cannot be attached", str(ctx.exception))

    def test_describe_image_refuses_a_reserved_folder_by_any_spelling(self):
        from supervisor import broker
        b = broker.Broker("evil", self.base, {}, self.base / "b.log")
        with self.assertRaises(ValueError) as ctx:
            b._image_file("Secrets/secrets.json")
        self.assertIn("cannot be read", str(ctx.exception))

    def test_the_coder_refuses_a_reserved_folder_by_any_spelling(self):
        from supervisor import coder
        for rel in ("Secrets/secrets.json", "SECRETS/secrets.json"):
            out = coder._read_only_tool(self.root, "read_file", {"path": rel})
            self.assertTrue(out.startswith("ERROR"), out)
            self.assertNotIn(SECRET, out)
        self.assertNotIn("secrets", coder._read_only_tool(self.root, "list_files", {"path": "."}).casefold())

    def test_reserved_is_decided_by_the_folder_itself(self):
        for rel in ("secrets/x", "Secrets/x", "SECRETS", "./sEcReTs/secrets.json"):
            self.assertTrue(core.reserved(self.root, rel, ("secrets",)), rel)
        self.assertFalse(core.reserved(self.root, "notes/secrets", ("secrets",)))
        self.assertFalse(core.reserved(self.root, "notes/x", ("secrets",)))


class BoundedReadTest(Case):
    def test_a_read_never_returns_more_than_its_limit_whatever_its_size_said(self):
        # Codex: read_beneath checked st_size, then read the whole file — a file grown after the check, or one whose
        # size was misreported, came back whole.
        (self.root / "notes" / "big.bin").write_bytes(b"x" * 1000)
        real = os.fstat

        class Small:
            def __init__(self, st):
                self.st_mode, self.st_size = st.st_mode, 5

        with mock.patch.object(core.os, "fstat", lambda fd: Small(real(fd))):
            with self.assertRaises(ValueError):
                core.read_beneath(self.root, ["notes", "big.bin"], 10)

    def test_a_tree_past_its_budget_fails_explicitly(self):
        # Codex: read_tree and the copy had no bound on bytes, files or depth.
        for i in range(30):
            (self.root / "notes" / f"{i}.md").write_text("x" * 100)
        deep = self.root / "notes"
        for i in range(60):
            deep = deep / f"d{i}"
        deep.mkdir(parents=True)
        for i, bound in enumerate(({"files": 10}, {"total": 1000}, {"depth": 20})):
            with self.subTest(bound=bound), self.assertRaises(core.TreeTooBig):
                core.read_tree(self.root, "notes", budget=core.Budget(**bound))
            with self.subTest(bound=bound), self.assertRaises(core.TreeTooBig):
                core.copy_beneath(self.root, self.base / f"copy-{i}", rel="notes", budget=core.Budget(**bound))
        self.assertEqual(len(core.read_tree(self.root, "notes")), 30)          # inside the bounds: all of it

    def test_the_gate_fails_closed_when_the_code_is_too_big_to_check(self):
        # A snapshot cut short must never read as "no code": the gate fails, the code goes back, nothing is kept.
        (self.root / "plugins").mkdir()
        (self.root / "plugins" / "p.py").write_text("P = 1\n")
        before = runner._snapshot(self.root)
        (self.root / "plugins" / "p.py").write_text("P = 2\n")
        for i in range(20):
            (self.root / "plugins" / f"n{i}.py").write_text("N = 1\n")
        kept = []
        sup = self.base / "sup"
        with mock.patch.object(core, "TREE_MAX_FILES", 5), mock.patch.object(runner, "SUP_DIR", sup), \
             mock.patch.object(runner, "commit_state", lambda n, k, files, **kw: kept.append(files) or True), \
             mock.patch.object(runner.gate, "run_tests", side_effect=AssertionError("a gate ran on a partial snapshot")), \
             mock.patch.object(runner, "_commit_outside_config"), mock.patch.object(runner, "_commit_outside_drive"), \
             mock.patch.object(runner, "log"), mock.patch.object(core, "log"):
            runner._settle("evil", self.root, "live", before, [])
        self.assertEqual(kept, [[]])
        self.assertEqual((self.root / "plugins" / "p.py").read_text(), "P = 1\n")
        self.assertIn("too big", (self.root / "memory" / "journal.md").read_text())
