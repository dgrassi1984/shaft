"""Meeting processing through the queue, with synthetic audio and fake engines."""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from supervisor import autonomy, core, listen_pipeline, listen_worker, meetings
from supervisor.tests import test_app_api


class FakeEngines:
    processed_with = {"diarisation": "fake", "asr": "fake", "versions": {"fake": "1"}}

    def __init__(self, turns, results):
        self.turns = turns
        self.results = results
        self.calls = []

    def diarise(self, wav):
        return self.turns.get(wav.stem, [])

    def transcribe(self, wav, start, end):
        key = (wav.stem, start, end)
        self.calls.append(key)
        return self.results[key]


def spoken(text, start=0, lang="en"):
    return {"text": text, "lang": lang, "words": [{"w": text.split()[0], "start": start, "end": start + 0.3}]}


class ListenTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(mock.patch.object(core, "SUP_DIR", self.root))
        self.audio = self.root / "example.wav"
        self.audio.write_bytes(b"invalid audio")

    def enqueue(self, mode="inperson"):
        return meetings.import_audio(self.audio, title=None, mode=mode)["id"]

    def mock_decode(self):
        self.enterContext(mock.patch.object(listen_pipeline, "decode", return_value=[Path("ch1.wav")]))

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is unavailable")
    def test_online_worker_splits_audio_and_removes_only_echo(self):
        with wave.open(str(self.audio), "wb") as wav:
            wav.setparams((2, 2, 8000, 0, "NONE", ""))
            wav.writeframes(struct.pack("<hh", 1000, -2000) * 8000 * 11)
        meeting_id = self.enqueue("online")
        folder = self.root / "meetings" / meeting_id
        (folder / "sidecar.json").write_text('{"gaps": [{"start": 5, "end": 6, "reason": "interruption"}]}')
        self.enqueue("online")
        self.assertEqual(meetings.get(meeting_id)["state"], "queued")

        class Engines(FakeEngines):
            def diarise(inner, path):
                self.assertEqual(meetings.get(meeting_id)["state"], "processing")
                with wave.open(str(path), "rb") as wav:
                    self.assertEqual((wav.getnchannels(), wav.getframerate()), (1, 16_000))
                    wav.setpos(8000)
                    sample, = struct.unpack("<h", wav.readframes(1))
                    self.assertEqual(sample, 1000 if path.stem == "ch1" else -2000)
                return super().diarise(path)

        engines = Engines(
            {"ch1": [(4, 5, "B"), (0, 1, "A"), (1.5, 2, "A"), (7, 7.2, "C")], "ch2": [(1, 5, "A")]},
            {("ch1", 0, 2): spoken("WHAT IS THE NEXT STEP?"),
             ("ch1", 4, 5): spoken("Sono d'accordo.", 4, "it"),
             ("ch2", 1, 5): spoken("What is the next step?", 1)},
        )
        self.assertTrue(listen_worker.process_next(engines))
        self.assertEqual(engines.calls, [("ch1", 0, 2), ("ch1", 4, 5), ("ch2", 1, 5)])
        self.assertEqual([row["state"] for row in meetings.list_meetings()], ["queued", "ready"])
        row = meetings.get(meeting_id)
        self.assertEqual(row["processed_with"], FakeEngines.processed_with)
        transcript = json.loads((folder / "transcript.json").read_bytes())
        self.assertEqual(transcript, {
            "meeting": meeting_id,
            "segments": [
                {"start": 1, "end": 5, "channel": 2, "speaker": "S1", **spoken("What is the next step?", 1)},
                {"start": 4, "end": 5, "channel": 1, "speaker": "M2", **spoken("Sono d'accordo.", 4, "it")},
            ],
            "gaps": [{"start": 5, "end": 6, "reason": "interruption"}],
            "speakers": {"S1": {"channel": 2, "seconds": 4}, "M2": {"channel": 1, "seconds": 1}},
            "processed_with": FakeEngines.processed_with,
        })
        self.assertEqual((folder / "transcript.md").read_text(),
                         "00:00:01 S1: What is the next step?\n00:00:04 M2: Sono d'accordo.\n"
                         "[gap 00:00:05-00:00:06: interruption]\n")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is unavailable")
    def test_decode_error_records_ffmpeg_stderr(self):
        meeting_id = self.enqueue()
        with self.assertLogs("supervisor.listen_worker", level="ERROR"):
            self.assertTrue(listen_worker.process_next(FakeEngines({}, {})))
        row = meetings.get(meeting_id)
        self.assertEqual(row["state"], "failed")
        self.assertIn("Audio decode failed (ffmpeg exit", row["error"])
        self.assertIn("Invalid data found", row["error"])
        self.assertFalse((self.root / "meetings" / meeting_id / "transcript.json").exists())

    def test_words_floor_rejects_sparse_transcript_and_keeps_previous_output(self):
        self.mock_decode()
        meeting_id = self.enqueue()
        folder = self.root / "meetings" / meeting_id
        engine = FakeEngines({"ch1": [(0, 31, "A")]}, {("ch1", 0, 31): spoken("one two")})
        self.assertTrue(listen_worker.process_next(engine))
        old = (folder / "transcript.json").read_bytes(), (folder / "transcript.md").read_bytes()
        meetings.reprocess(meeting_id)
        engine.results["ch1", 0, 31] = spoken("one")
        with self.assertLogs("supervisor.listen_worker", level="ERROR"):
            self.assertTrue(listen_worker.process_next(engine))
        row = meetings.get(meeting_id)
        self.assertEqual(row["state"], "failed")
        self.assertIn("ASR word count was 1 for 31.0 seconds", row["error"])
        self.assertEqual(((folder / "transcript.json").read_bytes(), (folder / "transcript.md").read_bytes()), old)

    def test_no_speech_fails(self):
        self.mock_decode()
        meeting_id = self.enqueue()
        with self.assertLogs("supervisor.listen_worker", level="ERROR"):
            self.assertTrue(listen_worker.process_next(FakeEngines({}, {})))
        row = meetings.get(meeting_id)
        self.assertEqual((row["state"], row["error"]), ("failed", "Diarisation detected no speech."))

    def test_speech_without_words_fails_even_below_ten_seconds(self):
        self.mock_decode()
        meeting_id = self.enqueue()
        engines = FakeEngines({"ch1": [(0, 5, "A")]}, {("ch1", 0, 5): {"text": "", "lang": "en", "words": []}})
        with self.assertLogs("supervisor.listen_worker", level="ERROR"):
            self.assertTrue(listen_worker.process_next(engines))
        row = meetings.get(meeting_id)
        self.assertEqual((row["state"], row["error"]), ("failed", "ASR produced no words for the detected speech."))

    def test_worker_requeues_once_then_fails_after_second_crash(self):
        meeting_id = self.enqueue()
        for crashes, state, error in ((1, "queued", None), (2, "failed", "worker stopped while processing")):
            meetings.set_state(meeting_id, "processing")
            listen_worker.recover()
            listen_worker.recover()
            row = meetings.get(meeting_id)
            self.assertEqual((row["worker_crashes"], row["state"], row["error"]), (crashes, state, error))
        self.assertNotIn("worker_crashes", meetings.reprocess(meeting_id))

    def test_listen_cli_guards_processing_and_uses_listen_service(self):
        from supervisor.__main__ import main

        meeting_id = self.enqueue()
        meetings.set_state(meeting_id, "processing")
        with mock.patch.object(autonomy, "running", return_value=True), \
                mock.patch.object(autonomy, "service", return_value=0) as service:
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(main(["listen", "off"]), 1)
            self.assertIn(f"meetings are processing: {meeting_id}", error.getvalue())
            service.assert_not_called()
            self.assertEqual(main(["listen", "status"]), 0)
            service.assert_called_once_with("listen", "status")
        plist = autonomy._plist("listen")
        self.assertEqual(plist["Label"], "com.shaft.listen")
        self.assertEqual(plist["ProgramArguments"][-2:], ["-m", "supervisor.listen_worker"])


