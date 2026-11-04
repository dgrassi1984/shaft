"""Instances see images (docs/plans/PLAN-vision.md): native images for models that accept them, describe_image for every
instance. Against fakes: OpenRouter's model list behind vision.urlopen, the cloud model behind cloudllm.complete."""
from __future__ import annotations

import base64
import json
import os
import struct
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import battery, broker, cloudllm, core, metrics, runner, trial, vision

TEMPLATE = Path(__file__).resolve().parents[2] / "template"


def png(width: int, height: int, rgb=(200, 30, 30)) -> bytes:
    """A solid-colour PNG, written by hand (no imaging library here)."""
    row = b"\x00" + bytes(rgb) * width
    chunk = lambda kind, data: (struct.pack(">I", len(data)) + kind + data  # noqa: E731
                                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * height, 1)) + chunk(b"IEND", b""))


def image_part(data: bytes, media: str = "image/png") -> dict:
    return {"type": "image_url", "image_url": {"url": f"data:{media};base64,{base64.b64encode(data).decode()}"}}


class FakeModels:
    """OpenRouter's public model list."""

    def __init__(self, models: dict[str, list[str]] | None = None, fail: bool = False):
        self.models = models if models is not None else {"xiaomi/mimo-v2.6-pro": ["text", "image"],
                                                          "xiaomi/mimo-v2.5-pro": ["text"]}
        self.fail, self.calls = fail, 0

    def __call__(self, url, timeout=0):
        self.calls += 1
        if self.fail:
            raise urllib.error.URLError("offline")
        import io
        return io.BytesIO(json.dumps({"data": [{"id": k, "architecture": {"input_modalities": v}}
                                               for k, v in self.models.items()]}).encode())


class Case(unittest.TestCase):
    def setUp(self):
        self.sup = Path(tempfile.mkdtemp(prefix="shaft-vision-sup-"))
        self.models = FakeModels()
        for p in (mock.patch.object(core, "SUP_DIR", self.sup), mock.patch.object(vision, "SUP_DIR", self.sup),
                  mock.patch.object(vision, "urlopen", self.models)):
            p.start()
            self.addCleanup(p.stop)


class CapabilityTest(Case):
    def test_sees_images_learned_from_openrouter(self):
        self.assertTrue(vision.sees_images("openrouter/xiaomi/mimo-v2.6-pro"))
        self.assertFalse(vision.sees_images("openrouter/xiaomi/mimo-v2.5-pro"))
        self.assertFalse(vision.sees_images("openrouter/nobody/unknown"))
        self.assertTrue(vision.sees_images("claude-opus-5-5"))
        self.assertTrue(vision.sees_images("codex/gpt-6-astra"))
        self.assertFalse(vision.sees_images("gpu/bonsai2"))
        self.assertEqual(self.models.calls, 1)                    # cached: one fetch for all of them

    def test_cache_expires_and_a_failed_refresh_keeps_the_old_list(self):
        vision.sees_images("openrouter/xiaomi/mimo-v2.6-pro")
        cache = json.loads((self.sup / "models.json").read_text())
        cache["at"] -= vision.MODELS_TTL_S + 1
        (self.sup / "models.json").write_text(json.dumps(cache))
        self.models.fail = True
        with mock.patch.object(vision, "log") as log:
            self.assertTrue(vision.sees_images("openrouter/xiaomi/mimo-v2.6-pro"))
        self.assertEqual(self.models.calls, 2)
        self.assertIn("could not be refreshed", log.call_args[0][0])

    def test_unknown_without_any_list_is_an_error_and_no_for_the_run(self):
        self.models.fail = True
        with self.assertRaisesRegex(vision.VisionError, "no cached list"):
            vision.sees_images("openrouter/xiaomi/mimo-v2.6-pro")
        with mock.patch.object(vision, "log") as log:
            self.assertFalse(vision.sees_images_or_no("openrouter/xiaomi/mimo-v2.6-pro"))
        self.assertIn("treated as not seeing images", log.call_args[0][0])

    def test_agent_environment_says_whether_the_model_sees_images(self):
        root = Path(tempfile.mkdtemp(prefix="shaft-vision-inst-"))
        for model, seen in (("claude-opus-5-5", True), ("openrouter/xiaomi/mimo-v2.6-pro", True),
                            ("gpu/bonsai2", False), ("codex/gpt-6-astra", True)):
            (root / "config.env").write_text(f'LLM_MODEL="{model}"\n')
            self.assertEqual(runner._sees_images(root), seen, model)


