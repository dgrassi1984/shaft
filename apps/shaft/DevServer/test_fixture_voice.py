"""Voice fixtures through the real app dispatcher, upstream SSE reader and reply stream."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import fixture_server as fixture


class FixtureVoiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        cls.previous = os.environ.get("SHAFT_SUPERVISOR_DIR")
        cls.web, cls.chat, _, _ = fixture.setup(Path(cls.directory.name), 8899)
        response = cls.web.respond(cls.web.Request("POST", "/app/v1/pair", {
            "host": "127.0.0.1:8899", "content-type": "application/json"},
            json.dumps({"link": cls.web.new_pairing("voice-test"), "platform": "macos"}).encode()))
        assert response.status == 200, response.body
        paired = json.loads(response.body)
        cls.token = f"{paired['device']}.{paired['secret']}"

    @classmethod
    def tearDownClass(cls):
        mock.patch.stopall()
        cls.directory.cleanup()
        if cls.previous is None:
            os.environ.pop("SHAFT_SUPERVISOR_DIR", None)
        else:
            os.environ["SHAFT_SUPERVISOR_DIR"] = cls.previous

    def request(self, path, data=None, body=b"", content_type="application/json"):
        response = self.web.respond(self.web.Request("POST" if data is not None or body else "GET", path, {
            "host": "127.0.0.1:8899", "authorization": "Bearer " + self.token,
            "content-type": content_type}, json.dumps(data).encode() if data is not None else body))
        self.assertEqual(response.status, 200, response.body)
        return response

    def send(self, text, voice=True):
        fields = {"text": text, "mid": "test-" + str(time.monotonic_ns()), "mode": "now"}
        if voice:
            fields["voice"] = "1"
        body = "".join(f'--fixture\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
                       for k, v in fields.items()) + "--fixture--\r\n"
        return json.loads(self.request("/app/v1/chat/tundra/send", body=body.encode(),
                                       content_type="multipart/form-data; boundary=fixture").body)

    def events(self):
        response = self.request("/app/v1/chat/tundra/stream")
        for block in response.stream:
            for line in block.splitlines():
                if line.startswith(b"data: "):
                    yield json.loads(line[6:]), time.monotonic()

    def check_reply(self, rows):
        events = [event for event, _ in rows]
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual(len({e["reply"] for e in events}), 1)
        self.assertEqual([e["type"] for e in events[-3:]], ["step_end", "answer", "end"])
        self.assertTrue(events[-3]["answer"])
        self.assertIsNone(events[-1]["failure"])
        return events

    def test_default_voice_stream(self):
        self.send("Describe the garden")
        rows = list(self.events())
        events = self.check_reply(rows)
        self.assertEqual([e["type"] for e in events], ["status"] + ["draft"] * 7 + ["step_end", "answer", "end"])
        self.assertEqual(events[0]["phase"], "model")
        self.assertEqual(events[0]["detail"], "thinking")
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "draft"),
                         "The garden gate is open. The lantern is beside the door.")
        self.assertEqual(events[-2]["text"], fixture.VOICE_ANSWER)
        self.assertGreaterEqual(rows[-1][1] - rows[0][1], 0.45)

    def test_tools_take_interjection_at_next_step(self):
        self.send("Use a tool")
        rows = []
        for event, at in self.events():
            rows.append((event, at))
            if event["type"] == "tool_start" and event["name"] == "read_file":
                sent = self.send("Check the blue gate too")
                self.assertEqual(sent["said"], "tundra reads it at its next step")
        events = self.check_reply(rows)
        self.assertEqual([e["type"] for e in events],
                         ["status", "draft", "step_end", "tool_start", "tool_end", "tool_start", "tool_end"]
                         + ["draft"] * 7 + ["step_end", "answer", "end"])
        self.assertFalse(events[2]["answer"])
        tools = [(e, t) for e, t in rows if e["type"].startswith("tool_")]
        self.assertEqual([e["name"] for e, _ in tools], ["read_file", "read_file", "web_search", "web_search"])
        self.assertTrue(all(e["ok"] for e, _ in tools if e["type"] == "tool_end"))
        self.assertGreaterEqual(tools[1][1] - tools[0][1], 1.45)
        self.assertGreaterEqual(tools[3][1] - tools[2][1], 1.45)
        self.assertLessEqual(tools[2][1] - tools[1][1], 0.5)
        self.assertGreaterEqual(tools[2][1] - tools[1][1], 0.25)
        chat = self.chat.current("tundra")
        self.assertEqual(chat["state"], "idle")
        self.assertTrue(chat["turns"][-2]["interjected"])
        self.assertEqual(chat["turns"][-2]["via"], "voice")

    def test_long_reply_and_late_interjection_gets_new_reply(self):
        self.send("A long garden story")
        rows = []
        for event, at in self.events():
            rows.append((event, at))
            if event["type"] == "draft" and event["seq"] == 3:
                self.send("Describe the gate instead")
        events = self.check_reply(rows)
        self.assertEqual(events[-2]["text"].count("."), 10)
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "draft"), events[-2]["text"])
        self.assertGreaterEqual(rows[-1][1] - rows[0][1], 5.8)
        # _finish emits end before starting the unheard interjection's follow-up.
        deadline = time.monotonic() + 2
        while self.chat.stream_of(self.chat.current("tundra")["id"]).id == events[0]["reply"]:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        followup = self.check_reply(list(self.events()))
        self.assertNotEqual(followup[0]["reply"], events[0]["reply"])
        self.assertEqual(followup[-2]["text"], fixture.VOICE_ANSWER)

    def test_typed_stream_only_answer_and_end(self):
        self.send("Describe the garden", voice=False)
        events = [e for e, _ in self.events()]
        self.assertEqual([e["type"] for e in events], ["answer", "end"])
        self.assertEqual([e["seq"] for e in events], [1, 2])
        self.assertEqual(events[0]["text"], fixture.ANSWER.format(ask="Describe the garden"))
        self.assertIsNone(events[1]["failure"])

    def test_voice_routes_round_trip(self):
        def api(path="/app/v1/voices/tundra", data=None):
            return json.loads(self.request(path, data).body)
        voices = api()
        self.assertEqual([v["language"] for v in voices["available"]["macos"]], ["en-US", "it-IT"])
        for lang in ("en", "it"):
            assigned = api(data={"language": lang, "identifier": "fixture.voice." + lang})
            self.assertEqual(assigned["voices"][lang]["identifier"], "fixture.voice." + lang)
            self.assertEqual(api()["voices"][lang]["by"], "operator")
            self.assertIsNone(api(data={"language": lang, "clear": True})["voices"][lang])
        rows = [{"identifier": "fixture.ios.en", "name": "Example", "language": "en-GB", "quality": "enhanced"}]
        self.assertEqual(api("/app/v1/voices/report", {"platform": "ios", "voices": rows})["available"]["ios"], rows)
        self.assertEqual(api()["available"]["ios"], rows)
        from supervisor import core, voices as store
        self.assertEqual(core.SUP_DIR, Path(self.directory.name).resolve() / "sup")
        proposal = store.propose("tundra", "en", "fixture.voice.en", "An invented choice")
        accepted = api("/app/v1/voices/tundra/proposals/" + proposal["id"], {"accept": True})
        self.assertEqual(accepted["voices"]["en"]["by"], "instance")
        proposal = store.propose("tundra", "it", "fixture.voice.it", "Another invented choice")
        rejected = api("/app/v1/voices/tundra/proposals/" + proposal["id"], {"accept": False, "reason": "Keep the default"})
        self.assertEqual(rejected["proposals"][-1]["state"], "rejected")


if __name__ == "__main__":
    unittest.main()
