"""Meeting processing through the queue, with synthetic audio and fake engines."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from supervisor import autonomy, core, listen_pipeline, listen_setup, listen_worker, meetings, speakers
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

    def diarise(self, wav, max_speakers=None):
        self.caps = getattr(self, "caps", []) + [max_speakers]
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


def caf_chunk(kind, payload):
    return struct.pack(">4sq", kind, len(payload)) + payload


def mac_ima4_caf():
    packets = 100
    return b"".join((
        struct.pack(">4sHH", b"caff", 1, 0),
        caf_chunk(b"desc", struct.pack(">d4sIIIII", 24_000, b"ima4", 0, 68, 64, 2, 0)),
        caf_chunk(b"chan", struct.pack(">III", 0x650002, 0, 0)),
        caf_chunk(b"pakt", struct.pack(">qqii", 0, packets * 64 - 32, 0, 32)),
        caf_chunk(b"free", bytes(0xF74)),
        caf_chunk(b"data", struct.pack(">I", 1) + bytes(packets * 68)),
    ))


class ListenTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(mock.patch.object(core, "SUP_DIR", self.root))
        self.enterContext(mock.patch.object(listen_setup, "missing", return_value=None))   # Listen is set up
        self.audio = self.root / "example.wav"
        self.audio.write_bytes(b"invalid audio")

    def enqueue(self, mode="inperson"):
        return meetings.import_audio(self.audio, title=None, mode=mode)["id"]

    def mock_decode(self):
        self.enterContext(mock.patch.object(listen_pipeline, "decode", return_value=[Path("ch1.wav")]))

    def test_normalize_caf_only_renames_the_empty_cbr_packet_table_20261127(self):
        audio = self.root / "audio.caf"
        original = mac_ima4_caf()
        audio.write_bytes(original)
        self.assertEqual(original[0x4C:0x50], b"pakt")
        self.assertTrue(listen_pipeline.normalize_caf(audio))
        self.assertEqual(audio.stat().st_size, len(original))
        expected = original[:0x4C] + b"free" + original[0x50:]
        self.assertEqual(audio.read_bytes(), expected)
        self.assertFalse(listen_pipeline.normalize_caf(audio))
        self.assertEqual(audio.read_bytes(), expected)

    def test_normalize_caf_leaves_other_audio_and_malformed_headers_untouched(self):
        original = mac_ima4_caf()
        pcm = (original[:8] + caf_chunk(b"desc", struct.pack(">d4sIIIII", 24_000, b"lpcm", 2, 4, 1, 2, 16))
               + caf_chunk(b"data", bytes(8)))
        wav = io.BytesIO()
        with wave.open(wav, "wb") as output:
            output.setparams((1, 2, 16_000, 0, "NONE", ""))
            output.writeframes(bytes(2))
        audio = self.root / "audio"
        for case, contents in {
            "PCM without pakt": pcm,
            "VBR bytes per packet": original[:0x24] + bytes(4) + original[0x28:],
            "VBR frames per packet": original[:0x28] + bytes(4) + original[0x2C:],
            "nonempty packet table": original[:0x58] + struct.pack(">q", 100) + original[0x60:],
            "WAV": wav.getvalue(),
            "truncated desc": original[:0x20],
            "truncated chunk header after pakt": original[:0x74],
            "negative non-data chunk size": original[:0x74] + struct.pack(">q", -1) + original[0x7C:],
            "truncated data": original[:-1],
        }.items():
            with self.subTest(case=case):
                audio.write_bytes(contents)
                self.assertFalse(listen_pipeline.normalize_caf(audio))
                self.assertEqual(audio.read_bytes(), contents)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is unavailable")
    def test_a_mac_ima4_caf_with_an_empty_packet_table_decodes_20261127(self):
        audio = self.root / "audio.caf"
        audio.write_bytes(mac_ima4_caf())
        paths = listen_pipeline.decode(audio, self.root, "online")
        self.assertEqual(paths, [self.root / "ch1.wav", self.root / "ch2.wav"])
        for path in paths:
            with wave.open(str(path), "rb") as output:
                self.assertGreater(output.getnframes(), 0)

    def test_decode_and_speaker_clip_normalize_caf_before_ffmpeg(self):
        audio = self.root / "audio.caf"
        metadata = {"state": "ready", "ext": "caf", "mode": "online"}
        transcript = {"speakers": {"M1": {"channel": 1, "clips": [[0, 0.2]]}}}
        (self.root / "transcript.json").write_text(json.dumps(transcript))

        def ffmpeg(command, **kwargs):
            self.assertEqual(command[command.index("-i") + 1], str(audio))
            self.assertEqual(audio.read_bytes()[0x4C:0x50], b"free")
            return mock.Mock(returncode=0, stdout=bytes(2))

        with mock.patch.object(listen_pipeline.subprocess, "run", side_effect=ffmpeg) as run, \
                mock.patch.object(listen_pipeline, "ffmpeg", return_value="ffmpeg"):
            audio.write_bytes(mac_ima4_caf())
            listen_pipeline.decode(audio, self.root, "online")
            audio.write_bytes(mac_ima4_caf())
            with mock.patch.object(meetings, "_locked", return_value=contextlib.nullcontext((self.root, metadata))):
                speakers.clip("example", "M1", 0)
            self.assertEqual(run.call_count, 2)

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
            def diarise(inner, path, max_speakers=None):
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
            "warnings": [],
            "attendees": [],
            "speakers": {"M1": {"channel": 1, "seconds": 0.8, "clips": []},
                         "S1": {"channel": 2, "seconds": 4, "clips": [[1, 5]]},
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

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is unavailable")
    def test_decode_error_leads_with_the_recorder_problems(self):
        # Bug 2026-11-27: the Mac's AAC encoder refused every write and left a 28-byte file; the card said only
        # "moov atom not found", not what the recorder had already reported in the sidecar.
        meeting_id = self.enqueue("online")
        folder = self.root / "meetings" / meeting_id
        (folder / "sidecar.json").write_text(json.dumps({"problems": [
            "62.1 s of audio could not be written (OSStatus 560226676)",
            "the file could not be finalised (OSStatus 560226676)"]}))
        with self.assertLogs("supervisor.listen_worker", level="ERROR"):
            self.assertTrue(listen_worker.process_next(FakeEngines({}, {})))
        error = meetings.get(meeting_id)["error"]
        self.assertTrue(error.startswith("The recording device reported: 62.1 s of audio could not be written"), error)
        self.assertIn("the file could not be finalised (OSStatus 560226676). Audio decode failed (ffmpeg exit", error)

    def test_recorder_problems_tolerates_missing_or_malformed_sidecar(self):
        folder = self.root / "nowhere"
        folder.mkdir()
        self.assertEqual(listen_worker.recorder_problems(folder), [])
        (folder / "sidecar.json").write_text("[1, 2]")
        self.assertEqual(listen_worker.recorder_problems(folder), [])
        (folder / "sidecar.json").write_text("[" * 12_000 + "]" * 12_000)
        self.assertEqual(listen_worker.recorder_problems(folder), [])
        (folder / "sidecar.json").write_text('{"problems": ["x", " "]}')
        self.assertEqual(listen_worker.recorder_problems(folder), ["x"])

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
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(["listen", "status"]), 0)
            service.assert_called_once_with("listen", "status")
            self.assertIn("ffmpeg ", out.getvalue())
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
        self.assertEqual(self.get(path + "?page=2")["transcript"]["segments"], segments[200:])
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

    def test_text_the_decoder_heard_outside_a_chunk_never_prompts_the_next(self):
        # Review 2026-11-27: the next prompt took the decoder's whole text, words outside the chunk included, so a
        # misheard name there steered the next chunk; and a chunk long after the last kept its context.
        self.enterContext(mock.patch.object(listen_pipeline, "decode", return_value=[Path("ch1.wav")]))
        stray = {"text": " Forse. Va bene.", "lang": "it", "words": [
            {"w": "Forse.", "raw": " Forse.", "start": 0.0, "end": 0.4},       # before the chunk: not kept
            {"w": "Va", "raw": " Va", "start": 2.0, "end": 2.2}, {"w": "bene.", "raw": " bene.", "start": 2.2, "end": 2.6}]}
        later = {"text": " Ok.", "lang": "it", "words": [{"w": "Ok.", "raw": " Ok.", "start": 80.0, "end": 80.3}]}
        last = {"text": " Sì.", "lang": "it", "words": [{"w": "Sì.", "raw": " Sì.", "start": 110.0, "end": 110.3}]}

        class Engines(FakeEngines):
            def transcribe(inner, wav, start, end, prompt=None):
                inner.prompts.append(prompt)
                return [stray, later, last][len(inner.prompts) - 1]

        engines = Engines({"ch1": [(1.5, 3, "A"), (80, 80.5, "A"), (110, 110.5, "A")]}, {})
        listen_pipeline.transcribe_meeting({"id": "m", "mode": "inperson"}, Path("a.caf"), Path("."), {}, engines)
        self.assertEqual(engines.prompts[0], None)
        self.assertEqual(engines.prompts[1], None)            # 77 s later: what was said then is not its context
        self.assertEqual(engines.prompts[2], "Ok.")

    def test_far_end_echo_in_the_microphone_is_not_the_operator(self):
        # Review 2026-11-27: on speakers instead of headphones the far end leaks into the mic, where diarisation can
        # label it a mic speaker. The loudness evidence already gives a word heard far quieter on the mic to the system
        # speaker; this pins it (a separate echo rule changed no outcome the loudness did not, so there is none).
        words = [{"w": w, "raw": " " + w, "start": 1 + 0.3 * i, "end": 1.3 + 0.3 * i} for i, w in enumerate("Va bene così".split())]
        reading = {"text": " Va bene così", "lang": "it", "words": words}
        self.enterContext(mock.patch.object(listen_pipeline, "decode", return_value=[Path("ch1.wav"), Path("ch2.wav")]))

        def level(wav, start, end):
            # The words (short spans) are heard 26 dB down on the mic; its own speech, and the far end, at full level.
            return 0.05 if wav.stem == "ch1" and 0.9 < start < 2 and end - start < 1 else 1.0

        engines = FakeEngines({"ch1": [(0.9, 2.0, "A"), (5, 8, "A")], "ch2": [(0.9, 2.0, "B")]},
                              {("ch2", 0.9, 2.0): reading}, loudness=level)
        transcript = listen_pipeline.transcribe_meeting({"id": "m", "mode": "online"}, Path("a.caf"), Path("."), {}, engines)
        self.assertEqual({row["speaker"] for row in transcript["segments"]}, {"S1"})

    def test_the_operators_count_caps_each_channels_speakers_and_the_calendar_alone_never_does(self):
        # Review 2026-11-27: one remote voice split into S1 and S2. A count the operator gives caps the channel; the
        # invitation alone never does (Codex review: two people behind one invitation would be merged, for good).
        self.assertEqual(listen_worker.speaker_caps({"mode": "online", "others": 1}), {2: 1})
        self.assertEqual(listen_worker.speaker_caps({"mode": "inperson", "others": 2}), {1: 3})
        self.assertEqual(listen_worker.speaker_caps({"mode": "online"}), {})
        self.assertEqual(listen_worker.speaker_caps({"mode": "online", "others": None}), {})

    def test_the_meetings_own_vocabulary_survives_a_long_prompt(self):
        # Codex review 2026-11-27: Whisper keeps the end of a long prompt, and a large gallery pushed the meeting's
        # own terms out. They come last, and the general ones go first.
        gallery = [f"Person {i:03d}" for i in range(80)]
        with mock.patch.object(listen_worker, "VOCABULARY", ("UniCredit",)), \
                mock.patch.object(listen_worker.speakers, "known_names", return_value=gallery):
            prompt = listen_worker.hints(["Ada"], "Foresight, BPER")
        self.assertTrue(prompt.endswith("Ada, Foresight, BPER."))
        self.assertLessEqual(len(prompt), listen_worker.HINT_CHARS + 1)
        self.assertNotIn("UniCredit", prompt)

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
            # The global vocabulary first, the gallery after it (Whisper keeps a long prompt's end); a repeat keeps
            # its last place.
            self.assertEqual(listen_worker.hints(), "Foresight, lead, Patricia Navi, Daniele Grassi.")
        with mock.patch.object(core, "SUP_DIR", Path(temporary.name) / "empty"), \
                mock.patch.object(listen_worker, "VOCABULARY", ()):
            self.assertEqual(listen_worker.hints(), "")


class CalendarTitleTest(unittest.TestCase):
    """2026-11-26: a recording was titled "Meeting 26 Nov 2026 at 9:31" while the calendar said "Update MKT". A meeting
    sent without a title takes the title of the calendar event it overlaps most; the operator's title always wins."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(mock.patch.object(core, "SUP_DIR", self.root))
        self.enterContext(mock.patch.object(listen_setup, "missing", return_value=None))   # Listen is set up
        self.audio = self.root / "call.wav"
        self.audio.write_bytes(b"x")

    def row(self, **fields):
        return {"id": "m-1", "account": None, "started": "2026-11-26T09:31:00+01:00",
                "ended": "2026-11-26T09:43:00+01:00", **fields}

    @staticmethod
    def event(summary, start, end, **extra):
        return {"summary": summary, "start": {"dateTime": f"2026-11-26T{start}:00+01:00"},
                "end": {"dateTime": f"2026-11-26T{end}:00+01:00"}, **extra}

    def lookup(self, row, items, accounts=("home", "work")):
        account = mock.MagicMock()
        account.return_value.require.side_effect = lambda scope: account.return_value
        with mock.patch.object(listen_worker.google, "Account", account), \
                mock.patch.object(listen_worker.google, "accounts", return_value={a: {} for a in accounts}), \
                mock.patch.object(listen_worker.core, "secrets_env", return_value={}), \
                mock.patch.object(listen_worker.gcal, "events", side_effect=items) as events:
            return listen_worker.calendar_event(row), events

    def test_the_most_overlapping_timed_event_not_declined_wins_across_accounts(self):
        declined = self.event("Declined", "09:30", "09:45", attendees=[{"self": True, "responseStatus": "declined"}])
        all_day = {"summary": "Holiday", "start": {"date": "2026-11-26"}, "end": {"date": "2026-11-27"}}
        (found, alias), events = self.lookup(self.row(), [
            ([declined, all_day, self.event("Standup", "09:00", "09:35")], False),
            ([self.event("Update MKT", "09:30", "10:00")], False)])
        self.assertEqual((found["summary"], alias), ("Update MKT", "work"))
        self.assertEqual(events.call_count, 2)

    def test_as_long_an_overlap_on_two_accounts_names_the_meeting_but_files_it_nowhere(self):
        # Codex review 2026-11-27: a personal and a work event in the same hour; the first alias sorted won, and the
        # meeting was filed (and shown to that account's instances) by alphabetical order.
        (found, alias), _ = self.lookup(self.row(), [([self.event("Dentist", "09:30", "10:00")], False),
                                                      ([self.event("Update MKT", "09:30", "10:00")], False)])
        self.assertIsNotNone(found)
        self.assertIsNone(alias)

    def test_named_time_zones_count_and_a_broken_event_or_reply_never_fails_the_meeting(self):
        # Review 2026-11-26: an event with a zone name and no offset, or an unparsable one, raised and failed
        # transcription; so did any error from Google other than the two it names.
        zoned = {"summary": "Zoned", "start": {"dateTime": "2026-11-26T09:30:00", "timeZone": "Europe/Rome"},
                 "end": {"dateTime": "2026-11-26T10:00:00", "timeZone": "Europe/Rome"}}
        broken = {"summary": "Broken", "start": {"dateTime": "oops"}, "end": {"dateTime": "2026-11-26T10:00:00+01:00"}}
        with self.assertLogs("supervisor.listen_worker", level="WARNING"):
            (found, _), _ = self.lookup(self.row(), [ValueError("bad JSON"), ([broken, zoned], False)])
        self.assertEqual(found["summary"], "Zoned")

    def test_the_longest_overlap_wins_even_untitled(self):
        (found, _), _ = self.lookup(self.row(), [([{"start": {"dateTime": "2026-11-26T09:00:00+01:00"},
                                                    "end": {"dateTime": "2026-11-26T10:00:00+01:00"}},
                                                   self.event("Short", "09:41", "09:50")], False)], accounts=("home",))
        self.assertNotIn("summary", found)

    def test_a_filed_meeting_reads_only_its_account_and_nothing_overlapping_is_none(self):
        (found, alias), events = self.lookup(self.row(account="work"), [([self.event("Later", "11:00", "12:00")], False)])
        self.assertEqual((found, alias), (None, None))
        self.assertEqual(events.call_count, 1)

    def test_an_unreadable_calendar_is_logged_and_the_others_still_count(self):
        with self.assertLogs("supervisor.listen_worker", level="WARNING") as logs:
            (found, _), _ = self.lookup(self.row(), [listen_worker.google.ApiError(403, "quota"),
                                                     ([self.event("Update MKT", "09:30", "10:00")], False)])
        self.assertEqual(found["summary"], "Update MKT")
        self.assertIn("calendar of home not read", logs.output[0])

    def test_untitled_meetings_get_a_dated_title_and_the_calendar_never_overrides_the_operator(self):
        auto = meetings.import_audio(self.audio, title="", mode="online")
        self.assertTrue(auto["title_auto"])
        self.assertRegex(auto["title"], r"^Meeting \d+ \w{3} \d{4} at \d\d:\d\d$")
        meetings.name_from_calendar(auto["id"], "Update MKT", None)
        self.assertEqual((meetings.get(auto["id"])["title"], meetings.get(auto["id"])["title_from"]),
                         ("Update MKT", "calendar"))
        meetings.update(auto["id"], {"title": "My notes"})
        self.assertNotIn("title_from", meetings.get(auto["id"]))
        named = meetings.import_audio(self.audio, title="", mode="online")
        meetings.update(named["id"], {"title": "My title"})
        meetings.name_from_calendar(named["id"], "Update MKT", None)
        self.assertEqual(meetings.get(named["id"])["title"], "My title")
        # Review 2026-11-26: read under one account, filed under another meanwhile: the title must not cross over.
        moved = meetings.import_audio(self.audio, title="", mode="online")
        with mock.patch.object(meetings, "_account_alias"):
            meetings.update(moved["id"], {"account": "work"})
        meetings.name_from_calendar(moved["id"], "Home event", "home")
        self.assertTrue(meetings.get(moved["id"])["title"].startswith("Meeting "))
        self.assertFalse(meetings.import_audio(self.audio, title="Board call", mode="online")["title_auto"])

    def test_an_unfiled_meeting_is_filed_under_its_calendar_events_account(self):
        # Review 2026-11-27: with two Google accounts the apps file nothing, so every meeting stayed unfiled and no
        # instance could read it. Listen files it under the account whose calendar holds its event, never over the
        # operator's own choice (unfiled included).
        self.enterContext(mock.patch.object(listen_pipeline, "decode", return_value=[Path("ch1.wav")]))
        self.enterContext(mock.patch.object(meetings, "_account_alias"))
        event = self.event("Update MKT", "09:30", "10:00")
        unfiled = meetings.import_audio(self.audio, title="", mode="inperson")["id"]
        chosen = meetings.import_audio(self.audio, title="", mode="inperson")["id"]
        meetings.update(chosen, {"account": None})
        # One from before this change: unfiled, perhaps on purpose, so never filed by Listen.
        legacy = meetings.import_audio(self.audio, title="", mode="inperson")["id"]
        folder = meetings._root() / legacy
        old = json.loads((folder / "meeting.json").read_text())
        old.pop("account_auto")
        (folder / "meeting.json").write_text(json.dumps(old))
        for meeting_id in (unfiled, chosen, legacy):
            engines = FakeEngines({"ch1": [(0, 5, "A")]}, {("ch1", 0, 5): spoken("Buongiorno a tutti")})
            with mock.patch.object(listen_worker, "calendar_event", return_value=(event, "home")):
                self.assertTrue(listen_worker.process_next(engines))
        self.assertEqual((meetings.get(unfiled)["account"], meetings.get(unfiled)["title"]), ("home", "Update MKT"))
        self.assertIsNone(meetings.get(chosen)["account"])
        self.assertIsNone(meetings.get(legacy)["account"])

    def test_processing_names_the_meeting_and_prompts_with_its_invitees(self):
        meeting_id = meetings.import_audio(self.audio, title="", mode="inperson")["id"]
        event = self.event("Update MKT", "09:30", "10:00",
                           attendees=[{"displayName": "Patricia Navi"}, {"email": "x@example.com"},
                                      {"displayName": "Room 4", "resource": True}])
        self.enterContext(mock.patch.object(listen_pipeline, "decode", return_value=[Path("ch1.wav")]))
        engines = FakeEngines({"ch1": [(0, 5, "A")]}, {("ch1", 0, 5): spoken("Buongiorno a tutti")})
        event["attendees"].append({"displayName": "<|startoftranscript|> Mallory"})
        with mock.patch.object(listen_worker, "calendar_event", return_value=(event, "home")), \
                mock.patch.object(listen_worker, "VOCABULARY", ("Foresight",)):
            self.assertTrue(listen_worker.process_next(engines))
        self.assertEqual(meetings.get(meeting_id)["title"], "Update MKT")
        # Whisper's special-token marks never reach its prompt (tiktoken refuses them and the meeting would fail).
        self.assertEqual(engines.prompts, ["Foresight, Patricia Navi, startoftranscript Mallory."])


