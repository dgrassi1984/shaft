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
    """The words the ASR is prompted with: the meeting's invitees, the gallery's people and SHAFT_LISTEN_VOCABULARY."""
    terms = list(dict.fromkeys([*people, *speakers.known_names(), *VOCABULARY]))
    return ", ".join(terms) + "." if terms else ""


def calendar_event(row: dict) -> dict | None:
    """The calendar event the recording overlaps most (timed, not declined) on the primary calendar of the account the
    meeting is filed under, or of every logged-in account when it is not filed yet; None when there is none. A
    calendar that cannot be read is logged and the meeting keeps its own title."""
    if not row.get("ended"):
        return None
    started, ended = (datetime.fromisoformat(row[key]) for key in ("started", "ended"))

    def overlap(event):
        if gcal.all_day(event) or gcal.own_response(event) == "declined":
            return 0.0
        start, end = (datetime.fromisoformat(event[key]["dateTime"]) for key in ("start", "end"))
        return (min(end, ended) - max(start, started)).total_seconds()

    timed = []
    for alias in [row["account"]] if row.get("account") else sorted(google.accounts()):
        try:
            account = google.Account(alias, core.secrets_env()).require(google.CALENDAR)
            items, _ = gcal.events(account, "primary", started, ended)
        except (google.AccountError, google.ApiError, OSError) as error:
            LOG.warning("%s: calendar of %s not read for the title: %s", row["id"], alias, error)
            continue
        timed += [event for event in items if event.get("summary")
                  and (event.get("start") or {}).get("dateTime") and (event.get("end") or {}).get("dateTime")]
    best = max(timed, key=overlap, default=None)
    return best if best is not None and overlap(best) > 0 else None


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
            event = calendar_event(row)
            if event and row.get("title_auto"):
                meetings.name_from_calendar(meeting_id, event["summary"].strip())
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
