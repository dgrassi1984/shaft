"""Prepare meeting audio and assemble a transcript using injected speech engines."""
from __future__ import annotations

import math
import subprocess
from collections import defaultdict
from pathlib import Path

SAMPLE_RATE = 16_000


class DiarisationTurns(list):
    """Exclusive turns for ASR, with the original turns for clip selection."""

    def __init__(self, exclusive, original):
        super().__init__(exclusive)
        self.original = original


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


def speech_seconds(intervals) -> float:
    """Count time with detected speech once, including overlapping channels or speakers."""
    total = last_end = 0.0
    for start, end in sorted(intervals):
        total += max(0, end - max(start, last_end))
        last_end = max(end, last_end)
    return total


def _clean_turns(turns):
    """Keep intervals with exactly one active label, including in overlapped speech."""
    events = defaultdict(list)
    for start, end, label in turns:
        events[start].append((label, 1))
        events[end].append((label, -1))
    active = {}
    previous = 0
    for at, changes in sorted(events.items()):
        if len(active) == 1 and at > previous:
            yield previous, at, next(iter(active))
        for label, delta in changes:
            active[label] = active.get(label, 0) + delta
            if not active[label]:
                del active[label]
        previous = at


def longest_clips(ranges):
    return [[start, min(end, start + 10)] for start, end in
            sorted(ranges, key=lambda span: (span[0] - span[1], span[0]))[:3] if end - start >= 5]


# Whisper decodes 30-second windows. Speech is cut into chunks that fit one window, at pauses, so every chunk carries
# its context (both sides of the conversation) and no turn is decoded alone: a sub-second turn padded to 30 s with
# silence is where Whisper invents "Thank you" or "Bye-bye".
CHUNK_SECONDS = 28.0
PAD_SECONDS = 0.3
CONTEXT_CHARS = 300
# A speaker change inside a phrase costs this much evidence; at a pause of PAUSE_SECONDS or more it is free.
SWITCH_COST = 2.0
PAUSE_SECONDS = 0.25
SEGMENT_GAP_SECONDS = 1.5


def chunk_spans(turns, limit: float = CHUNK_SECONDS, pad: float = PAD_SECONDS):
    """Speech regions from every channel's turns, padded, joined, and packed into chunks of at most `limit` seconds
    that start and end in a pause (a stretch with no pause is cut at the limit)."""
    regions = []
    for start, end in sorted((max(0.0, start - pad), end + pad) for start, end, *_ in turns):
        if regions and start <= regions[-1][1]:
            regions[-1][1] = max(regions[-1][1], end)
        else:
            regions.append([start, end])
    chunks = []
    for start, end in regions:
        while end - start > limit:
            chunks.append([start, start + limit])
            start += limit
        if chunks and end - chunks[-1][0] <= limit:
            chunks[-1][1] = end
        else:
            chunks.append([start, end])
    return [tuple(chunk) for chunk in chunks]


def _coverage(start, end, spans) -> float:
    covered = sum(max(0.0, min(end, span_end) - max(start, span_start)) for span_start, span_end in spans)
    return min(covered / max(end - start, 0.05), 1.0)


def attribute(words, speakers, evidence):
    """The likeliest speaker for each word: `evidence(word_index, speaker)` scores each, and a change of speaker
    inside a phrase costs SWITCH_COST (Viterbi), so a single word is not flipped by a stray timestamp."""
    if not words:
        return []
    score = {speaker: evidence(0, speaker) for speaker in speakers}
    back = []
    for index in range(1, len(words)):
        cost = SWITCH_COST if words[index]["start"] - words[index - 1]["end"] < PAUSE_SECONDS else 0.0
        step, came = {}, {}
        for speaker in speakers:
            best = max(speakers, key=lambda other: score[other] - (cost if other != speaker else 0))
            step[speaker] = score[best] - (cost if best != speaker else 0) + evidence(index, speaker)
            came[speaker] = best
        score = step
        back.append(came)
    speaker = max(speakers, key=score.get)
    path = [speaker]
    for came in reversed(back):
        speaker = came[speaker]
        path.append(speaker)
    return path[::-1]


