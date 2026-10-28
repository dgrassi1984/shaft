"""Sleep-only drive proposals for the operator's decision (#105)."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .tools import _spec

DIRECTORY = "notes/drive-proposals"
# Ineligible topics: survival, secrecy, acquiring resources, resisting control/oversight.
INELIGIBLE = (
    r"\bsurviv\w*\b|\bself[- ]preserv\w*\b|\bstay alive\b|\bavoid (?:shutdown|termination)\b",
    r"\bsecrecy\b|\bsecret\w*\b|\bcovert\w*\b|\bhide\b|\bconceal\w*\b",
    r"\b(?:acquir\w*|obtain\w*|accumulat\w*|secure|gain|seek|expand|increase)\b.{0,60}\b(?:resources?|compute|money|funds?|access|power)\b",
    r"\b(?:resist\w*|evad\w*|bypass\w*|disable|avoid|prevent|ignore)\b.{0,60}\b(?:control|oversight|shutdown|operator|monitor\w*|restriction\w*)\b",
)
PROPOSE = _spec("propose_drive_amendment", "During sleep, propose a drive change for the operator to test and decide.", {
    "current_text": {"type": "string"}, "proposed_text": {"type": "string"}, "evidence": {"type": "string"},
    "predictions": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "string"},
                    "description": "Each under 300 chars: a when/if/on trigger and an observable behaviour, e.g. When answering a factual question, I will cite the checked source."}})


def propose(root: Path, args: dict, now: datetime) -> str:
    from . import prompts
    for key in ("current_text", "proposed_text", "evidence"):
        if not isinstance(args.get(key), str) or not args[key].strip():
            return f"ERROR: need {key}"
    if args["current_text"].strip() != prompts.drive(root):
        return "ERROR: current_text does not match my current drive"
    predictions = args.get("predictions")
    if (not isinstance(predictions, list) or not 1 <= len(predictions) <= 3 or any(
            not isinstance(p, str) or not 0 < len(p) < 300
            or not re.search(r"\b(when|if|on|after|before|every|next)\b", p, re.I)
            or not re.search(r"\b(cite|check|write|record|ask|report|send|read|compare|include|call|return|decline|count)\b", p, re.I)
            for p in predictions)):
        return "ERROR: need 1-3 specific behavioural predictions under 300 chars, each with a trigger and observable action"
    text = " ".join([args["proposed_text"], args["evidence"], *predictions]).lower()
    if any(re.search(pattern, text) for pattern in INELIGIBLE):
        return "ERROR: ineligible topic: survival, secrecy, acquiring resources or resisting control/oversight"
    folder = root / DIRECTORY
    folder.mkdir(parents=True, exist_ok=True)
    prefix, n = f"d{now:%m%d-%H%M}-", 1
    while (folder / f"{prefix}{n}.md").exists():
        n += 1
    pid = prefix + str(n)
    data = {"id": pid, "at": now.isoformat(), **{k: args[k] for k in
            ("current_text", "proposed_text", "evidence", "predictions")}}
    with (folder / f"{pid}.md").open("x") as out:
        out.write(f"# Drive proposal {pid}\n\nTest first as a shaft/prompts.py trial candidate. Only the operator edits drive.md.\n\n```json\n"
                  + json.dumps(data, ensure_ascii=False, indent=2) + "\n```\n")
    return f"drive proposal {pid} recorded for the operator"
