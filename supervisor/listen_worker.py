"""Process the meeting queue serially from the dedicated Listen venv."""
from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from importlib.metadata import version
from pathlib import Path

from . import calendar as gcal
from . import core, google, listen_pipeline, meetings, speakers
from .core import knob
from .version import record_service

DIARISATION = "pyannote/speaker-diarization-community-1"
ASR = "mlx-community/whisper-large-v3-turbo"
POLL_SECONDS = 3
LANGUAGES, _ = knob(
    "SHAFT_LISTEN_LANGUAGES", "it,en", lambda s: tuple(x.strip().lower() for x in s.split(",") if x.strip()),
    "the languages meetings are spoken in, comma-separated (Whisper codes): a chunk heard as another language is "
    "transcribed again in each of these and the likelier kept", group="Meetings", services=("listen",), web=False)
VOCABULARY, _ = knob(
    "SHAFT_LISTEN_VOCABULARY", "", lambda s: tuple(x.strip() for x in s.split(",") if x.strip()),
    "names and terms meetings use that speech recognition should spell as written (product names, jargon, "
    "places), comma-separated; the speaker gallery's names are added on their own", group="Meetings",
    services=("listen",), web=False)
LOG = logging.getLogger(__name__)


def hints(people=()) -> str:
    """The words the ASR is prompted with: the meeting's invitees, the gallery's people and SHAFT_LISTEN_VOCABULARY.
    Each is one line, without Whisper's special-token marks (an invitee's name is text someone else wrote)."""
    clean = (" ".join(term.replace("<|", "").replace("|>", "").split()) for term in
             [*people, *speakers.known_names(), *VOCABULARY])
    terms = list(dict.fromkeys(term for term in clean if term))
    return ", ".join(terms) + "." if terms else ""


def _when(point: dict) -> datetime | None:
    """A timed event's start or end as an aware datetime (an offset, else its named time zone), or None."""
    try:
        moment = datetime.fromisoformat(point["dateTime"])
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=ZoneInfo(point["timeZone"]))
        return moment
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        return None


def calendar_event(row: dict) -> tuple[dict | None, str | None]:
    """The calendar event the recording overlaps most (timed, not declined) on the primary calendar of the account the
    meeting is filed under, or of every logged-in account when it is not filed yet, with the account it came from;
    (None, None) when there is none. The title is an extra: a calendar that cannot be read is logged, never fails the
    meeting, and the meeting keeps its own title."""
    if not row.get("ended"):
        return None, None
    started, ended = (datetime.fromisoformat(row[key]) for key in ("started", "ended"))
    found = []
    for alias in [row["account"]] if row.get("account") else sorted(google.accounts()):
        try:
            account = google.Account(alias, core.secrets_env()).require(google.CALENDAR)
            items, _ = gcal.events(account, "primary", started, ended)
        except Exception as error:  # noqa: BLE001 -- any failure here only costs the title
            LOG.warning("%s: calendar of %s not read for the title: %s: %s", row["id"], alias,
                        type(error).__name__, error)
            continue
        for event in items:
            start, end = _when(event.get("start") or {}), _when(event.get("end") or {})
            if start and end and gcal.own_response(event) != "declined":
                found.append(((min(end, ended) - max(start, started)).total_seconds(), alias, event))
    seconds, alias, event = max(found, key=lambda item: item[0], default=(0, None, None))
    return (event, alias) if seconds > 0 else (None, None)


class Engines:
    def __init__(self):
        self.pipeline = None

    @property
    def processed_with(self) -> dict:
        return {"diarisation": DIARISATION, "asr": ASR,
                "versions": {name: version(name) for name in ("pyannote.audio", "mlx-whisper", "soundfile", "numpy")}}

    def diarise(self, wav: Path):
        import soundfile
        import torch
        from pyannote.audio import Pipeline

        if self.pipeline is None:
            token = core.secrets_env().get("HF_TOKEN")
            if not token:
                raise ValueError("Set the supervisor's HF_TOKEN to load the gated diarisation model.")
            try:
                pipeline = Pipeline.from_pretrained(DIARISATION, token=token)
            except Exception as error:
                raise ValueError(f"Could not load the diarisation model: {str(error).replace(token, '[redacted]')}") from None
            if pipeline is None:
                raise ValueError("Could not load the diarisation model. Check HF_TOKEN and accept the model conditions.")
            if torch.backends.mps.is_available():
                pipeline.to(torch.device("mps"))
            self.pipeline = pipeline
        samples, rate = soundfile.read(wav, dtype="float32")
        if not len(samples):
            raise ValueError("Audio decode yielded no samples.")
        result = self.pipeline({"waveform": torch.from_numpy(samples[None, :]), "sample_rate": rate})
        turns = listen_pipeline.DiarisationTurns(
            [(turn.start, turn.end, label) for turn, label in result.exclusive_speaker_diarization],
            [(turn.start, turn.end, label) for turn, label in result.speaker_diarization],
        )
        labels = result.speaker_diarization.labels()
        vectors = result.speaker_embeddings if labels else ()
        return turns, dict(zip(labels, vectors, strict=True))

    def loudness(self, wav: Path, spans) -> list[float]:
        """Root-mean-square level of each (start, end) span of a channel."""
        import numpy
        import soundfile

        samples, rate = soundfile.read(wav, dtype="float32")
        levels = []
        for start, end in spans:
            piece = samples[round(start * rate):round(end * rate)]
            levels.append(float(numpy.sqrt(numpy.mean(piece * piece))) if len(piece) else 0.0)
        return levels

    def mix(self, wavs, weights, out: Path) -> None:
        """Sum the channels, each scaled by its weight, peak-normalised, into one mono file."""
        import numpy
        import soundfile

        channels = [soundfile.read(wav, dtype="float32") for wav in wavs]
        length = min(len(samples) for samples, _ in channels)
        mixed = sum(samples[:length] * weight for (samples, _), weight in zip(channels, weights, strict=True))
        peak = float(numpy.abs(mixed).max()) if length else 0.0
        soundfile.write(out, (mixed * (0.9 / peak) if peak else mixed).astype("float32"), channels[0][1],
                        subtype="PCM_16")

    def transcribe(self, wav: Path, start: float, end: float, prompt: str | None = None) -> dict:
        import mlx_whisper
        import soundfile

        first, last = (round(at * listen_pipeline.SAMPLE_RATE) for at in (start, end))
        with soundfile.SoundFile(wav) as source:
            source.seek(min(first, len(source)))
            samples = source.read(last - first, dtype="float32")
        if not len(samples):
            raise ValueError("A speech chunk lies outside the decoded audio.")
        options = {"path_or_hf_repo": ASR, "word_timestamps": True, "verbose": None, "initial_prompt": prompt,
                   "condition_on_previous_text": False}
        result = mlx_whisper.transcribe(samples, **options)
        if LANGUAGES and result["language"] not in LANGUAGES:
            # A chunk is sometimes heard as a third language (a spoken "Allora." as Portuguese): ask in each of the
            # meeting's languages and keep the likeliest reading.
            tries = [mlx_whisper.transcribe(samples, **options, language=code) for code in LANGUAGES]
            result = max(tries, key=_likelihood)
        offset = first / listen_pipeline.SAMPLE_RATE
        # "raw" keeps Whisper's own spacing ("c'" + "è" is one word on the page); the transcript stores "w".
        words = [{"w": word["word"].strip(), "raw": word["word"], "start": offset + word["start"],
                  "end": offset + word["end"]} for segment in result["segments"] for word in segment.get("words", [])]
        return {"text": result["text"], "lang": result["language"], "words": words}


