"""PLAN-voice M1, lane L2, the instance's side: a spoken turn (`request["voice"] is True`) gets the voice note and the
voice reasoning effort, both its episodes say `voice`, and a typed turn is unchanged."""
from __future__ import annotations

import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor.tests.test_autonomy_gaps_template import InstanceFixture
from shaft import config, prompts, talk


class VoiceTurnTest(InstanceFixture, unittest.TestCase):
    def answer(self, request: dict, replies: list[dict], **env) -> list[dict]:
        """One talk.answer against the fake model server; returns the request bodies the model got."""
        self.cfg = config.load(self.root, environ={"LLM_BASE_URL": "http://127.0.0.1:1/v1", "LLM_MODEL": "fake",
                                                   **env})
        fake, client = self.model(replies)
        client.reasoning_effort = self.cfg.LLM_REASONING_EFFORT     # as Client.from_config sets it
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            talk.answer(self.cfg, self.state, client, {"talk_id": "voice-1", **request,
                                                       "messages": [{"role": "user", "content": "Weather in Rome?"}]})
        return fake.requests

    @staticmethod
    def system(body: dict) -> str:
        return body["messages"][0]["content"]

    @staticmethod
    def effort(body: dict) -> str | None:
        return body["chat_template_kwargs"].get("reasoning_effort")

    def test_a_voice_turn_carries_the_note_and_marks_both_episodes_and_a_typed_one_neither(self):
        """Bug guarded: the instance not told it is heard aloud (tables and paths read out), or a typed turn told it
        is; and talk episodes without `voice`, so records and judges could not tell a spoken turn."""
        for request, spoken in (({"voice": True}, True), ({}, False), ({"voice": "true"}, False)):
            with self.subTest(request=request):
                bodies = self.answer(request, [{"content": "Sunny."}])
                self.assertEqual(prompts.VOICE_NOTE in self.system(bodies[0]), spoken)
                turn = [e for e in self.episodes() if e.get("kind") == "talk"][-2:]
                self.assertEqual([e["role"] for e in turn], ["user", "assistant"])
                self.assertEqual([e.get("voice") for e in turn], [True, True] if spoken else [None, None])
                self.assertTrue(all("voice" in e for e in turn) if spoken else not any("voice" in e for e in turn))

    def test_voice_effort_reaches_every_model_call_of_a_voice_turn_and_never_a_typed_one(self):
        """Bug guarded: VOICE_REASONING_EFFORT ignored on a spoken reply (M0: low cut the first word from 3.3 s to
        2.0 s), applied to only its first model call, or leaking into typed replies."""
        tool_then_answer = [{"tool_calls": [("read_file", {"path": "notes/weather.md"})]}, {"content": "Sunny."}]
        voice = self.answer({"voice": True}, list(tool_then_answer), VOICE_REASONING_EFFORT="low")
        typed = self.answer({}, list(tool_then_answer), VOICE_REASONING_EFFORT="low")
        self.assertEqual([self.effort(b) for b in voice], ["low", "low"])
        self.assertEqual([self.effort(b) for b in typed], ["medium", "medium"])

    def test_an_empty_voice_effort_falls_back_to_the_instance_effort(self):
        """Bug guarded: an unset VOICE_REASONING_EFFORT sending an empty effort (the model server's default, xhigh on
        Bonsai) instead of LLM_REASONING_EFFORT."""
        bodies = self.answer({"voice": True}, [{"content": "Sunny."}], LLM_REASONING_EFFORT="xhigh")
        self.assertEqual(self.effort(bodies[0]), "xhigh")

    def test_a_spoken_interjection_is_marked_on_its_episode(self):
        """Bug guarded: a message the operator spoke while the instance replied recorded as typed."""
        (self.root / ".tmp").mkdir()
        (self.root / ".tmp" / "interject-voice-1.jsonl").write_text(
            '{"text": "and tomorrow?", "voice": true}\n{"text": "typed"}\n')
        took = talk.Interjections(self.cfg, self.state, "voice-1", "app", mock.Mock())()
        self.assertEqual(len(took), 2)
        heard = [e for e in self.episodes() if e.get("interjected")]
        self.assertEqual([(e["content"], e.get("voice")) for e in heard], [("and tomorrow?", True), ("typed", None)])


class VoiceConfigTest(unittest.TestCase):
    def load(self, **env):
        root = Path(tempfile.mkdtemp(prefix="shaft-voice-cfg-"))
        self.addCleanup(shutil.rmtree, root, True)
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            return config.load(root, environ={"LLM_BASE_URL": "x", "LLM_MODEL": "m", **env})

    def test_voice_knobs_default_off_and_empty_and_refuse_other_values(self):
        """Bug guarded: voice mode on for every instance by default (Thelos only was decided), or a voice effort the
        model server refuses (Bonsai's template raises on an unknown effort, failing every spoken reply)."""
        cfg = self.load()
        self.assertEqual((cfg.VOICE_MODE, cfg.VOICE_REASONING_EFFORT), ("off", ""))
        cfg = self.load(VOICE_MODE="on", VOICE_REASONING_EFFORT="low")
        self.assertEqual((cfg.VOICE_MODE, cfg.VOICE_REASONING_EFFORT), ("on", "low"))
        cfg = self.load(VOICE_MODE="yes", VOICE_REASONING_EFFORT="max")
        self.assertEqual((cfg.VOICE_MODE, cfg.VOICE_REASONING_EFFORT), ("off", ""))
        self.assertEqual(config.CHOICES["VOICE_REASONING_EFFORT"], config.CHOICES["LLM_REASONING_EFFORT"])

    def test_the_supervisor_still_reads_the_efforts_without_importing_the_code(self):
        """Bug guarded: CHOICES no longer a plain literal (opconfig.effort_choices parses it with ast), which would
        silently drop the effort check of every settings page."""
        from supervisor import opconfig
        root = Path(tempfile.mkdtemp(prefix="shaft-voice-opconfig-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "shaft").mkdir()
        shutil.copy(Path(config.__file__), root / "shaft" / "config.py")
        self.assertEqual(opconfig.effort_choices(root), tuple(config.CHOICES["LLM_REASONING_EFFORT"]))

    def test_the_voice_note_never_tells_the_model_to_avoid_tools_or_documents(self):
        """Bug guarded: a "no tools" / "read-only" / "don't write" line in the note: gpt-6.1-sol obeys such lines
        literally and stopped correcting and writing (memory sol-obeys-prompts-literally)."""
        note = prompts.VOICE_NOTE.lower()
        for banned in ("no tool", "without tool", "read-only", "read only", "don't write", "do not write",
                       "don't use", "do not use", "avoid"):
            self.assertNotIn(banned, note)
        self.assertIn("use your tools as you normally would", note)
        self.assertIn("document", note)


if __name__ == "__main__":
    unittest.main()
