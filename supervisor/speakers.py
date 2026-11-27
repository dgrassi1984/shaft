"""Local voiceprints and operator-confirmed speaker names."""
from __future__ import annotations

import ast
import fcntl
import io
import json
import math
import re
import secrets
import struct
import subprocess
import sys
import wave
import zipfile
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path

from . import core, listen_pipeline, meetings
from .core import knob

ID = r"p-[0-9a-f]{8}"
AMBIGUITY = 0.05


def _threshold(raw: str) -> float:
    value = float(raw)
    if not -1 <= value <= 1:
        raise ValueError("must be between -1 and 1")
    return value


MATCH, _ = knob("SHAFT_LISTEN_MATCH", "0.6", _threshold,
                "minimum cosine similarity for automatic speaker matching (uncalibrated)",
                group="Meetings", services=("listen",))


@contextmanager
def _gallery():
    # When both locks are needed, take the gallery lock before the meeting lock.
    root = core.SUP_DIR / "speakers"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / ".lock").touch(mode=0o600)
    with (root / ".lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield root


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return default


def _write_json(path: Path, value) -> None:
    with meetings._atomic_output(path) as out:
        out.write(meetings._json_bytes(value))


# The app API and CLI run outside the Listen venv. NPZ here is a ZIP of 1-D
# little-endian float32 NPY arrays and needs no NumPy or pickle to read.
_NPY = b"\x93NUMPY\x01\x00"


def _write_vectors(path: Path, vectors: dict) -> None:
    with meetings._atomic_output(path) as out, zipfile.ZipFile(out, "w") as archive:
        for key, vector in vectors.items():
            size = len(vector)
            header = f"{{'descr': '<f4', 'fortran_order': False, 'shape': ({size},), }}".encode("ascii")
            header += b" " * (-(len(header) + 11) % 64) + b"\n"
            archive.writestr(key + ".npy", _NPY + struct.pack("<H", len(header)) + header
                             + struct.pack(f"<{size}f", *vector))


def _read_vectors(path: Path) -> dict:
    try:
        archive = zipfile.ZipFile(path)
    except FileNotFoundError:
        return {}
    with archive:
        vectors = {}
        for entry in archive.namelist():
            data = archive.read(entry)
            if data[:8] != _NPY:
                raise ValueError("Unsupported voiceprint format.")
            offset = 10 + struct.unpack_from("<H", data, 8)[0]
            header = ast.literal_eval(data[10:offset].decode("ascii").strip())
            if header["descr"] != "<f4" or len(header["shape"]) != 1:
                raise ValueError("Unsupported voiceprint format.")
            size, = header["shape"]
            vectors[entry.removesuffix(".npy")] = struct.unpack(f"<{size}f", data[offset:])
        return vectors


def _person_file(root: Path, pid: str) -> Path:
    return root / "embeddings" / f"{pid}.npz"


def _save_vectors(path: Path, rows: dict) -> None:
    if rows:
        path.parent.mkdir(mode=0o700, exist_ok=True)
        _write_vectors(path, rows)
    else:
        path.unlink(missing_ok=True)


def _cosine(vector, centroid) -> float:
    denominator = math.hypot(*vector) * math.hypot(*centroid)
    if not denominator or not math.isfinite(denominator):
        return -math.inf
    return sum(a * b for a, b in zip(vector, centroid)) / denominator


def _kind(speaker: dict) -> str:
    return "mic" if speaker["channel"] == 1 else "system"


def _centroids(rows: dict) -> dict:
    groups = {}
    for source, row in rows.items():
        kind = source.rsplit(":", 1)[1]
        groups.setdefault((kind, len(row)), []).append(row)
        groups.setdefault(("", len(row)), []).append(row)
    return {key: tuple(sum(column) / len(group) for column in zip(*group)) for key, group in groups.items()}


def _match(vector, kind, gallery):
    scores = []
    for person, centroids in gallery:
        key = (kind, len(vector))
        centroid = centroids.get(key, centroids.get(("", len(vector))))
        if centroid is not None:
            scores.append((_cosine(vector, centroid), person))
    scores.sort(key=lambda pair: pair[0], reverse=True)
    if not scores or scores[0][0] < MATCH:
        return None
    best, person = scores[0]
    if len(scores) > 1 and best - scores[1][0] <= AMBIGUITY:
        return None
    return person


def _render(folder: Path, transcript: dict) -> None:
    meetings._write_transcript(folder, transcript, listen_pipeline.render_markdown(transcript))


def _assign(speaker: dict, person: dict) -> None:
    speaker.update(person=person["id"], name=person["name"])


def known_names() -> list[str]:
    """The names of the people in the gallery, oldest first (forgotten people left out)."""
    with _gallery() as root:
        tombstones = _read_json(root / "tombstones.json", {})
        return [person["name"] for person in _read_json(root / "people.json", []) if person["id"] not in tombstones]


def complete_processing(meeting_id: str, transcript: dict, vectors: dict) -> None:
    with _gallery() as root, meetings._locked(meeting_id) as (folder, metadata):
        if metadata["state"] != "processing":
            raise meetings.Conflict("This meeting is no longer processing.")
        tombstones = _read_json(root / "tombstones.json", {})
        gallery = [(person, _centroids(_read_vectors(_person_file(root, person["id"]))))
                   for person in _read_json(root / "people.json", []) if person["id"] not in tombstones]
        for label, speaker in transcript["speakers"].items():
            forgotten = [entry for entry in tombstones.values() if label in entry["sources"].get(meeting_id, [])]
            if forgotten:
                speaker["clips"] = []
                vectors.pop(label, None)
                if any(entry["anonymise"] for entry in forgotten):
                    speaker["name"] = f"Speaker {label}"
            elif label in vectors and (person := _match(vectors[label], _kind(speaker), gallery)):
                _assign(speaker, person)
        _save_vectors(folder / "speakers.npz", vectors)
        meetings._complete_processing(folder, metadata, transcript, listen_pipeline.render_markdown(transcript))


def _name(value) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.splitlines()) != 1:
        raise meetings.Invalid("name must be a non-empty single line.")
    try:
        value.encode()
    except UnicodeError:
        raise meetings.Invalid("name must contain valid Unicode.") from None
    return value.strip()


