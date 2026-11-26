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
    """Diarisation turns per channel file, and a reading per turn: `results[(channel, start, end)]`. Transcribing a
    chunk returns, in time order, the readings of every turn that lies inside it, whatever the channel (the chunk is
    cut from the mixed conversation)."""
    processed_with = {"diarisation": "fake", "asr": "fake", "versions": {"fake": "1"}}

    def __init__(self, turns, results, embeddings=None, loudness=None):
        self.turns = turns
        self.results = results
        self.embeddings = embeddings or {}
        self.levels = loudness or (lambda wav, start, end: 1.0)
        self.calls = []
        self.prompts = []
        self.mixed = []

    def diarise(self, wav):
        return self.turns.get(wav.stem, []), self.embeddings.get(wav.stem, {})

    def loudness(self, wav, spans):
        return [self.levels(wav, start, end) for start, end in spans]

    def mix(self, wavs, weights, out):
        self.mixed.append(([wav.stem for wav in wavs], weights, out.name))

    def transcribe(self, wav, start, end, prompt=None):
        self.calls.append((wav.stem, start, end))
        self.prompts.append(prompt)
        inside = sorted(((key, result) for key, result in self.results.items()
                         if start <= key[1] and key[2] <= end), key=lambda item: item[0][1])
        texts = [result["text"] for _, result in inside]
        return {"text": " ".join(texts), "lang": inside[0][1]["lang"] if inside else "en",
                "words": [word for _, result in inside for word in result["words"]]}


def spoken(text, start=0, lang="en"):
    """A reading whose words follow each other every quarter second from `start`."""
    return {"text": text, "lang": lang,
            "words": [{"w": word, "raw": " " + word, "start": start + 0.25 * i, "end": start + 0.25 * (i + 1)}
                      for i, word in enumerate(text.split())]}


def segment(speaker, channel, text, start=0, lang="en"):
    """The transcript segment `spoken(text, start)` becomes when every word goes to one speaker."""
    words = [{"w": w["w"], "start": w["start"], "end": w["end"]} for w in spoken(text, start)["words"]]
    return {"start": start, "end": words[-1]["end"], "channel": channel, "speaker": speaker, "lang": lang,
            "words": words, "text": text}


