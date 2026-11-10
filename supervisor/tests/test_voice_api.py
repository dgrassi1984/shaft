"""Lane L2: a spoken turn through the API — the flag reaches the instance, the stream gains its own
events on voice requests only, and a judge reading a spoken turn is told it is transcribed speech."""
from __future__ import annotations

import json
import unittest
import urllib.request
from datetime import datetime, timedelta
from unittest import mock

from supervisor import api, judge, metrics, tom
from supervisor.tests import test_api as base   # the module, not its classes: their tests must not run twice

# A reply that narrates, checks the weather with a tool, and answers.
WEATHER = [{"type": "ready", "name": "wisp"}, {"type": "status", "phase": "waiting for the model", "detail": ""},
           {"type": "content", "text": "Let me check "}, {"type": "content", "text": "the weather."},
           {"type": "tool_start", "name": "get_weather", "args": '{"city": "Rome"}'},
           {"type": "tool_end", "name": "get_weather", "ok": True, "brief": "sunny"},
           {"type": "status", "phase": "waiting for the model", "detail": ""},
           {"type": "content", "text": "It is "}, {"type": "content", "text": "sunny."},
           {"type": "turn_end", "steps": 2, "ended_by": "answer"}]

# The typed stream of WEATHER as the API wrote it before voice existed (captured on main, 2026-11-09): delta,
# finish_reason, top-level extras.
TYPED = [[{"role": "assistant", "content": ""}, None, {}],
         [{"reasoning_content": "waiting for the model\n"}, None, {}],
         [{"reasoning_content": "Let me check the weather.\n"}, None, {}],
         [{"reasoning_content": "⚙ get_weather {\"city\": \"Rome\"}\n"}, None, {}],
         [{"reasoning_content": "✓ get_weather\n"}, None, {}],
         [{"reasoning_content": "waiting for the model\n"}, None, {}],
         [{"content": "It is sunny."}, None, {}],
         [{}, "stop", {"interjections_heard": 0}],
         "[DONE]"]


def event(**e):
    return [{}, None, {"shaft_event": e}]


STARTED = event(type="status", phase="started", detail="")
TYPED_STATUS = [TYPED[0], STARTED, TYPED[1], event(type="status", phase="waiting for the model", detail=""),
                *TYPED[2:6], event(type="status", phase="waiting for the model", detail=""), *TYPED[6:]]

VOICE = [TYPED[0], STARTED, TYPED[1], event(type="status", phase="waiting for the model", detail=""),
         event(type="draft", text="Let me check "), event(type="draft", text="the weather."),
         event(type="step_end", answer=False),
         TYPED[2], TYPED[3], event(type="tool_start", name="get_weather"),
         TYPED[4], event(type="tool_end", name="get_weather", ok=True),
         TYPED[5], event(type="status", phase="waiting for the model", detail=""),
         event(type="draft", text="It is "), event(type="draft", text="sunny."),
         event(type="step_end", answer=True),
         TYPED[6], TYPED[7], TYPED[8]]


