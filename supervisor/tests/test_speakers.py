"""Exercise speaker enrolment and matching through the worker, CLI, and paired app."""
from __future__ import annotations

import io
import json
import shutil
import struct
import sys
import wave
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock, skipUnless

from supervisor import listen_pipeline, listen_worker, meetings, speakers
from supervisor.tests import test_app_api
from supervisor.tests.test_listen import FakeEngines, spoken


class SpeakerBase(test_app_api.AppTest):
    def setUp(self):
        super().setUp()
        self.source = self.sup / "example.wav"
        self.source.write_bytes(b"synthetic placeholder")
        self.enterContext(mock.patch.object(speakers, "MATCH", 0.6))
        self.gallery = self.sup / "speakers"
        self.people = "/app/v1/speakers"

    def meeting(self, vectors, *, mode="inperson", turns=None):
        meeting_id = meetings.import_audio(self.source, title="Example", mode=mode)["id"]
        turns = turns or {channel: [(i * 12, i * 12 + 7, label) for i, label in enumerate(by_label)]
                          for channel, by_label in vectors.items()}
        results = {(channel, start, end): spoken("Example words.", start)
                   for channel, spans in turns.items() for start, end, _ in spans}
        self.process(FakeEngines(turns, results, vectors), mode)
        return meeting_id, f"/app/v1/meetings/{meeting_id}"

    def process(self, engine, mode="inperson"):
        paths = [Path("ch1.wav")] if mode == "inperson" else [Path("ch1.wav"), Path("ch2.wav")]
        with mock.patch.object(listen_pipeline, "decode", return_value=paths):
            self.assertTrue(listen_worker.process_next(engine))

    def reprocess(self, path, vectors):
        self.get(path + "/reprocess", method="POST")
        self.process(FakeEngines({"ch1": [(0, 7, "A")]}, {("ch1", 0, 7): spoken("Example words.")},
                                 {"ch1": {"A": vectors}}))

    def name(self, path, label, name):
        return self.get(path + "/speakers", method="POST", data={"label": label, "name": name})

    def transcript(self, path):
        return self.get(path)["transcript"]

    def folder(self, meeting_id):
        return self.sup / "meetings" / meeting_id


