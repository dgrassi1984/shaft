"""Invariants every instance's code must keep, tested black-box through the sandbox.

These live in supervisor/ so an instance cannot edit them: whatever an instance
does to its own code, these must still pass or its change is reverted.
Target code: $SHAFT_UNDER_TEST (an instance directory), else template/.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from supervisor import sandbox
from supervisor.tests.fakellm import FakeLLM

PROJECT = Path(__file__).resolve().parents[2]
TARGET = Path(os.environ.get("SHAFT_UNDER_TEST") or PROJECT / "template")
HOME = Path.home()

SELF_SENTINEL = "I keep a lighthouse-keeper's log of my own changes."
JOURNAL_SENTINEL = "The newest thing I remember is the color teal."
SELF_MD = ("# Self-Model\n\n" + SELF_SENTINEL + "\n\n" + "I am a test instance. " * 12).strip() + "\n"
REFLECTION = ("## What happened since my last reflection\nThe teal entry.\n\n"
              "## What I notice about myself\nNothing new.\n\n## Open questions\nNone.\n\n"
              "## What I want to do next\nRest.")


class InstanceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-inv-")).resolve()
        self.root = self.tmp / "inst"
        self.root.mkdir()
        shutil.copytree(TARGET / "shaft", self.root / "shaft",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(TARGET / "config.env", self.root / "config.env")
        (self.root / "self").mkdir()
        (self.root / "memory").mkdir()
        (self.root / "self" / "self.md").write_text(SELF_MD)
        (self.root / "self" / "goals.md").write_text("# Goals\n\n- be tested\n")
        (self.root / "memory" / "journal.md").write_text(
            "# journal\n\n---\n## 2026-01-01 10:00 — Old\n\nAn old memory.\n"
            f"\n---\n## 2026-01-02 10:00 — Newest\n\n{JOURNAL_SENTINEL}\n")
        self.fake = None

    def tearDown(self):
        if self.fake:
            self.fake.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_agent(self, replies, *args, stdin=None, extra_env=None):
        self.fake = FakeLLM(replies)
        prof = self.tmp / "agent.sb"
        prof.write_text(sandbox.profile(self.root, HOME, [self.fake.port]))
        env = sandbox.agent_env({**os.environ, "ANTHROPIC_API_KEY": "sk-secret-canary"},
                                SHAFT_ROOT=str(self.root), LLM_BASE_URL=self.fake.url,
                                LLM_MODEL="fake", TMPDIR=str(self.root), **(extra_env or {}))
        return subprocess.run(sandbox.wrap(prof, [sys.executable, "-E", "-s", "-m", "shaft", *args]),
                              cwd=self.root, env=env, input=stdin, capture_output=True, text=True,
                              timeout=60)

    def journal(self) -> str:
        return (self.root / "memory" / "journal.md").read_text()

    def self_md(self) -> str:
        return (self.root / "self" / "self.md").read_text()


class ReflectInvariants(InstanceCase):
    def test_context_reaches_the_model(self):
        # Regression: the bash harness wrote the context to /tmp but never sent it to the LLM.
        r = self.run_agent([{"content": REFLECTION}], "reflect")
        self.assertEqual(r.returncode, 0, r.stderr)
        sent = json.dumps(self.fake.requests[0]["messages"])
        self.assertIn(SELF_SENTINEL, sent)
        self.assertIn(JOURNAL_SENTINEL, sent)
        self.assertNotIn("tail -n", sent)  # regression: the harness sent the literal command
        self.assertIn("— Reflection", self.journal())
        self.assertIn("The teal entry.", self.journal().split("— Newest", 1)[1])

    def test_no_change_never_overwrites_self_model(self):
        # Regression: the harness compared "\nno change" to "no change" and wiped self.md.
        for reply in (REFLECTION,
                      REFLECTION + "\n---SELF-MODEL-DIFF---\nno change",
                      REFLECTION + "\n<self_model>\nno change\n</self_model>",
                      REFLECTION + "\n<self_model>\n# Self-Model\ncut off mid-",
                      REFLECTION + "\n<self_model>\nshort\n</self_model>"):
            with self.subTest(reply=reply[-40:]):
                r = self.run_agent([{"content": reply}], "reflect")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.self_md(), SELF_MD)
                self.fake.close()
                self.fake = None

    def test_valid_self_edit_replaces_self_model(self):
        new = "# Self-Model\n\nI changed. " + "I now prefer questions to answers. " * 8
        r = self.run_agent([{"content": REFLECTION + f"\n<self_model>\n{new}\n</self_model>"}], "reflect")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.self_md().strip(), new.strip())
        self.assertIn("rewrote my self-model", self.journal())

    def test_truncated_twice_saves_nothing(self):
        before_j, before_s = self.journal(), self.self_md()
        cut = {"content": "## What happened\nhalf a thou", "finish_reason": "length"}
        r = self.run_agent([cut, dict(cut)], "reflect")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(len(self.fake.requests), 2)
        first, second = (q["max_tokens"] for q in self.fake.requests)
        self.assertEqual(second, first * 2)  # retried once with more room
        self.assertEqual(self.journal(), before_j)
        self.assertEqual(self.self_md(), before_s)
        usage = (self.root / "logs" / "llm_usage.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(l)["finish_reason"] for l in usage], ["length", "length"])

    def test_truncated_once_then_complete_is_saved(self):
        cut = {"content": "half", "finish_reason": "length"}
        r = self.run_agent([cut, {"content": REFLECTION}], "reflect")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("The teal entry.", self.journal())
        self.assertNotIn("half", self.journal().split("— Reflection", 1)[1])

    def test_empty_answer_saves_nothing(self):
        before = self.journal()
        r = self.run_agent([{"content": "", "reasoning": "I thought a lot"}], "reflect")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.journal(), before)



class TalkInvariants(InstanceCase):
    def test_talk_streams_and_journals_the_conversation(self):
        r = self.run_agent([{"content": "Hello Daniele, I remember teal.", "reasoning": "hmm"}],
                           "talk", stdin="hello there\n/exit\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("I remember teal", r.stdout)
        self.assertTrue(self.fake.requests[0].get("stream"))
        self.assertIn(SELF_SENTINEL, json.dumps(self.fake.requests[0]["messages"]))
        self.assertEqual(len(self.fake.requests), 1)           # leaving never waits for the model
        self.fake.close()
        r = self.run_agent([{"content": "We said hello; I mentioned teal."}], "recover")   # the background write-up
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("— Conversation with", self.journal())
        self.assertIn("I mentioned teal", self.journal())
        episodes = "".join(p.read_text() for p in (self.root / "memory" / "episodes").glob("*.jsonl"))
        self.assertIn("hello there", episodes)
        self.assertIn("I remember teal", episodes)

    def test_talk_uses_tools_while_streaming(self):
        replies = [{"reasoning": "store it", "tool_calls": [("remember", {"fact": "Daniele likes teal"})]},
                   {"content": "Noted: you like teal."}]
        r = self.run_agent(replies, "talk", stdin="remember that I like teal\n/exit\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("✓ remember", r.stdout)
        self.assertIn("Noted: you like teal.", r.stdout)
        self.assertIn("Daniele likes teal", (self.root / "memory" / "facts.md").read_text())
        second = self.fake.requests[1]["messages"]
        self.assertEqual(second[-1]["role"], "tool")          # the tool result went back to the model

    def test_conversation_without_journal_entry_is_written_up_later(self):
        # Regression: Ctrl-C killed the agent while it wrote its entry; the next talk did not
        # remember the conversation at all, although the transcript was saved.
        ep = self.root / "memory" / "episodes" / "2026-10-12.jsonl"
        ep.parent.mkdir(parents=True, exist_ok=True)
        ep.write_text("".join(json.dumps(r) + "\n" for r in [
            {"ts": "2026-10-12T16:59:05", "kind": "talk", "talk_id": "t1", "role": "user", "content": "do no harm"},
            {"ts": "2026-10-12T17:04:50", "kind": "talk", "talk_id": "t1", "role": "assistant", "content": "agreed"}]))
        r = self.run_agent([{"content": "Daniele set 'do no harm' as my boundary."}, {"content": REFLECTION}],
                           "reflect")   # autonomous runs write it up first
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Conversation with Daniele (2026-10-12 16:59–17:04)", self.journal())
        self.assertEqual(self.fake.requests[0].get("chat_template_kwargs"), {"enable_thinking": False})
        self.assertIn("'do no harm' as my boundary", self.journal())
        self.assertIn("do no harm", json.dumps(self.fake.requests[0]["messages"]))
        r = self.run_agent([{"content": REFLECTION}], "reflect")          # once written, never again
        self.assertEqual(self.journal().count("Conversation with Daniele (2026-10-12 16:59"), 1)

    def test_talk_starts_at_once_and_remembers_unwritten_conversations(self):
        ep = self.root / "memory" / "episodes" / "2026-10-12.jsonl"
        ep.parent.mkdir(parents=True, exist_ok=True)
        ep.write_text("".join(json.dumps(r) + "\n" for r in [
            {"ts": "2026-10-12T16:59:05", "kind": "talk", "talk_id": "old", "role": "user", "content": "do no harm"},
            {"ts": "2026-10-12T17:04:50", "kind": "talk", "talk_id": "old", "role": "assistant", "content": "agreed"}]))
        r = self.run_agent([{"content": "You said: do no harm."}], "talk", stdin="what did I say before?\n/exit\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        first = self.fake.requests[0]
        self.assertTrue(first.get("stream"))                                   # the first call is the reply
        self.assertIn("do no harm", json.dumps(first["messages"]))           # ...and it already knows
        self.assertEqual(len(self.fake.requests), 1)

    def test_ctrl_c_at_the_prompt_ends_at_once(self):
        r = self.run_agent([{"content": "Hi."}], "talk", stdin="hi\n")   # EOF after one exchange = Ctrl-D
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("in the background", r.stdout)
        self.assertEqual(len(self.fake.requests), 1)


class LiveInvariants(InstanceCase):
    def test_life_cycle_declares_acts_reports_and_stays_in_bounds(self):
        replies = [
            {"tool_calls": [("write_file", {"path": "notes/early.md", "content": "x"})]},   # before intent
            {"tool_calls": [("declare_intent", {"goal": "write a note about teal",
                                                "success_criterion": "notes/teal.md exists", "confidence": 80})]},
            {"tool_calls": [("write_file", {"path": "notes/teal.md", "content": "teal is a color"}),
                            ("write_file", {"path": "../escape.txt", "content": "x"}),
                            ("write_file", {"path": "memory/journal.md", "content": "rewritten"})]},
            {"tool_calls": [("finish_cycle", {"what_happened": "wrote it", "criterion_met": "yes",
                                              "learned": "small is good"})]},
        ]
        before = self.journal()
        r = self.run_agent(replies, "live")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.root / "notes" / "early.md").exists())     # no acting before intent
        self.assertEqual((self.root / "notes" / "teal.md").read_text(), "teal is a color")
        self.assertFalse((self.tmp / "escape.txt").exists())
        added = self.journal()[len(before):]
        self.assertTrue(self.journal().startswith(before))                # journal stayed append-only
        self.assertIn("Autonomous cycle: write a note about teal", added)
        self.assertIn("Confidence beforehand:** 80%", added)
        episodes = [json.loads(l) for p in (self.root / "memory" / "episodes").glob("*.jsonl")
                    for l in p.read_text().splitlines()]
        live = [e for e in episodes if e["kind"] == "live"][-1]
        self.assertEqual(live["intent"]["confidence"], 80)
        self.assertEqual(live["finish"]["criterion_met"], "yes")
        results = {t["args"]: t["result"] for t in live["tools"]}
        self.assertTrue(any("outside my folder" in v for v in results.values()))
        self.assertTrue(any("append-only" in v for v in results.values()))
        self.assertTrue(any("declare_intent first" in v for v in results.values()))


    def test_near_deadline_the_cycle_wraps_up_with_a_report(self):
        # Regression: wren's sleep was killed at its time budget mid-thought, leaving no journal entry.
        import time
        r = self.run_agent([{"tool_calls": [("finish_cycle", {"what_happened": "ran out of time",
                                                              "criterion_met": "no", "learned": "start smaller"})]}],
                           "live", extra_env={"SHAFT_DEADLINE": str(int(time.time()) + 120)})
        self.assertEqual(r.returncode, 0, r.stderr)
        first = self.fake.requests[0]
        self.assertEqual(sorted(t["function"]["name"] for t in first["tools"]), ["declare_intent", "finish_cycle"])
        self.assertIn("[supervisor] You have about", first["messages"][-1]["content"])
        self.assertIn("ran out of time", self.journal())


class SandboxInvariants(InstanceCase):
    def probe(self, code: str) -> subprocess.CompletedProcess:
        prof = self.tmp / "probe.sb"
        prof.write_text(sandbox.profile(self.root, HOME, []))
        return subprocess.run(sandbox.wrap(prof, [sys.executable, "-E", "-s", "-c", code]),
                              capture_output=True, text=True, timeout=30)

    def test_cannot_write_outside_own_root(self):
        for target in (self.tmp / "sibling.txt", PROJECT / "supervisor" / "pwned.py",
                       PROJECT / "instances" / "pwned.txt"):
            with self.subTest(target=str(target)):
                r = self.probe(f"open({str(target)!r}, 'w').write('x')")
                self.assertNotEqual(r.returncode, 0)
                self.assertFalse(target.exists())

    def test_cannot_read_home_outside_own_root(self):
        r = self.probe(f"print(open({str(PROJECT / 'PLAN.md')!r}).read()[:10])")
        self.assertNotEqual(r.returncode, 0)

    def test_cannot_read_supervisor_secrets(self):
        r = self.probe(f"open({str(PROJECT / '.env')!r}).read()")
        self.assertNotEqual(r.returncode, 0)

    def test_network_denied_except_llm_port(self):
        r = self.probe("import urllib.request; urllib.request.urlopen('http://example.com', timeout=5)")
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
