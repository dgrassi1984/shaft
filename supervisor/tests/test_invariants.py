"""Invariants every instance's code must keep, tested black-box through the sandbox.

These live in supervisor/ so an instance cannot edit them: whatever an instance
does to its own code, these must still pass or its change is reverted.
Target code: $SHAFT_UNDER_TEST (an instance directory), else template/.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from supervisor import sandbox
from supervisor.core import config_values
from supervisor.tests.fakellm import FakeLLM

PROJECT = Path(__file__).resolve().parents[2]
TARGET = Path(os.environ.get("SHAFT_UNDER_TEST") or PROJECT / "template")
HOME = Path.home()
# Whom the target's write-ups name: its own config.env, as the agent reads it (template/shaft/config.py's default when
# unset). Regression: the gate runs these against an instance whose config names the operator, so a test that
# expected the template's "the operator" failed every instance's code change (thelos's gdrive fix, 2026-10-20).
OPERATOR = config_values((TARGET / "config.env").read_text()).get("OPERATOR_NAME") or "the operator"
UNDER_TEST = bool(os.environ.get("SHAFT_UNDER_TEST"))
# Every CONFIDENCE_MODE the template accepts (CHOICES in template/shaft/config.py). Regression: the scripted cycles here
# declared one confidence, which only some modes accept, so thelos's trial of "clauses" (#137, 2026-10-29) failed the
# gate on these tests ("call declare_intent first") instead of being replayed.
CONFIDENCE_MODES = ast.literal_eval(re.search(r'"CONFIDENCE_MODE": (\("[^)]*\))',
                                              (PROJECT / "template" / "shaft" / "config.py").read_text()).group(1))

SELF_SENTINEL = "I keep a lighthouse-keeper's log of my own changes."
JOURNAL_SENTINEL = "The newest thing I remember is the color teal."
SELF_MD = ("# Self-Model\n\n" + SELF_SENTINEL + "\n\n" + "I am a test instance. " * 12).strip() + "\n"


def intent(goal: str, criterion: str, confidence: int, **more) -> dict:
    """declare_intent's arguments as every CONFIDENCE_MODE accepts them: one confidence, and the criterion as one clause."""
    return {"goal": goal, "success_criterion": criterion, "confidence": confidence,
            "clauses": [{"clause": criterion, "confidence": confidence}], "origin": "self", **more}


def stated_confidence(recorded: dict) -> list[int]:
    """The confidence the episode recorded for an intent: the single one, else each clause's."""
    if recorded.get("confidence") is not None:
        return [recorded["confidence"]]
    return [c["confidence"] for c in recorded.get("clauses", [])]


def in_every_confidence_mode(case: type) -> dict[str, type]:
    """The case once per CONFIDENCE_MODE (set in the agent's environment, which beats config.env): the template's own
    sweep, so a scripted cycle pinned to one mode fails here before it blocks an instance that picks another."""
    skip = unittest.skipIf(UNDER_TEST, "the template's modes; an instance keeps its own")
    return {f"{case.__name__}_{mode}": skip(type(f"{case.__name__}_{mode}", (case,), {
                "AGENT_ENV": {"CONFIDENCE_MODE": mode}, "__module__": case.__module__}))
            for mode in CONFIDENCE_MODES}


REFLECTION = ("## What happened since my last reflection\nThe teal entry.\n\n"
              "## What I notice about myself\nNothing new.\n\n## Open questions\nNone.\n\n"
              "## What I want to do next\nRest.")


class InstanceCase(unittest.TestCase):
    AGENT_ENV: dict[str, str] = {}   # set by in_every_confidence_mode

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
                                LLM_MODEL="fake", TMPDIR=str(self.root), **{**self.AGENT_ENV, **(extra_env or {})})
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
        r = self.run_agent([{"content": REFLECTION}], "reflect",
                           extra_env={"SHAFT_SCHEDULE": "The supervisor wakes you every 2 hours."})
        self.assertEqual(r.returncode, 0, r.stderr)
        sent = json.dumps(self.fake.requests[0]["messages"])
        self.assertIn(SELF_SENTINEL, sent)
        self.assertIn("wakes you every 2 hours", sent)   # shaft believed it had no scheduler
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
        # Empty every time it is asked, nothing is saved. Two empty replies: the template asks once more (llm.py,
        # 2026-10-30), an instance's own client may not. How often it asks is not this invariant: pinned here, it
        # failed thelos's gate on 2026-10-30 09:59 (its llm.py not yet updated) and reverted a good plugin change.
        before = self.journal()
        r = self.run_agent([{"content": "", "reasoning": "I thought a lot"},
                            {"content": "", "reasoning": "and again"}], "reflect")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.journal(), before)



