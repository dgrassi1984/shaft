"""The model dropped the space between word and number, 2026-11-13."""
import io
import re
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
from shaft.llm import Client
from shaft.spacing import respace
from supervisor.tests.fakellm import FakeLLM


class RespaceTest(unittest.TestCase):
    def test_prose_boundaries(self):
        for original, expected in (
            ("Riscontro autonomo13nov10:22", "Riscontro autonomo 13 nov 10:22"),
            ("consegnato09:59. GET HTTP200", "consegnato 09:59. GET HTTP 200"),
            ("alle09:30 del9 novembre12 lunedì10", "alle 09:30 del 9 novembre 12 lunedì 10"),
        ):
            with self.subTest(original=original):
                self.assertEqual(respace(original), expected)

    def test_general_word_number_glue_2026_11_17(self):
        # The narrow rules let "Annex1", "sezioni1–5", "e2nov", "per24mesi" and "riga571" through into a document the
        # operator read; the instance then copied them from its own notes.
        for original, expected in (
            ("Letto fino a sezioni1–5, firme e Annex1; letti Annex2, Annex3 e matrice Annex4.",
             "Letto fino a sezioni 1–5, firme e Annex 1; letti Annex 2, Annex 3 e matrice Annex 4."),
            ("Confronto con corpi mail 30 ott e2nov citati", "Confronto con corpi mail 30 ott e 2 nov citati"),
            ("per24mesi dopo, oltre10settimane.", "per 24 mesi dopo, oltre 10 settimane."),
            ("Riletti riga571, righe553/557/590 e seguito492; entro7giorni.",
             "Riletti riga 571, righe 553/557/590 e seguito 492; entro 7 giorni."),
            ("creato585entro3giorni, recuperato7.6h, serie30d, totale23,70EUR",
             "creato 585 entro 3 giorni, recuperato 7.6h, serie 30d, totale 23,70EUR"),
            ("Nuove mail Valeriya17nov12:11/12:22 chiedono", "Nuove mail Valeriya 17 nov 12:11/12:22 chiedono"),
            ("weekly16, in571 o9, prevede1% del totale", "weekly 16, in 571 o 9, prevede 1% del totale"),
        ):
            with self.subTest(original=original):
                self.assertEqual(respace(original), expected)
                self.assertIsNone(re.search(r"[a-z]{4}\d", respace(original)))

    def test_names_seen_glued_before_the_glue_are_unchanged(self):
        # Measured on every instance's prose before 2026-11-08: these are names, ids and units, not glue.
        text = ("python3 Python3 base64 base64url latin1 Sheet1 knowbe4 Kali365 auth0 sha256 md5 Gen4 v0 user1984 "
                "MG4 SGIC18 CdA18 UniCredit16 DD24 msg4 cap10 e2e 4th 30min 72h 650k 3pm frmq3 "
                "'7dcndrb42vh3nnnieoa11kn8kk_20261020T080000Z' 78qdnelhei941gjmaf3de12dr8 "
                "absqmontage202610211712zab9 zzzabs2028q4 ordine406-9204337-7246705 info1@meet.example.it "
                "dfae5750 110fecb 0xaf88d065e77c8cc test17b-escape brief4.md notes/riga571.md")
        self.assertEqual(respace(text), text)

    def test_identifiers_and_protected_text_are_unchanged(self):
        for text in (
            "python3 sha256 IL6 Gen4 set1 2026-11-09T09:30 v0.44.0 c1108-1951-1 web-e98ea257829d7c02",
            "(27NOV26) 3Dec25 17feb5e1 %Y-%m-%dT00:00",
            "`alle09:30` https://x.y/del12 notes/del12nov.md",
            "Mon, Nov 9, 2026 at 9:34 AM",
            "```text\nalle09:30\n```",
        ):
            with self.subTest(text=text):
                self.assertEqual(respace(text), text)

    def test_complete_and_stream_repair_prose_but_preserve_code(self):
        calls = [("append_file", {"path": "notes/x.md", "content": "alle09:30"}),
                 ("write_file", {"path": "plugins/x/main.py", "content": "alle09:30"}),
                 ("run", {"command": "echo alle09:30"})]
        for stream in (False, True):
            with self.subTest(stream=stream), tempfile.TemporaryDirectory() as tmp:
                fake = FakeLLM([{"content": "Riscontro autonomo13nov10:22", "tool_calls": calls}])
                try:
                    client = Client(fake.url, "m", "", 30, Path(tmp) / "usage.jsonl")
                    call = client.stream if stream else client.complete
                    reply = call([{"role": "user", "content": "x"}], 100, "test")
                finally:
                    fake.close()
                self.assertEqual(reply.content, "Riscontro autonomo 13 nov 10:22")
                self.assertNotIn("autonomo13nov", reply.content)
                self.assertEqual(json.loads(reply.tool_calls[0]["arguments"]),
                                 {"path": "notes/x.md", "content": "alle 09:30"})
                for result, (_, args) in zip(reply.tool_calls[1:], calls[1:]):
                    self.assertEqual(result["arguments"], json.dumps(args))

    def test_argument_recursion_exclusions_and_exact_json(self):
        protected = {key: "alle09:30" for key in
                     ("path", "id", "name", "to", "project", "slug", "commitment_id", "other_id",
                      "made_to", "conversation", "supersedes", "reopens")}
        cases = [
            ("finish_cycle", json.dumps({"unfinished": ["alle09:30", {**protected, "text": "del9"}],
                                        "count": 2, "done": None}),
             {"unfinished": ["alle 09:30", {**protected, "text": "del 9"}], "count": 2, "done": None}),
            ("remember", '{ "fact" : "already spaced", "source": "operator" }', None),
            ("remember", '{"fact": "alle09:30"', None),
            # A tool the instance built itself is covered too (the glue reached a task_queue plugin's tasks).
            ("task_queue", '{ "task": "brief del12nov entro09:00", "task_id": "del9" }',
             {"task": "brief del 12 nov entro 09:00", "task_id": "del9"}),
            ("read_file", '{ "path": "alle09:30" }', None),
            ("append_file", '{ "path": "notes/x.py", "content": "alle09:30" }', None),
            ("write_file", '{ "path": "notes/del12nov.txt", "content": "alle09:30", "name": "del9" }',
             {"path": "notes/del12nov.txt", "content": "alle 09:30", "name": "del9"}),
            ("append_file", '{"path":"notes/x.markdown","content":"lunedì10"}',
             {"path": "notes/x.markdown", "content": "lunedì 10"}),
        ]
        for stream in (False, True):
            with self.subTest(stream=stream), tempfile.TemporaryDirectory() as tmp:
                calls = [{"id": str(i), "function": {"name": name, "arguments": args}}
                         for i, (name, args, _) in enumerate(cases)]
                if stream:
                    chunk = {"choices": [{"delta": {"content": "ok", "tool_calls": [
                        {"index": i, **call} for i, call in enumerate(calls)]}, "finish_reason": "tool_calls"}]}
                    wire = ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()
                else:
                    wire = json.dumps({"choices": [{"message": {"content": "ok", "tool_calls": calls},
                                                    "finish_reason": "tool_calls"}]}).encode()
                client = Client("http://example.test", "m", "", 30, Path(tmp) / "usage.jsonl")
                with patch("shaft.llm.urllib.request.urlopen", return_value=io.BytesIO(wire)):
                    call = client.stream if stream else client.complete
                    reply = call([{"role": "user", "content": "x"}], 100, "test")
                for result, (_, original, expected) in zip(reply.tool_calls, cases):
                    if expected is None:
                        self.assertEqual(result["arguments"], original)
                    else:
                        self.assertEqual(result["arguments"], json.dumps(expected, ensure_ascii=False))