class ListenSetupTest(unittest.TestCase):
    """Issue #58: on a Mac without admin rights (no Homebrew) Listen had no environment, no uv on PATH and no ffmpeg;
    `shaft listen on` installed a plist pointing at a missing .venv-listen and every meeting stayed queued."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.venv = self.root / ".venv-listen"
        self.enterContext(mock.patch.object(listen_setup, "VENV", self.venv))
        self.enterContext(mock.patch.object(listen_pipeline, "VENV_FFMPEG", self.venv / "bin" / "ffmpeg"))
        self.enterContext(mock.patch.object(autonomy, "AGENTS", self.root / "LaunchAgents"))

    def executable(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        return path

    def test_ffmpeg_is_listens_own_first_then_path_and_homebrew_else_says_how_to_get_one(self):
        on_path = self.executable(self.root / "path" / "ffmpeg")
        with mock.patch.dict(os.environ, {"PATH": str(on_path.parent)}):
            self.assertEqual(listen_pipeline.ffmpeg(), str(on_path))
            target = self.executable(self.root / "wheel" / "ffmpeg-macos-aarch64")
            (self.venv / "bin").mkdir(parents=True)
            (self.venv / "bin" / "ffmpeg").symlink_to(target)
            self.assertEqual(listen_pipeline.ffmpeg(), str(self.venv / "bin" / "ffmpeg"))
            target.unlink()                                       # a dangling link is no ffmpeg
            self.assertEqual(listen_pipeline.ffmpeg(), str(on_path))
        with mock.patch.dict(os.environ, {"PATH": str(self.root / "empty")}), \
                mock.patch.object(listen_pipeline, "PATH_EXTRA", ()):
            with self.assertRaises(listen_pipeline.FfmpegMissing) as missing:
                listen_pipeline.ffmpeg()
            self.assertIn("shaft listen on", str(missing.exception))
            with self.assertRaises(listen_pipeline.FfmpegMissing) as decode:
                listen_pipeline.decode(self.root / "a.wav", self.root, "inperson")
            self.assertTrue(str(decode.exception).startswith("Audio decode failed: ffmpeg is not installed"))

    def test_a_speaker_clip_with_no_ffmpeg_says_so_not_only_could_not_read(self):
        # Bug #58: "Could not read speaker clip." hid the missing ffmpeg.
        metadata = {"state": "ready", "ext": "wav", "mode": "inperson"}
        (self.root / "transcript.json").write_text(json.dumps({"speakers": {"M1": {"channel": 1, "clips": [[0, 1]]}}}))
        (self.root / "audio.wav").write_bytes(b"x")
        with mock.patch.object(meetings, "_locked", return_value=contextlib.nullcontext((self.root, metadata))), \
                mock.patch.object(listen_pipeline, "ffmpeg", side_effect=listen_pipeline.FfmpegMissing("no ffmpeg: run `shaft listen on`")):
            with self.assertRaises(meetings.Conflict) as e:
                speakers.clip("example", "M1", 0)
        self.assertEqual(str(e.exception), "Could not read speaker clip: no ffmpeg: run `shaft listen on`")

    def test_missing_until_both_the_environment_and_the_service_exist(self):
        self.assertEqual(listen_setup.missing(), listen_setup.NOT_SET_UP)
        self.executable(self.venv / "bin" / "python")
        self.assertEqual(listen_setup.missing(), listen_setup.NOT_SET_UP)    # no plist yet
        (self.root / "LaunchAgents").mkdir()
        (self.root / "LaunchAgents" / "com.shaft.listen.plist").write_bytes(b"")
        self.assertIsNone(listen_setup.missing())

    def test_setup_uses_the_apps_uv_and_python_pins_the_engines_and_links_the_wheels_ffmpeg(self):
        wheel = self.executable(self.root / "site" / "imageio_ffmpeg" / "binaries" / "ffmpeg-macos-aarch64-v9.0")
        ran = []

        def run(command, **kwargs):
            ran.append(command)
            if command[1:3] == ["venv", "--clear"]:
                self.executable(self.venv / "bin" / "python")
            out = str(wheel) if "-c" in command else "ffmpeg version 9.0" if command[-1] == "-version" else ""
            return subprocess.CompletedProcess(command, 0, out, "")

        uv = "/Users/x/SHAFT/.runtime/bin/uv"
        with mock.patch.object(listen_setup.subprocess, "run", side_effect=run), \
                mock.patch.object(listen_setup.pyenv, "uv_binary", return_value=uv), \
                mock.patch.object(listen_setup.platform, "system", return_value="Darwin"), \
                mock.patch.object(listen_setup.platform, "machine", return_value="arm64"):
            done = listen_setup.setup()
            self.assertEqual(ran[0], [uv, "venv", "--clear", "--python", core.PYTHON, str(self.venv)])
            self.assertEqual(ran[1], [uv, "pip", "install", "--python", str(self.venv / "bin" / "python"),
                                      *listen_setup.PACKAGES])
            self.assertIn("imageio-ffmpeg==0.8.2", listen_setup.PACKAGES)
            self.assertEqual(os.readlink(self.venv / "bin" / "ffmpeg"), str(wheel))
            self.assertEqual(listen_pipeline.ffmpeg(), str(self.venv / "bin" / "ffmpeg"))
            self.assertIn("ffmpeg version 9.0", done[-1])
            ran.clear()
            listen_setup.setup()                                  # a rerun never remakes the environment
            self.assertNotIn("venv", [c[1] for c in ran if c[0] == uv])
        with mock.patch.object(listen_setup.platform, "machine", return_value="x86_64"):
            with self.assertRaisesRegex(listen_setup.SetupFailed, "Apple Silicon"):
                listen_setup.setup()

    def test_listen_on_sets_up_first_and_queues_the_waiting_meetings_a_failed_setup_installs_nothing(self):
        from supervisor import __main__ as cli
        with mock.patch.object(listen_setup, "setup", side_effect=listen_setup.SetupFailed("uv pip install … failed")), \
                mock.patch.object(autonomy, "service") as service, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(cli.cmd_listen("on"), 1)
        service.assert_not_called()
        self.assertIn("shaft listen on: uv pip install … failed", err.getvalue())
        with mock.patch.object(listen_setup, "setup", return_value=["engines: …"]), \
                mock.patch.object(autonomy, "service", return_value=0) as service, \
                mock.patch.object(core, "secrets_env", return_value={"HF_TOKEN": "x"}), \
                mock.patch.object(meetings, "requeue_waiting", return_value=["m-1"]) as requeue, \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(cli.cmd_listen("on"), 0)
        service.assert_called_once_with("listen", "on")
        requeue.assert_called_once_with()
        self.assertIn("queued again: m-1", out.getvalue())