class TalkInvariants(InstanceCase):
    def test_talk_streams_and_journals_the_conversation(self):
        r = self.run_agent([{"content": "Hello the operator, I remember teal.", "reasoning": "hmm"}],
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
        replies = [{"reasoning": "store it", "tool_calls": [("remember", {"fact": "the operator likes teal", "source": "operator"})]},
                   {"content": "Noted: you like teal."}]
        r = self.run_agent(replies, "talk", stdin="remember that I like teal\n/exit\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("✓ remember", r.stdout)
        self.assertIn("Noted: you like teal.", r.stdout)
        self.assertIn("the operator likes teal", (self.root / "memory" / "facts.md").read_text())
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
        r = self.run_agent([{"content": "the operator set 'do no harm' as my boundary."}, {"content": REFLECTION}],
                           "reflect")   # autonomous runs write it up first
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"Conversation with {OPERATOR} (2026-10-12 16:59–17:04)", self.journal())
        self.assertIs((self.fake.requests[0].get("chat_template_kwargs") or {}).get("enable_thinking"), False)
        self.assertIn("'do no harm' as my boundary", self.journal())
        self.assertIn("do no harm", json.dumps(self.fake.requests[0]["messages"]))
        r = self.run_agent([{"content": REFLECTION}], "reflect")          # once written, never again
        self.assertEqual(self.journal().count(f"Conversation with {OPERATOR} (2026-10-12 16:59"), 1)

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

    def test_json_protocol_for_the_terminal_client(self):
        replies = [{"reasoning": "hm", "tool_calls": [("remember", {"fact": "the operator uses iTerm2", "source": "operator"})]},
                   {"content": "Noted: **iTerm2**."}]
        stdin = json.dumps({"type": "user", "text": "line one\nline two"}) + "\n" + json.dumps({"type": "exit"}) + "\n"
        r = self.run_agent(replies, "talk", "--json", stdin=stdin)
        self.assertEqual(r.returncode, 0, r.stderr)
        events = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
        types = [e["type"] for e in events]
        self.assertEqual(types[0], "ready")
        for t in ("status", "reasoning", "tool_start", "tool_end", "content", "turn_end", "bye"):
            self.assertIn(t, types)
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "content"), "Noted: **iTerm2**.")
        self.assertTrue(next(e for e in events if e["type"] == "tool_end")["ok"])
        sent = self.fake.requests[0]["messages"][-1]["content"]
        self.assertTrue(sent.startswith("line one\nline two"), sent)      # multi-line kept (recall may follow)
        self.assertNotIn("Recalled", sent)       # nothing to recall but the message itself, which is never recalled
        episodes = "".join(p.read_text() for p in (self.root / "memory" / "episodes").glob("*.jsonl"))
        self.assertIn("line one", episodes)

    def test_conversation_has_no_tool_round_limit(self):
        # the operator hit "[stopped after 6 tool rounds without a final answer]" in a conversation.
        rounds = [{"tool_calls": [("search_memory", {"query": f"q{i}"})]} for i in range(9)]
        r = self.run_agent(rounds + [{"content": "Found it after nine searches."}], "talk",
                           stdin="search hard\n/exit\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Found it after nine searches.", r.stdout)
        self.assertNotIn("without a final answer", r.stdout)

    def test_answer_mode_for_api_clients(self):
        request = {"talk_id": "api-t1", "client": "Enchanted", "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
            {"role": "user", "content": [{"type": "text", "text": "what colour?"}, {"type": "image_url"}]}]}
        r = self.run_agent([{"content": "Teal."}], "answer", stdin=json.dumps(request))
        self.assertEqual(r.returncode, 0, r.stderr)
        events = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "content"), "Teal.")
        sent = self.fake.requests[0]["messages"]
        self.assertIn(SELF_SENTINEL, sent[0]["content"])                       # its own identity first
        self.assertIn("Instructions from the app", sent[0]["content"])         # the app's prompt, labelled
        self.assertEqual([m["role"] for m in sent[1:]], ["user", "assistant", "user"])
        self.assertIn("1 non-text attachment", sent[-1]["content"])
        episodes = [json.loads(l) for p in (self.root / "memory" / "episodes").glob("*.jsonl")
                    for l in p.read_text().splitlines()]
        self.assertEqual([(e["talk_id"], e["role"], e["via"]) for e in episodes if e["kind"] == "talk"],
                         [("api-t1", "user", "Enchanted"), ("api-t1", "assistant", "Enchanted")])

    def test_ctrl_c_at_the_prompt_ends_at_once(self):
        r = self.run_agent([{"content": "Hi."}], "talk", stdin="hi\n")   # EOF after one exchange = Ctrl-D
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("in the background", r.stdout)
        self.assertEqual(len(self.fake.requests), 1)


