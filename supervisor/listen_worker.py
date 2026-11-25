"""Process the meeting queue serially from the dedicated Listen venv."""
from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import tempfile
import time
from importlib.metadata import version
from pathlib import Path

from . import core, listen_pipeline, meetings
from .core import knob
from .version import record_service

DIARISATION = "pyannote/speaker-diarization-community-1"
ASR = "mlx-community/whisper-large-v3-turbo"
POLL_SECONDS = 3
LANGUAGES, _ = knob(
    "SHAFT_LISTEN_LANGUAGES", "it,en", lambda s: tuple(x.strip().lower() for x in s.split(",") if x.strip()),
    "the languages meetings are spoken in, comma-separated (Whisper codes): a turn heard as another language is "
    "transcribed again in each of these and the likelier kept", group="Meetings", services=("listen",), web=False)
LOG = logging.getLogger(__name__)


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
        return [(turn.start, turn.end, label) for turn, label in result.exclusive_speaker_diarization]

    def transcribe(self, wav: Path, start: float, end: float) -> dict:
        import mlx_whisper
        import soundfile

        first, last = (round(at * listen_pipeline.SAMPLE_RATE) for at in (start, end))
        with soundfile.SoundFile(wav) as source:
            source.seek(min(first, len(source)))
            samples = source.read(last - first, dtype="float32")
        if not len(samples):
            raise ValueError("A diarised turn lies outside the decoded audio.")
        result = mlx_whisper.transcribe(samples, path_or_hf_repo=ASR, word_timestamps=True, verbose=None)
        if LANGUAGES and result["language"] not in LANGUAGES:
            # Short turns are often heard as a third language (a spoken "Allora." as Portuguese): ask in each of the
            # meeting's languages and keep the likeliest reading.
            tries = [mlx_whisper.transcribe(samples, path_or_hf_repo=ASR, word_timestamps=True, verbose=None,
                                            language=code) for code in LANGUAGES]
            result = max(tries, key=_likelihood)
        offset = first / listen_pipeline.SAMPLE_RATE
        words = [{"w": word["word"].strip(), "start": offset + word["start"], "end": offset + word["end"]}
                 for segment in result["segments"] for word in segment.get("words", [])]
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
            with tempfile.TemporaryDirectory(dir=meetings._root(), prefix=".listen-work-") as work:
                transcript = listen_pipeline.transcribe_meeting(
                    row, folder / f"audio.{row['ext']}", Path(work), sidecar, engines)
            meetings.complete_processing(meeting_id, transcript, listen_pipeline.render_markdown(transcript))
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
        record_service("listen", {"diarisation": DIARISATION, "asr": ASR})
        LOG.info("Listen started, polling every %ss at background priority with %s and %s",
                 POLL_SECONDS, DIARISATION, ASR)
        engines = Engines()
        while True:
            if not process_next(engines):
                time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