class PrepareTest(Case):
    def test_image_parts_become_anthropic_blocks(self):
        small = png(40, 30)
        body = {"model": "claude-opus-5-5", "messages": [
            {"role": "user", "content": [{"type": "text", "text": "what is this?"}, image_part(small)]}]}
        out = cloudllm.to_anthropic(vision.prepare(body), {})
        blocks = out["messages"][0]["content"]
        self.assertEqual(blocks[0], {"type": "text", "text": "what is this?"})
        self.assertEqual(blocks[1]["type"], "image")
        self.assertEqual(blocks[1]["source"]["media_type"], "image/png")
        self.assertEqual(base64.b64decode(blocks[1]["source"]["data"]), small)

    def test_media_type_comes_from_the_bytes_not_the_url(self):
        body = {"model": "claude-opus-5-5", "messages": [
            {"role": "user", "content": [image_part(png(20, 20), media="image/jpeg")]}]}
        url = vision.prepare(body)["messages"][0]["content"][0]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))

    def test_non_vision_model_refuses_image_part(self):
        body = {"model": "gpu/bonsai2", "messages": [{"role": "user", "content": [image_part(png(20, 20))]}]}
        with self.assertRaisesRegex(vision.VisionError, "model 'gpu/bonsai2' cannot see images; this request "
                                                        "carries 1 \\(describe_image"):
            vision.prepare(body)

    def test_large_image_downscaled_and_capped(self):
        big = png(3000, 2000)
        media, data = vision.prepare_bytes(big)
        self.assertEqual(media, "image/jpeg")
        with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
            f.write(data)
            f.flush()
            self.assertEqual(vision._edge(f.name), vision.MAX_EDGE)
        small = png(100, 80)
        self.assertEqual(vision.prepare_bytes(small), ("image/png", small))   # left as it is
        body = {"model": "claude-opus-5-5", "messages": [
            {"role": "user", "content": [image_part(small) for _ in range(vision.MAX_IMAGES + 1)]}]}
        with self.assertRaisesRegex(vision.VisionError, f"at most {vision.MAX_IMAGES}"):
            vision.prepare(body)

    def test_only_data_urls_and_only_images(self):
        for url, why in (("https://example.com/x.png", "data: URL"), ("data:image/png;base64,@@@", "not valid base64")):
            body = {"model": "claude-opus-5-5", "messages": [
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}]}
            with self.subTest(url=url), self.assertRaisesRegex(vision.VisionError, why):
                vision.prepare(body)
        with self.assertRaisesRegex(vision.VisionError, "not a JPEG, PNG, GIF or WebP"):
            vision.prepare_bytes(b"%PDF-1.7 not an image")

    def test_openrouter_forwards_image_parts(self):
        body = {"model": "openrouter/xiaomi/mimo-v2.6-pro", "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hi"}, image_part(png(30, 30))]}]}
        out = vision.prepare(body)
        self.assertEqual(out["messages"][0]["content"][1]["type"], "image_url")   # the OpenAI format, as sent

    def test_proxy_refusal_names_the_model_not_bad_json(self):
        # VisionError is a ValueError: caught after the "body must be JSON" handler, every refusal read as bad JSON.
        root = Path(tempfile.mkdtemp(prefix="shaft-vision-proj-"))
        b = broker.Broker("shaft", root, {}, root / "broker.log")
        base = b.start()
        self.addCleanup(b.stop)
        body = {"model": "gpu/bonsai2", "messages": [{"role": "user", "content": [image_part(png(20, 20))]}]}
        req = urllib.request.Request(base + "/llm/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"X-Shaft-Token": b.token, "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 400)
        self.assertIn("cannot see images", json.load(ctx.exception)["error"]["message"])
        self.assertNotIn("base64", (root / "broker.log").read_text())