def transcribe_meeting(meeting: dict, audio: Path, work: Path, sidecar: dict, engines, hints: str = "") -> dict:
    """Diarise each channel, transcribe the meeting as one conversation, and give each word to its speaker.

    Engine word timestamps use seconds from the start of the recording. `hints` (names and terms) prompt the ASR."""
    wavs = decode(audio, work, meeting["mode"])
    turns = []  # (start, end, speaker, channel)
    intervals = []
    embeddings = {}
    clean = defaultdict(list)
    for channel, wav in enumerate(wavs, 1):
        labels = {}
        prefix = "M" if channel == 1 else "S"
        diarised, vectors = engines.diarise(wav)
        original = getattr(diarised, "original", diarised)
        for start, end, label in sorted(diarised):
            if end > start:
                turns.append((start, end, labels.setdefault(label, f"{prefix}{len(labels) + 1}"), channel))
                intervals.append((start, end))
        embeddings.update({speaker: vectors[label] for label, speaker in labels.items() if label in vectors})
        for start, end, label in _clean_turns(original):
            if label in labels:
                clean[labels[label]].append((start, end))

    speech = speech_seconds(intervals)
    if not speech:
        raise ValueError("Diarisation detected no speech.")

    channel_of = {speaker: channel for _, _, speaker, channel in turns}
    spans = defaultdict(list)
    for start, end, speaker, _ in turns:
        spans[speaker].append((start, end))
    if len(wavs) > 1:
        # Bring each channel's speech to the same loudness before mixing, then use loudness again to tell channels
        # apart: a word is louder, against that channel's own speech, on the channel it was spoken into.
        levels = {channel: _median(engines.loudness(wav, [(s, e) for s, e, _, c in turns if c == channel]))
                  for channel, wav in enumerate(wavs, 1)}
        source = work / "mix.wav"
        engines.mix(wavs, [1 / levels[channel] if levels[channel] else 0.0 for channel in range(1, len(wavs) + 1)],
                    source)
    else:
        source = wavs[0]

    words = []
    context = ""
    for start, end in chunk_spans(turns):
        prompt = " ".join(part for part in (hints.strip(), context[-CONTEXT_CHARS:].strip()) if part)
        result = engines.transcribe(source, start, end, prompt or None)
        words.extend({**word, "lang": result["lang"]} for word in result["words"] if word["w"])
        context += " " + result["text"].strip()

    loud = {}
    if len(wavs) > 1:
        for channel, wav in enumerate(wavs, 1):
            values = engines.loudness(wav, [(max(0.0, w["start"] - 0.05), w["end"] + 0.05) for w in words])
            loud[channel] = [math.log(value / levels[channel] + 1e-3) if levels[channel] else -7.0 for value in values]

    def evidence(index, speaker):
        word = words[index]
        energy = loud[channel_of[speaker]][index] if loud else 0.0
        return 2 * _coverage(word["start"], word["end"], spans[speaker]) + energy

    segments = []
    for word, speaker in zip(words, attribute(words, sorted(spans), evidence)):
        row = segments[-1] if segments else None
        entry = {"w": word["w"], "start": word["start"], "end": word["end"]}
        raw = word.get("raw", " " + word["w"])
        if row and row["speaker"] == speaker and word["start"] - row["end"] < SEGMENT_GAP_SECONDS:
            row["words"].append(entry)
            row["raw"].append(raw)
            row["end"] = max(row["end"], word["end"])
        else:
            segments.append({"start": word["start"], "end": word["end"], "channel": channel_of[speaker],
                             "speaker": speaker, "lang": word["lang"], "words": [entry], "raw": [raw]})
    for row in segments:
        row["text"] = "".join(row.pop("raw")).strip()

    count = sum(len(row["words"]) for row in segments)
    if speech >= 10 and count * 30 < speech:
        raise ValueError(f"ASR word count was {count} for {speech:.1f} seconds of detected speech "
                         "(expected at least 1 word per 30 seconds).")
    if not count:
        raise ValueError("ASR produced no words for the detected speech.")

    # Speaking time and voice clips come from the diarised turns of each speaker who said something.
    speakers = {}
    for row in segments:
        if row["speaker"] not in speakers:
            label = row["speaker"]
            speakers[label] = {"channel": row["channel"], "seconds": sum(end - start for start, end in spans[label]),
                               "clips": longest_clips(clean[label])}
    return {"meeting": meeting["id"], "segments": segments,
            "gaps": sidecar.get("gaps", []), "speakers": speakers, "processed_with": engines.processed_with,
            "_embeddings": {label: embeddings[label] for label in speakers if label in embeddings}}


def _median(values) -> float:
    values = sorted(values)
    if not values:
        return 0.0
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def _clock(seconds) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def render_markdown(transcript: dict) -> str:
    lines = []
    for row in transcript["segments"]:
        label = row["speaker"]
        name = transcript["speakers"].get(label, {}).get("name") or f"Speaker {label} (unknown)"
        lines.append((row["start"], f'{_clock(row["start"])} {name}: {" ".join(row["text"].splitlines())}'))
    lines.extend((gap["start"], f'[gap {_clock(gap["start"])}-{_clock(gap["end"])}: {gap["reason"]}]')
                 for gap in transcript["gaps"])
    return "".join(line + "\n" for _, line in sorted(lines, key=lambda item: item[0]))