class ListenTest(unittest.TestCase):
    maxDiff = None

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
    def test_online_worker_mixes_channels_and_gives_each_word_to_its_speaker(self):
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

        def loudness(wav, start, end):
            # The microphone is loud from 4 s, where the system channel only carries the far side's fading voice.
            if start >= 3.9:
                return 3.0 if wav.stem == "ch1" else 0.5
            return 1.0

        engines = Engines(
            {"ch1": [(4, 5, "B"), (0, 0.8, "A"), (7, 7.2, "C")], "ch2": [(1, 5, "A")]},
            {("ch1", 0, 0.8): spoken("Ciao."),
             ("ch2", 1, 5): spoken("What is the next step?", 1),
             ("ch1", 4, 5): spoken("Sono d'accordo.", 4, "it")},
            loudness=loudness,
        )
        self.assertTrue(listen_worker.process_next(engines))
        # One chunk for the whole conversation, cut from the mix of both channels levelled by their own speech.
        self.assertEqual(engines.calls, [("mix", 0.0, 7.5)])
        self.assertEqual(engines.mixed, [(["ch1", "ch2"], [1 / 3, 1.0], "mix.wav")])
        self.assertEqual(engines.prompts, [None])
        self.assertEqual([row["state"] for row in meetings.list_meetings()], ["queued", "ready"])
        row = meetings.get(meeting_id)
        self.assertEqual(row["processed_with"], FakeEngines.processed_with)
        transcript = json.loads((folder / "transcript.json").read_bytes())
        self.assertEqual(transcript, {
            "meeting": meeting_id,
            "segments": [segment("M1", 1, "Ciao."), segment("S1", 2, "What is the next step?", 1),
                         segment("M2", 1, "Sono d'accordo.", 4)],
            "gaps": [{"start": 5, "end": 6, "reason": "interruption"}],
            "speakers": {"M1": {"channel": 1, "seconds": 0.8, "clips": []},
                         "S1": {"channel": 2, "seconds": 4, "clips": []},
                         "M2": {"channel": 1, "seconds": 1, "clips": []}},
            "processed_with": FakeEngines.processed_with,
        })
        self.assertEqual((folder / "transcript.md").read_text(),
                         "00:00:00 Speaker M1 (unknown): Ciao.\n"
                         "00:00:01 Speaker S1 (unknown): What is the next step?\n"
                         "00:00:04 Speaker M2 (unknown): Sono d'accordo.\n"
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
        engine = FakeEngines({"ch1": [(0, 31, "A")]}, {("ch1", 0, 15): spoken("one two")})
        self.assertTrue(listen_worker.process_next(engine))
        old = (folder / "transcript.json").read_bytes(), (folder / "transcript.md").read_bytes()
        meetings.reprocess(meeting_id)
        engine.results["ch1", 0, 15] = spoken("one")
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
            self.assertEqual((kw["initial_prompt"], kw["condition_on_previous_text"]), ("Allora, Foresight.", False))
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
            out = listen_worker.Engines().transcribe(Path("x.wav"), 0.0, 1.0, "Allora, Foresight.")
        self.assertEqual((out["text"], out["lang"]), ("Allora.", "it"))
        self.assertEqual(calls, [None, "it", "en"])
        self.assertNotEqual(out["lang"], "pt")


class ConversationTest(unittest.TestCase):
    """Bugs 2026-11-26 (a real meeting compared with another transcriber): every diarised turn was decoded alone, so
    sub-second turns came back as invented "Thank you", "Bye-bye", "See you soon"; turns lost the conversation
    around them, so "Foresight" was heard as "forse" and "lead" as "l'id"; and words were joined with spaces, so
    Whisper's "c'" + "è" read as "c' è"."""

    def test_a_short_turn_is_decoded_inside_the_conversation_not_alone(self):
        turns = [(0, 10, "S1", 2), (4, 4.6, "M1", 1), (11, 11.5, "M1", 1)]
        self.assertEqual(listen_pipeline.chunk_spans(turns), [(0.0, 11.8)])

    def test_chunks_fit_one_whisper_window_and_end_in_pauses(self):
        turns = [(0, 20, "S1", 2), (21, 26, "M1", 1), (30, 40, "S1", 2), (45, 115, "S1", 2)]
        chunks = listen_pipeline.chunk_spans(turns)
        self.assertEqual(chunks[:2], [(0.0, 26.3), (29.7, 40.3)])
        # 70 s with no pause: three equal pieces, never a sliver of a few frames at the end (review 2026-11-26: a
        # 28.000000000000004 s region left a zero-frame chunk that failed the meeting).
        self.assertEqual([round(end - start, 6) for start, end in chunks[2:]], [23.533333] * 3)
        self.assertEqual((chunks[2][0], chunks[-1][1]), (44.7, 115.3))
        self.assertEqual(len(listen_pipeline.chunk_spans([(0.36, 27.76, "M1", 1)])), 1)
        self.assertTrue(all(end - start <= listen_pipeline.CHUNK_SECONDS + 1e-9 for start, end in chunks))

    def test_a_stray_word_inside_a_phrase_keeps_its_speaker_and_a_pause_lets_it_change(self):
        words = [{"start": 0.0, "end": 0.3}, {"start": 0.3, "end": 0.6}, {"start": 0.6, "end": 0.9},
                 {"start": 1.5, "end": 1.8}]
        favours = {0: "A", 1: "B", 2: "A", 3: "B"}

        def evidence(index, speaker):
            return 1.0 if favours[index] == speaker else 0.0

        self.assertEqual(listen_pipeline.attribute(words, ["A", "B"], evidence), ["A", "A", "A", "B"])

    def test_each_chunk_is_prompted_with_the_hints_and_what_was_just_said(self):
        self.enterContext(mock.patch.object(listen_pipeline, "decode", return_value=[Path("ch1.wav")]))
        first = {"text": " Allora, c'è il Foresight.", "lang": "it", "words": [
            {"w": "Allora,", "raw": " Allora,", "start": 1.0, "end": 1.4},
            {"w": "c'", "raw": " c'", "start": 1.4, "end": 1.6},
            {"w": "è", "raw": "è", "start": 1.6, "end": 1.7},
            {"w": "il", "raw": " il", "start": 1.7, "end": 1.8},
            {"w": "Foresight.", "raw": " Foresight.", "start": 1.8, "end": 2.4}]}
        engines = FakeEngines({"ch1": [(1, 20, "A"), (40, 50, "A")]},
                              {("ch1", 1, 20): first, ("ch1", 40, 50): spoken("Ok.", 40)})
        transcript = listen_pipeline.transcribe_meeting(
            {"id": "m-1", "mode": "inperson"}, Path("audio.m4a"), Path("work"), {}, engines, "Patricia Navi, lead.")
        self.assertEqual(engines.calls, [("ch1", 0.7, 20.3), ("ch1", 39.7, 50.3)])
        self.assertEqual(engines.prompts, ["Patricia Navi, lead.", "Patricia Navi, lead. Allora, c'è il Foresight."])
        self.assertEqual([row["text"] for row in transcript["segments"]], ["Allora, c'è il Foresight.", "Ok."])
        self.assertNotIn("Thank you", " ".join(row["text"] for row in transcript["segments"]))


class AttributionTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(listen_pipeline, "decode",
                                            return_value=[Path("ch1.wav"), Path("ch2.wav")]))

    def run_online(self, loudness):
        # Both channels' diarisation claims 2-3 s; only loudness says who spoke "Va bene".
        engines = FakeEngines({"ch1": [(0, 1, "A"), (2, 3, "A")], "ch2": [(2, 3, "A"), (5, 9, "A")]},
                              {("ch1", 0, 1): spoken("Ciao."), ("ch1", 2, 3): spoken("Va bene", 2),
                               ("ch2", 5, 9): spoken("Allora partiamo", 5)}, loudness=loudness)
        transcript = listen_pipeline.transcribe_meeting(
            {"id": "m-1", "mode": "online"}, Path("audio.m4a"), Path("work"), {}, engines)
        return [(row["speaker"], row["text"]) for row in transcript["segments"]]

    def test_the_louder_channel_against_its_own_speech_takes_the_word(self):
        mic = lambda wav, start, end: (4.0 if 1.9 <= start < 3 else 1.0) if wav.stem == "ch1" else 1.0
        far = lambda wav, start, end: (4.0 if 1.9 <= start < 3 else 1.0) if wav.stem == "ch2" else 1.0
        self.assertEqual(self.run_online(mic), [("M1", "Ciao."), ("M1", "Va bene"), ("S1", "Allora partiamo")])
        self.assertEqual(self.run_online(far), [("M1", "Ciao."), ("S1", "Va bene"), ("S1", "Allora partiamo")])

    def test_labels_are_numbered_by_their_first_real_turn(self):
        # Review 2026-11-26: a 0.2 s blip labelled B first must not take M1 from A, or a saved anonymisation of M1
        # would land on someone else after reprocessing.
        self.enterContext(mock.patch.object(listen_pipeline, "decode", return_value=[Path("ch1.wav")]))
        engines = FakeEngines({"ch1": [(0, 0.2, "B"), (1, 8, "A")]}, {("ch1", 1, 8): spoken("Buongiorno a tutti", 1)})
        transcript = listen_pipeline.transcribe_meeting(
            {"id": "m-1", "mode": "inperson"}, Path("audio.m4a"), Path("work"), {}, engines)
        self.assertEqual([row["speaker"] for row in transcript["segments"]], ["M1"])

    def test_chunks_join_cleanly_and_words_past_a_chunk_are_dropped(self):
        self.enterContext(mock.patch.object(listen_pipeline, "decode", return_value=[Path("ch1.wav")]))
        late = {"text": "Yes", "lang": "en", "words": [{"w": "Yes", "raw": "Yes", "start": 0.5, "end": 0.8},
                                                      {"w": "ghost", "raw": " ghost", "start": 40.0, "end": 40.2}]}
        early = {"text": "Ciao", "lang": "it", "words": [{"w": "Ciao", "raw": "Ciao", "start": 40.0, "end": 40.3}]}
        engines = FakeEngines({"ch1": [(0, 1, "A"), (40, 41, "A")]}, {("ch1", 0, 1): late, ("ch1", 40, 41): early})
        transcript = listen_pipeline.transcribe_meeting(
            {"id": "m-1", "mode": "inperson"}, Path("audio.m4a"), Path("work"), {}, engines)
        self.assertEqual([(row["text"], row["lang"]) for row in transcript["segments"]], [("Yes", "en"), ("Ciao", "it")])


class HintsTest(unittest.TestCase):
    def test_gallery_names_and_vocabulary_without_forgotten_people_or_repeats(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        gallery = Path(temporary.name) / "speakers"
        gallery.mkdir()
        (gallery / "people.json").write_text(json.dumps([
            {"id": "p-00000001", "name": "Patricia Navi"}, {"id": "p-00000002", "name": "Forgotten Person"},
            {"id": "p-00000003", "name": "Daniele Grassi"}]))
        (gallery / "tombstones.json").write_text(json.dumps({"p-00000002": {"sources": {}, "anonymise": False}}))
        with mock.patch.object(core, "SUP_DIR", Path(temporary.name)), \
                mock.patch.object(listen_worker, "VOCABULARY", ("Foresight", "Patricia Navi", "lead")):
            self.assertEqual(listen_worker.hints(), "Patricia Navi, Daniele Grassi, Foresight, lead.")
        with mock.patch.object(core, "SUP_DIR", Path(temporary.name) / "empty"), \
                mock.patch.object(listen_worker, "VOCABULARY", ()):
            self.assertEqual(listen_worker.hints(), "")