def _speaker(transcript: dict, label) -> dict:
    if not isinstance(label, str) or label not in transcript["speakers"]:
        raise meetings.Missing("No such speaker label.")
    return transcript["speakers"][label]


def _ready_transcript(folder: Path, metadata: dict) -> dict:
    if metadata["state"] != "ready":
        raise meetings.Conflict("The meeting is not ready.")
    return _read_json(folder / "transcript.json", {})


def name_label(meeting_id: str, label, name) -> dict:
    return _name_label(meeting_id, label, name)


_ANY_REVISION = object()


def _name_label(meeting_id, label, name, *, expected_revision=_ANY_REVISION) -> dict:
    name = _name(name)
    with _gallery() as root, meetings._locked(meeting_id) as (folder, metadata):
        if expected_revision is not _ANY_REVISION and metadata.get("ready_revision") != expected_revision:
            raise meetings.Conflict("The meeting was reprocessed after this proposal.")
        transcript = _ready_transcript(folder, metadata)
        speaker = _speaker(transcript, label)
        vector = _read_vectors(folder / "speakers.npz").get(label)
        if vector is None or not math.isfinite(math.hypot(*vector)) or not any(vector):
            raise meetings.Conflict("This label has no usable voiceprint.")
        people_path = root / "people.json"
        people = _read_json(people_path, [])
        tombstones = _read_json(root / "tombstones.json", {})
        person = next((p for p in people if p["name"].casefold() == name.casefold() and p["id"] not in tombstones),
                      None)
        if person is None:
            reserved = {p["id"] for p in people} | tombstones.keys()
            pid = "p-" + secrets.token_hex(4)
            while pid in reserved:
                pid = "p-" + secrets.token_hex(4)
            person = {"id": pid, "name": name, "created": datetime.now().astimezone().isoformat()}
            people.append(person)
            _write_json(people_path, people)
        source = f"{meeting_id}:{label}:{_kind(speaker)}"
        for other in people:
            if other != person:
                path = _person_file(root, other["id"])
                rows = _read_vectors(path)
                if source in rows:
                    del rows[source]
                    _save_vectors(path, rows)
        path = _person_file(root, person["id"])
        rows = _read_vectors(path)
        rows[source] = vector
        _save_vectors(path, rows)
        _assign(speaker, person)
        _render(folder, transcript)
        return {"label": label, **speaker}


def merge_labels(meeting_id: str, label, merge_into) -> dict:
    with meetings._locked(meeting_id) as (folder, metadata):
        transcript = _ready_transcript(folder, metadata)
        target = _speaker(transcript, merge_into)
        if label == merge_into:
            raise meetings.Invalid("Choose a different destination label.")
        source = _speaker(transcript, label)
        for row in transcript["segments"]:
            if row["speaker"] == label:
                row["speaker"] = merge_into
        target["seconds"] += source["seconds"]
        del transcript["speakers"][label]
        archive = folder / "speakers.npz"
        vectors = _read_vectors(archive)
        if label in vectors:
            del vectors[label]
            _save_vectors(archive, vectors)
        _render(folder, transcript)
        return {"label": merge_into, **target}


def clip(meeting_id: str, label, index: int) -> bytes:
    with meetings._locked(meeting_id) as (folder, metadata):
        speaker = _speaker(_ready_transcript(folder, metadata), label)
        ranges = speaker.get("clips", [])
        if not 0 <= index < len(ranges):
            raise meetings.Missing("No such speaker clip.")
        start, end = ranges[index]
        audio = folder / f"audio.{metadata['ext']}"
        listen_pipeline.normalize_caf(audio)
        command = ["ffmpeg", "-nostdin", "-v", "error", "-ss", str(start), "-i", str(audio), "-map", "0:a:0"]
        if metadata["mode"] == "online":
            command += ["-af", f"channelmap=map={speaker['channel'] - 1}:channel_layout=mono"]
        rate = listen_pipeline.SAMPLE_RATE
        command += ["-t", str(min(end - start, 10)), "-ac", "1", "-ar", str(rate), "-f", "s16le", "pipe:1"]
        try:
            pcm = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            raise meetings.Conflict("Could not read speaker clip.") from None
        if not pcm:
            raise meetings.Missing("No such speaker clip.")
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setparams((1, 2, rate, 0, "NONE", ""))
            wav.writeframes(pcm[:10 * rate * 2])
        return output.getvalue()