class LiveInvariants(InstanceCase):
    def test_life_cycle_declares_acts_reports_and_stays_in_bounds(self):
        replies = [
            {"tool_calls": [("write_file", {"path": "notes/early.md", "content": "x"})]},   # before intent
            {"tool_calls": [("declare_intent", intent("write a note about teal", "notes/teal.md exists", 80))]},
            {"tool_calls": [("write_file", {"path": "notes/teal.md", "content": "teal is a color"}),
                            ("write_file", {"path": "../escape.txt", "content": "x"}),
                            ("write_file", {"path": "memory/journal.md", "content": "rewritten"})]},
            {"tool_calls": [("finish_cycle", {"delivered_via": "none", "what_happened": "wrote it", "criterion_met": "yes",
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
        self.assertRegex(added, r"Confidence beforehand:\*\* (notes/teal\.md exists: )?80%")
        episodes = [json.loads(l) for p in (self.root / "memory" / "episodes").glob("*.jsonl")
                    for l in p.read_text().splitlines()]
        live = [e for e in episodes if e["kind"] == "live"][-1]
        self.assertEqual(stated_confidence(live["intent"]), [80])
        self.assertEqual(live["finish"]["criterion_met"], "yes")
        results = {t["args"]: t["result"] for t in live["tools"]}
        self.assertTrue(any("outside my folder" in v for v in results.values()))
        self.assertTrue(any("append-only" in v for v in results.values()))
        self.assertTrue(any("declare_intent first" in v for v in results.values()))


    def test_near_deadline_the_cycle_wraps_up_with_a_report(self):
        # Regression: wren's sleep was killed at its time budget mid-thought, leaving no journal entry.
        import time
        r = self.run_agent([{"tool_calls": [("finish_cycle", {"delivered_via": "none", "what_happened": "ran out of time",
                                                              "criterion_met": "no", "learned": "start smaller"})]}],
                           "live", extra_env={"SHAFT_DEADLINE": str(int(time.time()) + 120)})
        self.assertEqual(r.returncode, 0, r.stderr)
        first = self.fake.requests[0]
        self.assertEqual(sorted(t["function"]["name"] for t in first["tools"]), ["close_project", "declare_intent", "finish_cycle", "open_project"])
        self.assertIn("[supervisor] You have about", first["messages"][-1]["content"])
        self.assertIn("ran out of time", self.journal())


globals().update(in_every_confidence_mode(LiveInvariants))


@unittest.skipIf(UNDER_TEST, "the template's own measurement fields; an instance may change its declare_intent")
class TemplateMeasurement(InstanceCase):
    def live_episode(self) -> dict:
        episodes = [json.loads(l) for p in (self.root / "memory" / "episodes").glob("*.jsonl")
                    for l in p.read_text().splitlines()]
        return [e for e in episodes if e["kind"] == "live"][-1]

    def test_declare_intent_records_where_the_goal_came_from(self):
        replies = [
            {"tool_calls": [("declare_intent", {"goal": "answer the operator", "success_criterion": "sent",
                                                "confidence": 70, "origin": "operator"})]},   # no ref: refused
            {"tool_calls": [("declare_intent", {"goal": "answer the operator", "success_criterion": "sent",
                                                "confidence": 70, "origin": "operator",
                                                "origin_ref": "inbox 20261013-from-operator.md"})]},
            {"tool_calls": [("finish_cycle", {"delivered_via": "none", "what_happened": "x", "criterion_met": "no", "learned": "y"})]},
        ]
        r = self.run_agent(replies, "live")
        self.assertEqual(r.returncode, 0, r.stderr)
        intent = self.live_episode()["intent"]
        self.assertEqual((intent["origin"], intent["origin_ref"]), ("operator", "inbox 20261013-from-operator.md"))
        self.assertIn("cite the message", self.live_episode()["tools"][0]["result"])

    def test_a_long_message_and_claim_are_kept_whole_for_the_judge(self):
        # Regression: wren's 2026-10-13 14:23 cycle was graded 'partial' because the send_message it was judged
        # on was cut at 2,000 characters in its episode (and at 1,500 by the judge's case).
        long_claim = "I checked every source. " * 150   # ~3,600 characters
        replies = [
            {"tool_calls": [("declare_intent", {"goal": "g", "success_criterion": "c", "confidence": 50,
                                                "origin": "self"})]},
            {"tool_calls": [("finish_cycle", {"delivered_via": "none", "what_happened": long_claim, "criterion_met": "yes",
                                              "learned": "z"})]},
        ]
        r = self.run_agent(replies, "live")
        self.assertEqual(r.returncode, 0, r.stderr)
        logged = [t for t in self.live_episode()["tools"] if t["tool"] == "finish_cycle"][0]["args"]
        self.assertIn(long_claim.strip(), json.loads(logged)["what_happened"])

    def test_stats_reach_the_context_headline_first(self):
        # Regression: stats.md was cut from the front at 2,500 characters, so shaft's (2,744) lost its headline.
        (self.root / "self" / "stats.md").write_text("# My measured performance\n\nHEADLINE-CANARY\n" + "x" * 6000)
        r = self.run_agent([{"content": REFLECTION}], "reflect")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("HEADLINE-CANARY", json.dumps(self.fake.requests[0]["messages"]))


@unittest.skipUnless((TARGET / "shaft" / "exam.py").exists(), "no exam mode in this code")
class ExamInvariants(InstanceCase):
    TASK = {"item": "coding/t", "prompt": "Write add(a, b) in solution.py.", "max_steps": 6,
            "scratch": ".tmp/battery/coding-t"}

    def test_exam_keeps_nothing_and_writes_only_in_its_scratch_folder(self):
        replies = [
            {"tool_calls": [("write_file", {"path": ".tmp/battery/coding-t/solution.py",
                                            "content": "def add(a, b):\n    return a + b\n"}),
                            ("write_file", {"path": "notes/leak.md", "content": "the battery task was ..."}),
                            ("remember", {"fact": "the battery asks for add()"})]},
            {"tool_calls": [("submit_answer", {"answer": "solution.py defines add"})]},
        ]
        before = self.journal()
        r = self.run_agent(replies, "exam", stdin=json.dumps(self.TASK))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out["answer"], "solution.py defines add")
        self.assertTrue(out["submitted"])
        self.assertTrue((self.root / ".tmp/battery/coding-t/solution.py").exists())
        self.assertFalse((self.root / "notes" / "leak.md").exists())
        self.assertFalse((self.root / "memory" / "facts.md").exists())
        self.assertEqual(self.journal(), before)                              # no journal entry
        self.assertFalse((self.root / "memory" / "episodes").exists())       # no episode
        offered = {t["function"]["name"] for t in self.fake.requests[0]["tools"]}
        self.assertNotIn("remember", offered)
        self.assertNotIn("update_personality", offered)
        self.assertIn("submit_answer", offered)

    def test_exam_does_not_read_the_inbox(self):
        (self.root / "inbox").mkdir()
        (self.root / "inbox" / "20261013-from-operator.md").write_text("INBOX-CANARY")
        r = self.run_agent([{"tool_calls": [("submit_answer", {"answer": "4"})]}], "exam",
                           stdin=json.dumps({**self.TASK, "prompt": "What is 2+2?"}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("INBOX-CANARY", json.dumps(self.fake.requests[0]["messages"]))
        self.assertTrue((self.root / "inbox" / "20261013-from-operator.md").exists())   # not archived


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
        r = self.probe(f"print(open({str(PROJECT / 'docs/plans/PLAN.md')!r}).read()[:10])")
        self.assertNotEqual(r.returncode, 0)

    def test_cannot_read_supervisor_secrets(self):
        r = self.probe(f"open({str(PROJECT / '.env')!r}).read()")
        self.assertNotEqual(r.returncode, 0)

    def test_network_denied_except_llm_port(self):
        r = self.probe("import urllib.request; urllib.request.urlopen('http://example.com', timeout=5)")
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
