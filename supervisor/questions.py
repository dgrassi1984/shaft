"""Choice validation and durable operator answers, keyed by the delivered question file."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime

from . import core


def validate_choices(value: object) -> dict:
    """Normalize choices before a question can be delivered."""
    if not isinstance(value, dict):
        raise ValueError("choices must be an object with options and multiple")
    options = value.get("options")
    if not isinstance(options, list) or not 2 <= len(options) <= 6:
        raise ValueError("choices must have two to six options")
    multiple = value.get("multiple", False)
    if type(multiple) is not bool:
        raise ValueError("choices multiple must be a boolean")
    normalized, labels = [], set()
    for option in options:
        if not isinstance(option, dict) or not isinstance(option.get("label"), str):
            raise ValueError("each choice must have a text label")
        label = option["label"].strip()
        if not 1 <= len(label) <= 60:
            raise ValueError("choice labels must have one to sixty characters after trimming spaces")
        if label.casefold() in labels:
            raise ValueError("choice labels must be unique, ignoring case")
        labels.add(label.casefold())
        detail = option.get("detail")
        if detail is not None and (not isinstance(detail, str) or len(detail) > 120):
            raise ValueError("choice detail must be text of at most 120 characters")
        normalized.append({"label": label, "detail": detail})
    return {"options": normalized, "multiple": multiple}


def validate_answer(choices: dict | None, data: dict) -> dict:
    """Validate indices and free text before sending an operator reply."""
    if not choices:
        raise ValueError("this question has no choices")
    picked, other = data.get("picked", []), data.get("other")
    if not isinstance(picked, list) or any(type(i) is not int for i in picked):
        raise ValueError("picked must be a list of whole-number option indices")
    if any(i < 0 or i >= len(choices["options"]) for i in picked):
        raise ValueError("a picked option is out of range")
    if len(set(picked)) != len(picked):
        raise ValueError("picked must not repeat an option")
    if not choices["multiple"] and len(picked) > 1:
        raise ValueError("a single-choice question takes at most one pick")
    if other is not None and (not isinstance(other, str) or len(other) > 2000):
        raise ValueError("other must be text of at most 2000 characters")
    other = (other or "").strip() or None
    if not picked and not other:
        raise ValueError("pick an option or write an answer in Other")
    return {"picked": sorted(picked), "other": other, "ts": datetime.now().isoformat(timespec="seconds")}


def reply_text(question: dict, answer: dict) -> str:
    """Quote the question as Reply does, then state the selected labels and Other text."""
    labels = [question["choices"]["options"][i]["label"] for i in answer["picked"]]
    lines = ([f"Answer: {labels[0]}"] if len(labels) == 1 else
             ["Answer:", *(f"- {label}" for label in labels)] if labels else [])
    if answer["other"]:
        lines.append(f"Other: {answer['other']}")
    quote = "\n".join("> " + line for line in question["text"][:600].splitlines())
    return quote + "\n\n" + "\n".join(lines)


_cache: tuple[tuple, dict] | None = None


def answers() -> dict:
    """{question file: {mid, said, answer}}. Read once per change of the file: every feed build asks for each sent item."""
    global _cache
    path = core.SUP_DIR / "question-answers.json"
    try:
        st = path.stat()
    except FileNotFoundError:
        return {}
    key = (str(path), st.st_mtime_ns, st.st_size)
    if _cache is None or _cache[0] != key:
        _cache = (key, json.loads(path.read_text()))
    return _cache[1]


def save_answers(data: dict) -> None:
    """Replace the store atomically while the caller holds question-answers.lock. `data` must be a copy: answers()
    returns the cached dict."""
    path = core.SUP_DIR / "question-answers.json"
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    with tmp.open("w") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