def _likelihood(result: dict) -> float:
    """Mean log-probability over the segments, weighted by their length; no segment is the least likely."""
    segments = result.get("segments") or []
    total = sum(max(s["end"] - s["start"], 0.01) for s in segments)
    return sum(s.get("avg_logprob", -10) * max(s["end"] - s["start"], 0.01) for s in segments) / total if total else -99


def recover() -> None:
    for row in meetings.list_meetings():
        if row["state"] == "processing":
            try:
                meetings.recover_processing(row["id"])
            except meetings.Missing:
                continue  # Deleted after the scan.


def recorder_problems(folder: Path) -> list[str]:
    """The problems the recording device wrote in the sidecar, or none when it is missing or unreadable."""
    try:
        problems = json.loads((folder / "sidecar.json").read_bytes()).get("problems")
    except (OSError, ValueError, AttributeError, RecursionError):
        return []
    return [str(p) for p in problems if str(p).strip()] if isinstance(problems, list) else []


def process_next(engines) -> bool:
    for row in reversed(meetings.list_meetings()):
        if row["state"] != "queued":
            continue
        meeting_id = row["id"]
        try:
            meetings.set_state(meeting_id, "processing")
            LOG.info("%s processing", meeting_id)
            folder = meetings._root() / meeting_id
            sidecar = json.loads((folder / "sidecar.json").read_bytes())
            event, alias = calendar_event(row)
            title = " ".join(str((event or {}).get("summary") or "").split())
            if title and row.get("title_auto"):
                meetings.name_from_calendar(meeting_id, title, row.get("account"))
            people = [person["displayName"] for person in (event or {}).get("attendees") or []
                      if person.get("displayName") and not person.get("resource")]
            with tempfile.TemporaryDirectory(dir=meetings._root(), prefix=".listen-work-") as work:
                transcript = listen_pipeline.transcribe_meeting(
                    row, folder / f"audio.{row['ext']}", Path(work), sidecar, engines, hints(people))
            speakers.complete_processing(meeting_id, transcript, transcript.pop("_embeddings"))
            LOG.info("%s ready", meeting_id)
        except meetings.Missing:
            return True
        except Exception as error:
            message = str(error) or type(error).__name__
            problems = recorder_problems(meetings._root() / meeting_id)
            if problems:  # what the recording device said first: ffmpeg only sees the file it left (bug 2026-11-27)
                message = "The recording device reported: " + "; ".join(problems) + ". " + message
            try:
                meetings.set_state(meeting_id, "failed", error=message)
            except meetings.Missing:
                return True
            LOG.error("%s failed: %s", meeting_id, message)
        return True
    return False


def main() -> int:
    logging.basicConfig(format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    LOG.setLevel(logging.INFO)
    os.setpriority(os.PRIO_PROCESS, 0, max(10, os.getpriority(os.PRIO_PROCESS, 0)))
    os.environ["PYANNOTE_METRICS_ENABLED"] = "0"
    root = meetings._root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / ".listen.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.error("A Listen worker is already running.")
            return 1
        for work in root.glob(".listen-work-*"):
            shutil.rmtree(work)
        recover()
        meetings.deliver_owed()
        record_service("listen", {"diarisation": DIARISATION, "asr": ASR})
        LOG.info("Listen started, polling every %ss at background priority with %s and %s",
                 POLL_SECONDS, DIARISATION, ASR)
        engines = Engines()
        while True:
            if not process_next(engines):
                time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