class DescribeTest(Case):
    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="shaft-vision-wren-")).resolve()
        (self.root / "attachments" / "from-operator" / "20261016-091200").mkdir(parents=True)
        self.photo = self.root / "attachments" / "from-operator" / "20261016-091200" / "photo-1.png"
        self.photo.write_bytes(png(64, 48))
        (self.root / "secrets").mkdir()
        (self.root / "secrets" / "x.png").write_bytes(png(8, 8))
        self.calls = []

        def complete(instance, body, secrets, remembered):
            self.calls.append((instance, body))
            return {"choices": [{"message": {"content": "A red rectangle."}, "finish_reason": self.finish.pop(0)
                                 if self.finish else "stop"}]}
        self.finish: list[str] = []
        p = mock.patch.object(cloudllm, "complete", complete)
        p.start()
        self.addCleanup(p.stop)

    def broker(self, **kw):
        return broker.Broker("wren", self.root.parent, {}, self.root / "b.log", root=self.root, **kw)

    def test_describe_image_confined_and_billed(self):
        b = self.broker(usage_name="wren")
        r = b.call("describe_image", {"path": "attachments/from-operator/20261016-091200/photo-1.png",
                                      "question": "what colour?"})
        self.assertTrue(r["ok"], r)
        self.assertIn("[described by claude-haiku-5-5, not seen by you", r["result"])
        self.assertIn("A red rectangle.", r["result"])
        instance, body = self.calls[0]
        self.assertEqual(instance, "wren")                                   # billed to the instance, in its cap
        self.assertEqual(body["model"], broker.DESCRIBE_MODEL)
        self.assertIn("what colour?", body["messages"][0]["content"][1]["text"])
        outside = Path(tempfile.mkdtemp(prefix="shaft-vision-out-")) / "o.png"
        outside.write_bytes(png(8, 8))
        (self.root / "link.png").symlink_to(outside)
        (self.root / "notes").mkdir()
        (self.root / "notes" / "n.txt").write_text("not an image")
        for path in ("../" + outside.name, str(outside), "secrets/x.png", "link.png", "notes/n.txt", "nope.png", ""):
            with self.subTest(path=path):
                self.assertFalse(b.call("describe_image", {"path": path})["ok"])
        for path in (str(outside), "link.png"):                              # refused for what it is, by name
            self.assertIn("give the path of an image file in your folder",
                          b.call("describe_image", {"path": path})["error"])
        self.assertEqual(len(self.calls), 1)                                 # nothing refused reached the model

    def test_a_cut_description_is_asked_again_then_refused(self):
        b = self.broker()
        self.finish = ["length", "stop"]
        self.assertTrue(b.call("describe_image", {"path": str(self.photo.relative_to(self.root))})["ok"])
        self.assertEqual([c[1]["max_tokens"] for c in self.calls], [2000, 8000])
        self.finish = ["length", "length"]
        r = b.call("describe_image", {"path": str(self.photo.relative_to(self.root))})
        self.assertIn("cut at 8000 tokens twice", r["error"])

    def test_at_most_ten_per_run(self):
        b = self.broker()
        rel = str(self.photo.relative_to(self.root))
        results = [b.call("describe_image", {"path": rel}) for _ in range(broker.DESCRIBE_CALLS_PER_RUN + 1)]
        self.assertTrue(all(r["ok"] for r in results[:-1]))
        self.assertIn("at most 10 describe_image calls per run", results[-1]["error"])

    def test_replay_pair_shares_one_description(self):
        cache, lock = {}, threading.Lock()

        def replay():
            state = Path(tempfile.mkdtemp(prefix="copy-")).resolve()
            (state / "a").mkdir()
            (state / "a" / "p.png").write_bytes(self.photo.read_bytes())
            return broker.ReplayBroker("wren", self.root.parent, {}, state / "b.log", state_root=state,
                                       start_commit="0", as_of=datetime.now(), cache=cache, cache_lock=lock,
                                       billed="wren@trial:t", tools_log=state / "t.log")
        a, b = replay(), replay()
        self.assertEqual(a.call("describe_image", {"path": "a/p.png"})["result"],
                         b.call("describe_image", {"path": "a/p.png"})["result"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "wren@trial:t")

    def test_battery_has_no_image_tools(self):
        self.assertIn("describe_image", battery.EXAM_OFF)


class ReplayAttachmentsTest(unittest.TestCase):
    def test_replay_sees_attachments_before_t0_only(self):
        src = Path(tempfile.mkdtemp(prefix="shaft-vision-src-"))
        dest = Path(tempfile.mkdtemp(prefix="shaft-vision-dst-"))
        for stamp in ("20261016-080000", "20261016-100000-123456"):
            d = src / "attachments" / "from-operator" / stamp
            d.mkdir(parents=True)
            (d / "photo.jpg").write_bytes(b"\xff\xd8\xff" + stamp.encode())
        outside = Path(tempfile.mkdtemp(prefix="shaft-vision-secret-"))
        (outside / "secret.txt").write_text("not the instance's")
        (src / "attachments" / "from-operator" / "20261016-070000").symlink_to(outside)
        early = src / "attachments" / "from-operator" / "20261016-080000"
        (early / "linked.txt").symlink_to(outside / "secret.txt")           # a link inside a real batch
        (early / "sub").symlink_to(outside)                                  # and a linked folder inside it
        n = trial.copy_attachments(src, dest, datetime(2026, 10, 16, 9, 0))
        self.assertEqual(n, 1)
        self.assertTrue((dest / "attachments/from-operator/20261016-080000/photo.jpg").is_file())
        self.assertFalse((dest / "attachments/from-operator/20261016-100000-123456").exists())
        self.assertFalse(list(dest.rglob("secret.txt")))


class JudgeNoteTest(unittest.TestCase):
    def test_judge_note_only_for_cycles_that_used_an_image(self):
        plain = {"tools": [{"tool": "read_file", "args": "{}", "result": "x"}]}
        seen = {"tools": [{"tool": "view_image", "args": "{}", "result": "a.png: shown below"}]}
        self.assertNotIn("## Images", metrics._case(plain))
        self.assertIn("You cannot see the image", metrics._case(seen))
        self.assertIn("## Images", metrics._case({"tools": [{"tool": "describe_image", "args": "{}", "result": ""}]}))


class TemplateTest(unittest.TestCase):
    """The template half: attaching images, view_image, keeping only the newest, nothing stored with bytes."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(TEMPLATE))
        from shaft import images, tools
        cls.images, cls.tools = images, tools

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shaft-vision-tpl-")).resolve()
        d = self.root / "attachments" / "from-operator" / "20261016-091200"
        d.mkdir(parents=True)
        (d / "photo-7.jpg").write_bytes(b"\xff\xd8\xff\xe0fake jpeg")
        self.line = "\n\n[The operator attached, saved in my folder:]\n- attachments/from-operator/20261016-091200/photo-7.jpg (1 KB)"

    def env(self, vision: bool):
        p = mock.patch.dict(os.environ, {"SHAFT_VISION": "1"} if vision else {}, clear=False)
        p.start()
        self.addCleanup(p.stop)
        if not vision:
            os.environ.pop("SHAFT_VISION", None)

    def test_attached_photo_goes_with_its_message(self):
        self.env(True)
        out = self.images.with_attached("look" + self.line, self.root)
        self.assertEqual(out[0]["type"], "text")
        self.assertTrue(out[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_no_vision_says_so(self):
        self.env(False)
        out = self.images.with_attached("look" + self.line, self.root)
        self.assertIsInstance(out, str)
        self.assertIn("My model cannot see images, so I have not seen this image", out)

    def test_only_images_the_message_itself_lists(self):
        # Memory recalled into the message can quote an old attachment line: it must not bring that photo back.
        self.env(True)
        self.assertEqual(self.images.with_attached("hello" + self.line, self.root, listed_in="hello"), "hello" + self.line)
        elsewhere = Path(tempfile.mkdtemp(prefix="shaft-vision-else-")).resolve()
        (elsewhere / "x.png").write_bytes(b"\x89PNG real file outside")
        rel = os.path.relpath(elsewhere / "x.png", self.root)
        out = self.images.with_attached(f"- {rel} (1 KB)\n- {elsewhere / 'x.png'} (1 KB)", self.root)
        self.assertIsInstance(out, str)                                      # nothing outside my folder

    def test_only_newest_images_kept(self):
        part = lambda n: {"type": "image_url", "image_url": {"url": "data:x"}, "shaft_path": f"/r/p{n}.jpg"}  # noqa: E731
        msgs = [{"role": "user", "content": [{"type": "text", "text": f"m{i}"}, part(i)]} for i in range(6)]
        self.images.keep_newest(msgs)
        carried = [p["shaft_path"] for m in msgs for p in m["content"] if p["type"] == "image_url"]
        self.assertEqual(carried, [f"/r/p{i}.jpg" for i in range(2, 6)])
        self.assertIn("image shown earlier, no longer in view: p0.jpg", msgs[0]["content"][1]["text"])
        sent = self.images.for_model(msgs)
        self.assertFalse([p for m in sent for p in m["content"] if "shaft_path" in p])

    def test_view_image_confined_to_folder(self):
        self.env(True)
        box = self.tools.Toolbox(self.root)
        self.assertIn("view_image", [t["function"]["name"] for t in box.specs()])
        self.assertIn("shown below", box.run("view_image", json.dumps(
            {"path": "attachments/from-operator/20261016-091200/photo-7.jpg"})))
        (self.root / "secrets").mkdir()
        (self.root / "secrets" / "k.png").write_bytes(b"\x89PNG")
        (self.root / "n.txt").write_text("x")
        for path in ("../x.png", "/etc/hosts.png", "secrets/k.png", "n.txt", "missing.png"):
            with self.subTest(path=path):
                self.assertTrue(box.run("view_image", json.dumps({"path": path})).startswith("ERROR"))
        self.assertEqual(len(box.shown), 1)
        self.env(False)
        self.assertNotIn("view_image", [t["function"]["name"] for t in self.tools.Toolbox(self.root).specs()])

    def test_agent_sends_view_image_after_the_tool_results(self):
        self.env(True)
        from shaft import agent
        from shaft.llm import Reply
        seen = []

        def call(msgs, t=None):
            seen.append(json.loads(json.dumps(msgs)))
            if len(seen) == 1:
                r = Reply(content="", max_tokens=10)
                r.tool_calls = [{"id": "c1", "name": "view_image",
                                 "arguments": json.dumps({"path": "attachments/from-operator/20261016-091200/photo-7.jpg"})}]
                return r
            return Reply(content="It is a photo.", max_tokens=10)
        box = self.tools.Toolbox(self.root)
        out = agent.run(None, [{"role": "user", "content": "what did I send?"}], box, max_tokens=10, purpose="talk",
                        max_steps=0, call=call, ledger=False)
        self.assertEqual(out.reply.content, "It is a photo.")
        second = seen[1]
        self.assertEqual([m["role"] for m in second[-2:]], ["tool", "user"])
        self.assertEqual(second[-1]["content"][1]["type"], "image_url")
        self.assertNotIn("data:", json.dumps(box.log))                      # the tool log keeps text only


if __name__ == "__main__":
    unittest.main()


from supervisor.tests.test_invariants import InstanceCase  # noqa: E402 — the sandboxed end-to-end harness


class AnswerEndToEndTest(InstanceCase):
    """A Telegram photo through the instance's real answer path, in its sandbox, against a fake model."""

    def ask(self, vision_on: bool):
        d = self.root / "attachments" / "from-operator" / "20261016-091200"
        d.mkdir(parents=True, exist_ok=True)
        (d / "photo-9.png").write_bytes(png(32, 24))
        text = ("what is this?\n\n[The operator attached, saved in my folder:]\n"
                "- attachments/from-operator/20261016-091200/photo-9.png (1 KB)")
        request = {"talk_id": "api-v1", "client": "Telegram", "messages": [{"role": "user", "content": text}]}
        r = self.run_agent([{"content": "A red rectangle."}], "answer", stdin=json.dumps(request),
                           extra_env={"SHAFT_VISION": "1"} if vision_on else {})
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.fake.requests[0]["messages"][-1]["content"]

    def stored(self) -> str:
        return "\n".join(p.read_text(errors="replace") for p in self.root.rglob("*")
                         if p.is_file() and p.suffix in (".jsonl", ".md", ".json", ".log") and "attachments" not in p.parts)

    def test_no_image_bytes_in_episodes_logs_or_journal(self):
        sent = self.ask(True)
        self.assertEqual([p["type"] for p in sent], ["text", "image_url"])
        self.assertNotIn("shaft_path", json.dumps(sent))                     # SHAFT's own key never leaves
        self.assertTrue(sent[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertNotIn("base64,", self.stored())
        self.assertIn("photo-9.png", self.stored())                          # the path is what is kept

    def test_a_model_without_vision_is_told_it_has_not_seen_it(self):
        sent = self.ask(False)
        self.assertIsInstance(sent, str)
        self.assertIn("My model cannot see images, so I have not seen this image", sent)