class ListenRouteTest(test_app_api.AppTest):
    def test_transcript_pages_and_reprocess_route_and_cli(self):
        from supervisor.__main__ import main

        source = self.sup / "example.wav"
        source.write_bytes(b"example")
        meeting_id = meetings.import_audio(source, title=None, mode="inperson")["id"]
        path = f"/app/v1/meetings/{meeting_id}"
        self.assertEqual(self.api(path + "/reprocess", "POST").status, 409)
        self.assertNotIn("transcript", self.get(path))
        meetings.set_state(meeting_id, "processing")
        self.assertEqual(self.api(path + "/reprocess", "POST").status, 409)
        segments = [{"start": t, "end": t + 1, "speaker": "M1", "channel": 1, **spoken("Example.", t)}
                    for t in range(201)]
        meetings.complete_processing(meeting_id, {
            "meeting": meeting_id, "segments": segments, "gaps": [], "speakers": {},
            "processed_with": FakeEngines.processed_with,
        }, "Example.\n")

        for query, page, expected in (("", 1, segments[:200]), ("?page=2", 2, segments[200:]), ("?page=3", 3, [])):
            transcript = self.get(path + query)["transcript"]
            self.assertEqual((transcript["page"], transcript["total_segments"], transcript["segments"]),
                             (page, 201, expected))
        for invalid in ("0", "", "two"):
            self.assertEqual(self.api(path + "?page=" + invalid).status, 400)

        response = self.api(path + "/reprocess", "POST")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body)["state"], "queued")
        self.assertNotIn("transcript", self.get(path))
        meetings.set_state(meeting_id, "processing")
        meetings.set_state(meeting_id, "failed", error="Example failure")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["meetings", "reprocess", meeting_id, "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["state"], "queued")
        self.assertIsNone(self.get(path)["error"])


class LanguageRetryTest(unittest.TestCase):
    """Bug 2026-11-25 (first real run): a short Italian turn ("Allora.") came back as Portuguese. A turn heard outside
    SHAFT_LISTEN_LANGUAGES is transcribed again in each of them and the likeliest reading is kept."""

    def test_a_turn_heard_outside_the_languages_is_asked_again_in_each(self):
        import sys
        import types
        from unittest import mock
        from supervisor import listen_worker
        calls = []

        def transcribe(samples, **kw):
            calls.append(kw.get("language"))
            seg = lambda p: [{"start": 0.0, "end": 1.0, "avg_logprob": p, "words": []}]
            if kw.get("language") is None:
                return {"text": "Alora.", "language": "pt", "segments": seg(-0.5)}
            return {"text": "Allora." if kw["language"] == "it" else "All right.", "language": kw["language"],
                    "segments": seg(-0.2 if kw["language"] == "it" else -0.9)}

        fake = types.SimpleNamespace(transcribe=transcribe)
        sound = types.SimpleNamespace(SoundFile=mock.MagicMock())
        sound.SoundFile.return_value.__enter__.return_value.__len__.return_value = 32_000
        sound.SoundFile.return_value.__enter__.return_value.read.return_value = [0.0] * 16_000
        with mock.patch.dict(sys.modules, {"mlx_whisper": fake, "soundfile": sound}), \
                mock.patch.object(listen_worker, "LANGUAGES", ("it", "en")):
            out = listen_worker.Engines().transcribe(Path("x.wav"), 0.0, 1.0)
        self.assertEqual((out["text"], out["lang"]), ("Allora.", "it"))
        self.assertEqual(calls, [None, "it", "en"])
        self.assertNotEqual(out["lang"], "pt")