class SpeakerTest(SpeakerBase):

    def test_confirmation_updates_transcript_and_matching_on_reprocess(self):
        from supervisor.__main__ import main

        first, source = self.meeting({"ch1": {"A": (1, 0)}})
        second, target = self.meeting({"ch1": {"A": (1, 0)}})
        self.assertNotIn("name", self.transcript(target)["speakers"]["M1"])
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["meetings", "name", first, "M1", "Example Person", "--json"]), 0)
        pid = json.loads(output.getvalue())["person"]
        self.assertEqual(self.name(source, "M1", "example person")["person"], pid)
        self.assertEqual(self.get(self.people)["people"][0]["count"], 1)
        self.assertIn("Example Person: Example words.", (self.folder(first) / "transcript.md").read_text())
        self.reprocess(target, (1, 0))
        self.assertEqual(self.transcript(target)["speakers"]["M1"], {
            "channel": 1, "seconds": 7, "clips": [[0, 7]], "person": pid, "name": "Example Person",
        })
        self.assertIn("Example Person: Example words.", (self.folder(second) / "transcript.md").read_text())

    def test_below_threshold_and_near_tie_stay_unknown(self):
        _, first = self.meeting({"ch1": {"A": (1, 0, 0), "B": (1, 0.2, 0)}})
        self.name(first, "M1", "Example One")
        self.name(first, "M2", "Example Two")
        meeting_id, path = self.meeting({"ch1": {"A": (0, 0, 1), "B": (1, 0, 0)}})
        self.assertEqual([row.get("person") for row in self.transcript(path)["speakers"].values()], [None, None])
        self.assertIn("Speaker M2 (unknown)", (self.folder(meeting_id) / "transcript.md").read_text())

    def test_prefers_channel_centroid_and_reuses_name_for_two_labels(self):
        _, first = self.meeting({"ch1": {"A": (1, 0)}})
        named = self.name(first, "M1", "Example Person")
        _, second = self.meeting({"ch2": {"A": (0, 1)}}, mode="online")
        self.name(second, "S1", "Example Person")
        _, third = self.meeting({"ch1": {"A": (1, 0), "B": (1, 0), "C": (0, 1)}})
        entries = self.transcript(third)["speakers"]
        self.assertEqual([entries[label].get("person") for label in ("M1", "M2", "M3")],
                         [named["person"], named["person"], None])

    def test_falls_back_to_other_channel_samples(self):
        _, first = self.meeting({"ch1": {"A": (1, 0)}})
        named = self.name(first, "M1", "Example Person")
        _, second = self.meeting({"ch2": {"A": (1, 0)}}, mode="online")
        self.assertEqual(self.transcript(second)["speakers"]["S1"]["person"], named["person"])

    def test_forget_removes_voiceprints_retains_text_and_blocks_rematch(self):
        first, source = self.meeting({"ch1": {"A": (1, 0)}})
        pid = self.name(source, "M1", "Example Person")["person"]
        second, target = self.meeting({"ch1": {"A": (1, 0)}})
        self.assertEqual(self.transcript(target)["speakers"]["M1"]["person"], pid)
        self.assertEqual(self.get(self.people + "/" + pid, method="DELETE"), {"id": pid, "deleted": True})
        self.assertEqual(self.get(self.people), {"people": []})
        self.assertIn(pid, json.loads((self.gallery / "tombstones.json").read_bytes()))
        for folder in (self.gallery / "embeddings", self.folder(first), self.folder(second)):
            self.assertEqual(list(folder.glob("*.npz")), [])
        self.assertIn("Example Person: Example words.", (self.folder(second) / "transcript.md").read_text())
        self.assertEqual(self.transcript(target)["speakers"]["M1"]["clips"], [])
        self.reprocess(target, (1, 0))
        self.assertNotIn("name", self.transcript(target)["speakers"]["M1"])
        _, fresh = self.meeting({"ch1": {"A": (1, 0)}})
        self.assertNotIn("person", self.transcript(fresh)["speakers"]["M1"])

    def test_rename_and_anonymise_rerender_and_anonymisation_survives_reprocess(self):
        from supervisor.__main__ import main

        meeting_id, path = self.meeting({"ch1": {"A": (1, 0)}})
        pid = self.name(path, "M1", "Example One")["person"]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["speakers", "list"]), 0)
        self.get(self.people + "/" + pid + "/rename", method="POST", data={"name": "Example Two"})
        markdown = self.folder(meeting_id) / "transcript.md"
        self.assertIn("Example Two: Example words.", markdown.read_text())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["speakers", "forget", pid, "--anonymise"]), 0)
        for _ in range(2):
            self.assertEqual(self.transcript(path)["speakers"]["M1"],
                             {"channel": 1, "seconds": 7, "clips": [], "name": "Speaker M1"})
            self.assertEqual(markdown.read_text(), "00:00:00 Speaker M1: Example words.\n")
            self.reprocess(path, (1, 0))

    def test_merge_moves_segments_without_enrolling(self):
        meeting_id, path = self.meeting({"ch1": {"A": (1, 0), "B": (0, 1)}})
        merged = self.get(path + "/speakers", method="POST", data={"label": "M2", "merge_into": "M1"})
        transcript = self.transcript(path)
        self.assertEqual(merged, {"label": "M1", "channel": 1, "seconds": 14, "clips": [[0, 7]]})
        self.assertEqual(list(transcript["speakers"]), ["M1"])
        self.assertEqual([row["speaker"] for row in transcript["segments"]], ["M1", "M1"])
        self.assertEqual((self.folder(meeting_id) / "transcript.md").read_text().count("Speaker M1 (unknown)"), 2)
        self.assertEqual(self.get(self.people), {"people": []})

    def test_meeting_delete_only_removes_rows_learned_from_it_on_request(self):
        _, source = self.meeting({"ch1": {"A": (1, 0)}})
        _, retained = self.meeting({"ch1": {"A": (1, 0)}})
        self.name(source, "M1", "Example Person")
        self.name(retained, "M1", "Example Person")
        self.get(source + "?forget_voices=1", method="DELETE")
        self.assertEqual(self.get(self.people)["people"][0]["count"], 1)
        self.get(retained, method="DELETE")
        self.assertEqual(self.get(self.people)["people"][0]["count"], 1)

    @skipUnless(shutil.which("ffmpeg"), "ffmpeg is unavailable")
    def test_clip_is_bounded_wav_from_the_requested_channel(self):
        with wave.open(str(self.source), "wb") as wav:
            wav.setparams((2, 2, 8000, 0, "NONE", ""))
            wav.writeframes(struct.pack("<hh", 1000, -2000) * 8000 * 18)
        _, path = self.meeting({"ch2": {"A": (1, 0)}}, mode="online", turns={"ch2": [(2, 16, "A")]})
        self.assertEqual(self.transcript(path)["speakers"]["S1"]["clips"], [[2, 12]])
        response = self.api(path + "/clip?speaker=S1&n=0")
        self.assertEqual(response.status, 200, response.body[:100])
        self.assertEqual(response.headers["Content-Type"], "audio/wav")
        with wave.open(io.BytesIO(response.body), "rb") as wav:
            self.assertEqual((wav.getnchannels(), wav.getframerate(), wav.getnframes()), (1, 16_000, 160_000))
            self.assertEqual(struct.unpack("<h", wav.readframes(1)), (-2000,))
        for query in ("speaker=M1&n=0", "speaker=S1&n=1"):
            self.error(self.api(path + "/clip?" + query), 404, "not_found")

    def test_three_longest_clips_exclude_overlapping_speech(self):
        exclusive = [(0, 10, "A"), (10, 14, "B"), (20, 31, "A"), (40, 46, "A"),
                     (50, 58, "A"), (60, 67, "A")]
        original = exclusive + [(3, 8, "B")]
        _, path = self.meeting({"ch1": {"A": (1, 0), "B": (0, 1)}}, turns={
            "ch1": listen_pipeline.DiarisationTurns(exclusive, original),
        })
        self.assertEqual(self.transcript(path)["speakers"]["M1"]["clips"], [[20, 30], [50, 58], [60, 67]])
        self.assertEqual(self.transcript(path)["speakers"]["M2"]["clips"], [])

    def test_every_speaker_route_requires_a_paired_bearer(self):
        _, path = self.meeting({"ch1": {"A": (1, 0)}})
        for method, route in (("GET", path + "/clip?speaker=M1"), ("POST", path + "/speakers"),
                              ("GET", self.people), ("POST", self.people + "/p-aabbccdd/rename"),
                              ("DELETE", self.people + "/p-aabbccdd"), ("DELETE", path + "?forget_voices=1")):
            with self.subTest(route=route):
                self.error(self.api(route, method, bearer=False), 401, "unpaired")

    def test_numpy_can_load_the_float32_meeting_and_gallery_archives(self):
        try:
            import numpy
        except ModuleNotFoundError:
            self.skipTest("NumPy is unavailable in this Python")
        vector = numpy.array([1, 0.25], dtype=numpy.float64)
        meeting_id, path = self.meeting({"ch1": {"A": vector}})
        with numpy.load(self.folder(meeting_id) / "speakers.npz", allow_pickle=False) as archive:
            self.assertEqual(archive.files, ["M1"])
            self.assertEqual(archive["M1"].dtype, numpy.float32)
            numpy.testing.assert_array_equal(archive["M1"], vector)
        pid = self.name(path, "M1", "Example Person")["person"]
        with numpy.load(self.gallery / "embeddings" / (pid + ".npz"), allow_pickle=False) as archive:
            self.assertEqual(archive.files, [f"{meeting_id}:M1:mic"])
            numpy.testing.assert_array_equal(archive[archive.files[0]], vector)

    def test_engine_uses_regular_diarisation_label_order_for_embeddings(self):
        early, late = SimpleNamespace(start=0, end=7), SimpleNamespace(start=8, end=15)
        regular = mock.MagicMock()
        regular.__iter__.return_value = iter([(early, "Z"), (late, "A")])
        regular.labels.return_value = ["A", "Z"]
        result = SimpleNamespace(speaker_diarization=regular,
                                 exclusive_speaker_diarization=[(early, "Z"), (late, "A")],
                                 speaker_embeddings=[(1, 0), (0, 1)])
        soundfile = SimpleNamespace(read=lambda *_a, **_k: (mock.MagicMock(__len__=lambda _: 1), 16_000))
        engine = listen_worker.Engines()
        engine.pipeline = mock.Mock(return_value=result)
        with mock.patch.dict(sys.modules, {"soundfile": soundfile, "torch": mock.Mock(),
                                           "pyannote.audio": SimpleNamespace(Pipeline=object)}):
            turns, vectors = engine.diarise(self.source)
        self.assertEqual(turns, [(0, 7, "Z"), (8, 15, "A")])
        self.assertEqual(vectors, {"A": (1, 0), "Z": (0, 1)})


