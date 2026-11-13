"""Thread replies in the fixture: the seeded cycle answer shows in its thread, the answer for an ended thread in From
with `re`, and Next cycle sent from a thread (`cid` with `mode=background`) stays in that thread."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import fixture_server as fixture

THREAD = "web-3f9a1c0d7e2b4a65"
ENDED = "web-77ab02c4e9d15f30"


class FixtureThreadRepliesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="shaft-fixture-test-")
        cls.addClassCleanup(cls.directory.cleanup)   # also when setUpClass fails (tearDownClass is then skipped)
        cls.previous = os.environ.get("SHAFT_SUPERVISOR_DIR")
        base = Path(cls.directory.name)
        cls.web, cls.chat, messages, _ = fixture.setup(base, 8899)
        fixture.seed(base.resolve(), cls.web, cls.chat, messages)
        response = cls.web.respond(cls.web.Request("POST", "/app/v1/pair", {
            "host": "127.0.0.1:8899", "content-type": "application/json"},
            json.dumps({"link": cls.web.new_pairing("threads-test"), "platform": "ios"}).encode()))
        assert response.status == 200, response.body
        paired = json.loads(response.body)
        cls.token = f"{paired['device']}.{paired['secret']}"

    @classmethod
    def tearDownClass(cls):
        mock.patch.stopall()
        if cls.previous is None:
            os.environ.pop("SHAFT_SUPERVISOR_DIR", None)
        else:
            os.environ["SHAFT_SUPERVISOR_DIR"] = cls.previous

    def request(self, path, body=b"", content_type="application/json"):
        response = self.web.respond(self.web.Request("POST" if body else "GET", path, {
            "host": "127.0.0.1:8899", "authorization": "Bearer " + self.token, "content-type": content_type}, body))
        self.assertEqual(response.status, 200, response.body)
        return json.loads(response.body)

    def sent(self, path):
        return {i["text"]: i for i in self.request(path)["items"] if i["type"] == "sent"}

    def later(self, path):
        return [i["text"] for i in self.request(path)["items"] if i["type"] == "later"]

    def background(self, text, cid=None):
        return self.send(text, cid, "background")

    def send(self, text, cid=None, mode="now", name="tundra"):
        fields = {"text": text, "mid": "test-" + str(time.monotonic_ns()), "mode": mode}
        if cid is not None:
            fields["cid"] = cid
        body = "".join(f'--fixture\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
                       for k, v in fields.items()) + "--fixture--\r\n"
        return self.request(f"/app/v1/chat/{name}/send", body=body.encode(),
                            content_type="multipart/form-data; boundary=fixture")

    def test_the_cycle_answer_shows_in_its_thread_and_not_in_from(self):
        answer = self.sent(f"/app/v1/chat/tundra/{THREAD}")["March reconciles: 3 lines moved."]
        self.assertIs(answer["from_cycle"], True)
        self.assertIsNone(answer["re"])
        self.assertNotIn("March reconciles: 3 lines moved.", self.sent("/app/v1/from/tundra"))

    def test_the_answer_for_an_ended_thread_shows_in_from_with_re(self):
        answer = self.sent("/app/v1/from/tundra")["September closed clean too."]
        self.assertEqual(answer["re"]["cid"], ENDED)
        self.assertTrue(answer["re"]["title"])
        self.assertIs(answer["from_cycle"], False)

    def test_next_cycle_from_a_thread_stays_there_and_from_from_stays_in_from(self):
        said = self.background("In the thread, for its next cycle", cid=THREAD)
        self.assertIsNone(said["chat"])
        self.assertTrue(said["file"])
        self.background("From From, for its next cycle", cid="from")
        self.assertIn("In the thread, for its next cycle", self.later(f"/app/v1/chat/tundra/{THREAD}"))
        self.assertNotIn("In the thread, for its next cycle", self.later("/app/v1/from/tundra"))
        self.assertIn("From From, for its next cycle", self.later("/app/v1/from/tundra"))
        self.assertNotIn("From From, for its next cycle", self.later(f"/app/v1/chat/tundra/{THREAD}"))

    def test_from_answers_queues_and_stops_in_its_own_conversation(self):
        path = "/app/v1/from/wisp"
        sent = self.send("slow From conversation", cid="from", name="wisp")
        self.assertEqual(sent["chat"], "from")
        feed = self.request(path)
        self.assertEqual(feed["chat"]["title"], "wisp")
        # Always finish the fixture worker before its temporary directory is removed, even on a failed assertion.
        internal_id = feed["chat"]["id"]
        def finish_reply():
            relay = self.chat._relays.get(internal_id)
            if relay:
                relay.stop()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and self.chat.load(internal_id)["state"] == "replying":
                time.sleep(0.05)
        self.addCleanup(finish_reply)
        self.assertIsNotNone(feed["reply"])
        self.assertTrue(feed["can_stop"])
        self.assertFalse(feed["can_end"])
        self.assertFalse(feed["can_clear"])
        self.assertTrue(any(i["type"] == "user" for i in feed["items"]))
        row = self.request("/app/v1/threads/wisp")["from"]
        self.assertIn(row["state"], ("replying", "waiting"))
        self.assertIn("waiting", row)
        queued = self.send("After this reply", cid="from", mode="queue", name="wisp")
        self.assertEqual(queued["chat"], "from")
        queued_item = self.request(path)["queued"][0]
        self.request("/app/v1/chat/wisp/unqueue", json.dumps({"cid": "from", "mid": queued_item["mid"]}).encode())
        self.assertEqual(self.request(path)["queued"], [])
        self.request("/app/v1/chat/wisp/stop", b'{"cid":"from"}')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            feed = self.request(path)
            if feed["reply"] is None:
                break
            time.sleep(0.05)
        self.assertIsNone(feed["reply"])
        self.assertTrue(any(i["type"] == "assistant" for i in feed["items"]))
        self.assertFalse(any(t["title"] == "From wisp" for t in self.request("/app/v1/threads/wisp")["threads"]))

    def test_next_cycle_notes_can_be_cancelled_in_thread_and_from(self):
        thread_path = f"/app/v1/chat/tundra/{THREAD}"
        legacy = next(i for i in self.request(thread_path)["items"] if i.get("file") == "seed-next-cycle-cancel.md")
        self.assertIsNone(legacy["mid"])
        self.request("/app/v1/chat/tundra/uncycle", json.dumps({"file": legacy["file"]}).encode())
        cancelled = next(i for i in self.request(thread_path)["items"] if i.get("file") == legacy["file"])
        self.assertIsNotNone(cancelled["cancelled"])
        for cid, path in ((THREAD, f"/app/v1/chat/tundra/{THREAD}"), ("from", "/app/v1/from/tundra")):
            sent = self.background("Cancel this next-cycle note " + cid, cid=cid)
            note = next(i for i in self.request(path)["items"] if i.get("file") == sent["file"])
            self.assertTrue(note["cancellable"])
            self.assertIsNone(note["cancelled"])
            self.request("/app/v1/chat/tundra/uncycle", json.dumps({"mid": note["mid"]}).encode())
            cancelled = next(i for i in self.request(path)["items"] if i.get("file") == sent["file"])
            self.assertFalse(cancelled["cancellable"])
            self.assertIsNotNone(cancelled["cancelled"])


if __name__ == "__main__":
    unittest.main()