def list_people() -> list[dict]:
    with _gallery() as root:
        tombstones = _read_json(root / "tombstones.json", {})
        return [{**p, "count": len(_read_vectors(_person_file(root, p["id"])))}
                for p in _read_json(root / "people.json", []) if p["id"] not in tombstones]


def _find(people: list, pid: str) -> dict:
    for person in people:
        if person["id"] == pid:
            return person
    raise meetings.Missing("No such person.")


@contextmanager
def _saved_transcript(meeting_id: str):
    with ExitStack() as stack:
        try:
            folder, _ = stack.enter_context(meetings._locked(meeting_id))
        except meetings.Missing:
            yield None, None  # Deleted after the scan.
        else:
            yield folder, _read_json(folder / "transcript.json", None)


def rename(pid: str, name) -> dict:
    name = _name(name)
    with _gallery() as root:
        people_path = root / "people.json"
        tombstones = _read_json(root / "tombstones.json", {})
        people = _read_json(people_path, [])
        person = _find([p for p in people if p["id"] not in tombstones], pid)
        if any(p != person and p["id"] not in tombstones and p["name"].casefold() == name.casefold()
               for p in people):
            raise meetings.Conflict("A person already has that name.")
        person["name"] = name
        _write_json(people_path, people)
        for row in meetings.list_meetings():
            with _saved_transcript(row["id"]) as (folder, transcript):
                if transcript is None:
                    continue
                matches = [s for s in transcript["speakers"].values() if s.get("person") == pid]
                for speaker in matches:
                    _assign(speaker, person)
                if matches:
                    _render(folder, transcript)
        return person


def forget(pid: str, *, anonymise: bool) -> None:
    with _gallery() as root:
        people_path = root / "people.json"
        people = _read_json(people_path, [])
        tombstone_path = root / "tombstones.json"
        tombstones = _read_json(tombstone_path, {})
        if pid not in tombstones:
            _find(people, pid)
        entry = tombstones.setdefault(pid, {"anonymise": False, "sources": {}})
        entry["anonymise"] |= anonymise
        sources = entry["sources"]
        path = _person_file(root, pid)
        for key in _read_vectors(path):
            meeting_id, label, _ = key.split(":")
            labels = sources.setdefault(meeting_id, [])
            if label not in labels:
                labels.append(label)
        _write_json(tombstone_path, tombstones)
        path.unlink(missing_ok=True)
        _write_json(people_path, [p for p in people if p["id"] != pid])

        for row in meetings.list_meetings():
            meeting_id = row["id"]
            with _saved_transcript(meeting_id) as (folder, transcript):
                if transcript is None:
                    continue
                labels = set(sources.get(meeting_id, []))
                labels.update(label for label, speaker in transcript["speakers"].items()
                              if speaker.get("person") == pid)
                if not labels:
                    continue
                sources[meeting_id] = sorted(labels)
                _write_json(tombstone_path, tombstones)
                archive = folder / "speakers.npz"
                vectors = _read_vectors(archive)
                for label in labels:
                    vectors.pop(label, None)
                    speaker = transcript["speakers"].get(label)
                    if speaker is not None:
                        speaker["clips"] = []
                        if entry["anonymise"] and speaker.get("person") in (None, pid):
                            speaker.pop("person", None)
                            speaker["name"] = f"Speaker {label}"
                _save_vectors(archive, vectors)
                _render(folder, transcript)


def delete_meeting(meeting_id: str) -> None:
    with _gallery() as root, meetings._locked(meeting_id) as (folder, _):
        for person in _read_json(root / "people.json", []):
            path = _person_file(root, person["id"])
            rows = _read_vectors(path)
            retained = {key: vector for key, vector in rows.items() if not key.startswith(meeting_id + ":")}
            if len(retained) != len(rows):
                _save_vectors(path, retained)
        meetings._delete_folder(folder)


def command(args) -> int:
    try:
        if args.action == "list":
            rows = list_people()
            if args.json:
                print(json.dumps({"people": rows}))
            else:
                for row in rows:
                    print(f"{row['id']}  {row['name']}  {row['count']}")
            return 0
        if args.action == "rename":
            result = rename(args.pid, args.name)
            message = result["name"]
        else:
            forget(args.pid, anonymise=args.anonymise)
            result = {"id": args.pid, "deleted": True}
            message = f"Forgot {args.pid}."
    except (meetings.Error, OSError) as error:
        if args.json:
            print(json.dumps({"ok": False, "error": str(error)}))
        else:
            print(error, file=sys.stderr)
        return 1
    print(json.dumps(result) if args.json else message)
    return 0
