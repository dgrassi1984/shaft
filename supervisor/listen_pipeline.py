"""Prepare meeting audio and assemble a transcript using injected speech engines."""
from __future__ import annotations

import subprocess
from difflib import SequenceMatcher
from pathlib import Path

SAMPLE_RATE = 16_000


def decode(audio: Path, work: Path, mode: str) -> list[Path]:
    """Decode the first audio stream to 16 kHz mono PCM for each meeting channel."""
    command = ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-abort_on", "empty_output_stream", "-i", str(audio)]
    paths = []
    for index in range(2 if mode == "online" else 1):
        path = work / f"ch{index + 1}.wav"
        paths.append(path)
        command += ["-map", "0:a:0"]
        if mode == "online":
            command += ["-af", f"channelmap=map={index}:channel_layout=mono"]
        command += ["-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(path)]
    try:
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise ValueError("Audio decode failed: ffmpeg is not installed.") from None
    if result.returncode:
        tail = result.stderr[-2000:].decode(errors="replace").strip()
        raise ValueError(f"Audio decode failed (ffmpeg exit {result.returncode}): {tail}")
    return paths


def merge_turns(turns):
    merged = []
    for start, end, label in turns:
        if merged and label == merged[-1][2] and start < merged[-1][1] + 1:
            previous, previous_end, _ = merged[-1]
            merged[-1] = (previous, max(end, previous_end), label)
        else:
            merged.append((start, end, label))
    return [(start, end, label) for start, end, label in merged if end >= start + 0.4]


def speech_seconds(intervals) -> float:
    """Count time with detected speech once, including overlapping channels or speakers."""
    total = last_end = 0.0
    for start, end in sorted(intervals):
        total += max(0, end - max(start, last_end))
        last_end = max(end, last_end)
    return total


def drop_bleed(segments):
    system = [row for row in segments if row["channel"] == 2]

    def echoed(mic):
        words = mic["text"].lower().split()
        minimum = (mic["end"] - mic["start"]) / 2
        for row in system:
            overlap = min(mic["end"], row["end"]) - max(mic["start"], row["start"])
            if overlap >= minimum and SequenceMatcher(
                None, words, row["text"].lower().split(), autojunk=False
            ).ratio() >= 0.6:
                return True
        return False

    return [row for row in segments if row["channel"] == 2 or not echoed(row)]


def transcribe_meeting(meeting: dict, audio: Path, work: Path, sidecar: dict, engines) -> dict:
    """Engine word timestamps use seconds from the start of the recording."""
    segments = []
    intervals = []
    for channel, wav in enumerate(decode(audio, work, meeting["mode"]), 1):
        labels = {}
        prefix = "M" if channel == 1 else "S"
        turns = sorted(engines.diarise(wav))
        intervals.extend((start, end) for start, end, _ in turns)
        for start, end, label in merge_turns(turns):
            speaker = labels.setdefault(label, f"{prefix}{len(labels) + 1}")
            result = engines.transcribe(wav, start, end)
            if text := result["text"].strip():
                segments.append({"start": start, "end": end, "channel": channel, "speaker": speaker,
                                 "lang": result["lang"], "text": text, "words": result["words"]})

    speech = speech_seconds(intervals)
    if not speech:
        raise ValueError("Diarisation detected no speech.")
    if meeting["mode"] == "online":
        segments = drop_bleed(segments)
    words = sum(len(row["text"].split()) for row in segments)
    if speech >= 10 and words * 30 < speech:
        raise ValueError(f"ASR word count was {words} for {speech:.1f} seconds of detected speech "
                         "(expected at least 1 word per 30 seconds).")
    if not words:
        raise ValueError("ASR produced no words for the detected speech.")

    speakers = {}
    for row in segments:
        speaker = speakers.setdefault(row["speaker"], {"channel": row["channel"], "seconds": 0.0})
        speaker["seconds"] += row["end"] - row["start"]
    return {"meeting": meeting["id"], "segments": sorted(segments, key=lambda row: row["start"]),
            "gaps": sidecar.get("gaps", []), "speakers": speakers, "processed_with": engines.processed_with}


def _clock(seconds) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def render_markdown(transcript: dict) -> str:
    lines = [(row["start"], f'{_clock(row["start"])} {row["speaker"]}: {" ".join(row["text"].splitlines())}')
             for row in transcript["segments"]]
    lines.extend((gap["start"], f'[gap {_clock(gap["start"])}-{_clock(gap["end"])}: {gap["reason"]}]')
                 for gap in transcript["gaps"])
    return "".join(line + "\n" for _, line in sorted(lines, key=lambda item: item[0]))
