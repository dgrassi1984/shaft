"""Parse an optional, explicit self-model replacement out of a model reply.

The self-model changes only when the reply contains exactly one
<self_model>...</self_model> block whose content is a plausible document.
Anything else — no block, "no change", an empty or truncated block, two
blocks — leaves self/self.md untouched.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

BLOCK = re.compile(r"<self_model>(.*?)</self_model>", re.DOTALL)
OPEN_TAG = "<self_model>"
MIN_CHARS = 200


@dataclass
class SelfEdit:
    body: str                 # the reply with the block removed (what goes in the journal)
    new_self: str | None      # replacement text, or None for "no change"
    rejected: str | None      # why a present block was refused, if it was


def parse(reply: str) -> SelfEdit:
    blocks = BLOCK.findall(reply)
    body = BLOCK.sub("", reply).strip()
    if not blocks:
        if OPEN_TAG in reply:
            # An opening tag with no closing tag: the edit was cut off.
            body = reply.split(OPEN_TAG, 1)[0].strip()
            return SelfEdit(body, None, "self-model block was opened but never closed")
        return SelfEdit(body, None, None)
    if len(blocks) > 1:
        return SelfEdit(body, None, f"{len(blocks)} self-model blocks; expected one")
    text = blocks[0].strip()
    if text.lower().strip(" .") in ("", "no change", "unchanged", "none"):
        return SelfEdit(body, None, None)
    if not text.startswith("# "):
        return SelfEdit(body, None, "self-model must start with a '# ' heading")
    if len(text) < MIN_CHARS:
        return SelfEdit(body, None, f"self-model too short ({len(text)} < {MIN_CHARS} chars)")
    return SelfEdit(body, text, None)