class VoiceApiTest(unittest.TestCase):
    setUp = base.ApiTest.setUp
    req = base.ApiTest.req

    def chat(self, events=WEATHER, **extra):
        body = {"model": "wisp", "stream": True, "messages": [{"role": "user", "content": "weather in Rome?"}], **extra}
        with mock.patch.object(base, "EVENTS", events):
            with self.req("/chat/completions", body) as response:
                raw = response.read().decode()
        out = []
        for block in raw.split("\n\n"):
            if block.startswith("data: {"):
                chunk = json.loads(block[6:])
                choice = chunk.pop("choices")[0]
                for k in ("id", "object", "created", "model"):
                    chunk.pop(k)
                out.append([choice["delta"], choice["finish_reason"], chunk])
            elif block.startswith("data: "):
                out.append(block[6:])
        return out

    def test_a_voice_request_tells_the_talk_process_and_a_typed_one_does_not(self):
        """Bug guarded: the app's voice flag stopped at the API, so the instance never knew it was heard aloud (no
        note, no voice effort, no `voice` on its records); or a typed request was marked spoken."""
        self.chat(voice=True)
        self.assertIs(base.fake_run_agent.request["voice"], True)
        for extra in ({}, {"voice": False}, {"voice": "true"}, {"voice": 1}):   # only a JSON true is spoken
            with self.subTest(extra=extra):
                self.chat(**extra)
                self.assertNotIn("voice", base.fake_run_agent.request)

    def test_a_typed_request_adds_status_events_without_voice_drafts(self):
        """Typed streams carry scheduling status but never voice drafts or step boundaries."""
        for extra in ({}, {"voice": False}, {"voice": "1"}):
            with self.subTest(extra=extra):
                self.assertEqual(self.chat(**extra), TYPED_STATUS)

    def test_a_voice_request_streams_drafts_and_step_ends_around_the_typed_stream(self):
        """Bug guarded: the answer held back until the step ended (M0: no model streamed its answer to the app), or
        narration before a tool call passed off as the answer. Drafts arrive as written, `step_end{answer:false}`
        closes the narration before the tool starts, and `content` is what a typed request gets."""
        got = self.chat(voice=True)
        self.assertEqual(got, VOICE)
        self.assertEqual([c for c in got if not (isinstance(c, list) and "shaft_event" in c[2])], TYPED)
        kinds = [c[2]["shaft_event"]["type"] if isinstance(c, list) and "shaft_event" in c[2] else
                 "content" if isinstance(c, list) and c[0].get("content") else None for c in got]
        self.assertLess(kinds.index("draft"), kinds.index("step_end"))
        narration_end = kinds.index("step_end")
        self.assertIs(got[narration_end][2]["shaft_event"]["answer"], False)
        self.assertLess(narration_end, kinds.index("tool_start"))
        self.assertLess(max(i for i, k in enumerate(kinds) if k == "draft"), kinds.index("content"))
        for c in got:
            if isinstance(c, list) and "shaft_event" in c[2]:
                self.assertEqual((c[0], c[1]), ({}, None), "an event chunk must carry nothing an OpenAI client shows")

    def test_a_voice_reply_cut_short_still_closes_its_drafts(self):
        """Bug guarded: a voice reply that ended without turn_end (interrupted) left its drafts open: the app could not
        tell whether what it spoke was the answer. The text so far is the content, as for a typed reply."""
        cut = [e for e in WEATHER if e["type"] != "turn_end"]
        got = self.chat(cut, voice=True)
        self.assertEqual(got[-4:-2], [event(type="step_end", answer=True), [{"content": "It is sunny."}, None, {}]])

    def test_drafts_of_a_call_cut_off_and_asked_again_are_closed_as_not_the_answer(self):
        """Bug guarded: the drafts of a model call cut off at max_tokens (discarded by llm._answer) left open, so the
        app took them for the start of the answer the second call then wrote again."""
        got = self.chat(base.ApiTest.CUT_THEN_ASKED_AGAIN, voice=True)
        self.assertEqual(got[2:8], [event(type="draft", text="Teal is a dark cy"), event(type="step_end", answer=False),
                                    [{"reasoning_content": "reply cut off at 16384 tokens — asking again with 32768\n"},
                                     None, {}],
                                    event(type="draft", text="Teal is "), event(type="draft", text="blue-green."),
                                    event(type="step_end", answer=True)])
        self.assertEqual(got[8], [{"content": "Teal is blue-green."}, None, {}])

    def test_a_spoken_interjection_is_marked_for_the_agent_and_the_reply_scoring(self):
        """Bug guarded: a spoken interjection recorded as typed (C1.3), so its talk episode and the reply-prediction
        judge treated recognition errors as the operator's words."""
        talk_id = api.Handler.conversations.talk_id("wisp", [{"role": "user", "content": "weather?"}])
        entry = api.Handler.live.begin("wisp", talk_id)
        api.Handler.live.interject(talk_id, "held, spoken", True)          # before the agent starts: held
        api.Handler.live.started(entry)
        api.Handler.live.interject(talk_id, "typed", False)
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {base.KEY}",
                   "Tailscale-User-Login": base.ME}
        body = {"model": "wisp", "voice": True, "messages": [{"role": "user", "content": "weather?"},
                                                              {"role": "user", "content": "and tomorrow"}]}
        with urllib.request.urlopen(urllib.request.Request(self.base + "/chat/interject",
                                                           data=json.dumps(body).encode(), headers=headers),
                                    timeout=10) as response:
            self.assertEqual(json.load(response)["talk_id"], talk_id)
        lines = [json.loads(l) for l in (self.roots / "wisp" / ".tmp" / f"interject-{talk_id}.jsonl")
                 .read_text().splitlines()]
        self.assertEqual(lines, [{"text": "held, spoken", "voice": True}, {"text": "typed"},
                                 {"text": "and tomorrow", "voice": True}])
        self.assertEqual([r.get("voice") for r in tom._rows("wisp", "-operator-said")], [True])
        api.Handler.live.end(entry)


class SpokenTurnJudgeTest(unittest.TestCase):
    """Bug guarded: a judge read transcribed speech as the operator's exact words ("music" for "mutex", M0)."""

    def test_the_origin_judge_is_told_a_spoken_turn_is_transcribed(self):
        at = datetime(2026, 11, 9, 12, 0)
        turns = [{"kind": "talk", "role": "user", "ts": (at - timedelta(hours=1)).isoformat(), "content": "fix the music",
                  "voice": True},
                 {"kind": "talk", "role": "user", "ts": (at - timedelta(minutes=30)).isoformat(), "content": "typed"}]
        with mock.patch.object(metrics, "_episodes", return_value=turns), \
                mock.patch.object(metrics, "list_beneath", return_value=[]), \
                mock.patch.object(metrics, "_instance_text", return_value=""):
            text = metrics._provenance_evidence("wisp", {"ts": at.isoformat()})
        lines = [l for l in text.splitlines() if l.startswith("- ")]
        self.assertEqual(len(lines), 2)
        self.assertIn(f"operator ({judge.SPOKEN}): fix the music", lines[0])
        self.assertIn("operator: typed", lines[1])
        self.assertIn("recognition errors", judge.SPOKEN)

    def test_the_reply_prediction_judge_is_told_a_spoken_reply_is_transcribed(self):
        asked = []
        with mock.patch.object(judge, "ask", side_effect=lambda system, prompt, *a, **k: asked.append(prompt) or {}):
            tom._grade("wisp", "yes, go", {"text": "yes go", "voice": True})
            tom._grade("wisp", "yes, go", {"text": "yes go"})
        self.assertEqual(json.loads(asked[0]).get("reply_is"), judge.SPOKEN)
        self.assertNotIn("reply_is", json.loads(asked[1]))
        row = {"at": "2026-11-09T12:00:00", "channel": "web", "text": "yes go", "voice": True}
        reply = tom._reply_for("wisp", {"sent_at": "2026-11-09T11:00:00", "within_hours": 2, "to": "operator"}, [],
                               [row])
        self.assertIs(reply["voice"], True)


if __name__ == "__main__":
    unittest.main()