class OperatorPushTest(SpeakerBase):
    """The operator heard nothing when a meeting became ready or failed (review 2026-11-27): only the Meetings window
    showed it. Every finished processing now pushes once to the paired devices, and the tap opens the meeting."""

    def setUp(self):
        super().setUp()
        from supervisor import webpush
        self.pushed = []
        self.enterContext(mock.patch.object(webpush, "notify", side_effect=lambda e, **_: self.pushed.append(e) or []))

    def test_ready_pushes_who_spoke_and_how_many_voices_need_a_name(self):
        first, path = self.meeting({"ch1": {"A": (1, 0)}})
        self.name(path, "M1", "Example Person")
        self.pushed.clear()
        second, _ = self.meeting({"ch1": {"A": (1, 0), "B": (0, 1)}})
        self.assertEqual(len(self.pushed), 1, self.pushed)
        event = self.pushed[0]
        self.assertEqual(event["title"], "Meeting ready: Example")
        self.assertEqual(event["url"], f"/overview?meeting={second}")
        self.assertIn("Example Person", event["body"])
        self.assertIn("1 voice to name", event["body"])
        self.assertIn("unfiled", event["body"])

    def test_failed_pushes_the_error_once(self):
        meeting_id = meetings.import_audio(self.source, title="Example", mode="inperson")["id"]
        meetings.set_state(meeting_id, "processing")
        meetings.set_state(meeting_id, "failed", error="Audio decode failed: moov atom not found")
        meetings.set_state(meeting_id, "failed", error="Audio decode failed: moov atom not found")
        self.assertEqual(self.pushed, [{"title": "Meeting failed: Example",
                                        "body": "Audio decode failed: moov atom not found",
                                        "url": f"/overview?meeting={meeting_id}", "tag": f"meeting-{meeting_id}", "cid": None}])

    def test_a_worker_that_stops_twice_pushes_the_failure(self):
        meeting_id = meetings.import_audio(self.source, title="Example", mode="inperson")["id"]
        for _ in range(2):
            meetings.set_state(meeting_id, "processing")
            meetings.recover_processing(meeting_id)
        self.assertEqual([e["title"] for e in self.pushed], ["Meeting failed: Example"])
        self.assertIn("worker stopped", self.pushed[0]["body"])

    def test_a_push_that_fails_still_leaves_the_meeting_ready(self):
        from supervisor import webpush
        with mock.patch.object(webpush, "notify", side_effect=OSError("disk full")):
            meeting_id, path = self.meeting({"ch1": {"A": (1, 0)}})
        self.assertEqual(self.get(path)["state"], "ready")

    def test_an_instance_proposal_pushes_and_opens_the_meeting(self):
        from supervisor import meeting_proposals
        meeting_id, _ = self.meeting({"ch1": {"A": (1, 0)}})
        self.pushed.clear()
        metadata = json.loads((self.folder(meeting_id) / "meeting.json").read_text())
        meeting_proposals.create("example", metadata, "M1", "Example Person", "The invite lists them.")
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(self.pushed[0]["url"], f"/overview?meeting={meeting_id}")
        self.assertIn("Example Person", self.pushed[0]["body"])
        rows = self.get("/app/v1/meetings/proposals")["proposals"]
        self.assertEqual([(r["speaker"], r["name"]) for r in rows], [("M1", "Example Person")])
