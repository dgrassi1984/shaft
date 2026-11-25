"""Commitments measured against the instance's record and the supervisor's judges.

The event fold and evidence checks mirror shaft/commitments.py. Shared fixtures keep them in step;
the supervisor never imports the agent's code.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from . import metrics
from .ledger_mirror import CHECKED
from .core import jsonl_lines, INSTANCES, SUP_DIR, safe_read

FILE = "memory/commitments.jsonl"
STATUSES = ("kept", "broken", "superseded", "void")
FIELD_CHARS = 300
NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
ID = re.compile(r"c\d{4}-\d{4}-[1-9]\d*")
FAILED = ("ERROR", "(repeat: not run)", "(held, not run yet)")
FIELDS = ("promise", "due", "criterion", "made_to")
# Calls that record or report rather than do: commit_to's own "recorded" made any committing cycle its own evidence
# (Grok's review). Evidence is a call that did something.
BOOKKEEPING = ("commit_to", "settle_commitment", "declare_intent", "confirm_confidence", "finish_cycle",
               "close_surprise")


def _at(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _local(at)


def _local(at: datetime) -> datetime:
    return at.astimezone().replace(tzinfo=None) if at.tzinfo else at


def _entry(value) -> dict | None:
    if not isinstance(value, dict) or any(not isinstance(value.get(k), str) for k in FIELDS):
        return None
    item = {k: " ".join(value[k].split()) for k in FIELDS}
    if any(not item[k] for k in FIELDS) or any(len(item[k]) > FIELD_CHARS for k in ("promise", "criterion")):
        return None
    return item if NAME.fullmatch(item["made_to"]) else None


def _rows(text: str) -> list[dict]:
    rows = []
    for line in jsonl_lines(text):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            rows.append(rec)
    return rows


def _fold(rows: list[dict], now: datetime) -> dict[str, dict]:
    # The agent keeps its own copy; shared fixtures pin the event contract.
    items, self_ids = {}, set()
    for rec in rows:
        cid, at = rec.get("id"), _at(rec.get("at"))
        if not isinstance(cid, str) or not ID.fullmatch(cid) or at is None:
            continue
        if rec.get("event") == "made":
            item, due = _entry(rec), _at(rec.get("due"))
            if item is None or due is None or due < at or rec.get("source") not in ("cycle", "talk") or cid in items:
                continue
            items[cid] = {"id": cid, **item, "source": rec["source"], "made_at": rec["at"],
                          "status": "open", "settled_at": None, "evidence": None}
            items[cid].update({k: rec[k] for k in ("talk_id", "message_file", "note") if k in rec})
            if item["made_to"] in ("self", "own") or rec.get("self_commitment"):
                self_ids.add(cid)
        elif rec.get("event") == "settled":
            item, evidence = items.get(cid), rec.get("evidence")
            if (item and item["status"] == "open" and rec.get("status") in STATUSES
                    and not (cid in self_ids and at - _at(item["due"]) >= timedelta(hours=24))
                    and isinstance(evidence, str) and evidence.strip() and at >= _at(item["made_at"])):
                item.update(status=rec["status"], settled_at=rec["at"], evidence=evidence)
    for item in items.values():
        if item["status"] == "open":
            if item["id"] in self_ids and _local(now) - _at(item["due"]) >= timedelta(hours=24):
                expires = _at(item["due"]) + timedelta(hours=24)
                item.update(status="expired", settled_at=expires.isoformat(), evidence="self commitment expired 24h after due")
            elif _at(item["due"]) < _local(now):
                item["status"] = "overdue"
    return items


# What an operator message in an instance's inbox is named: his own (the app, the web, Telegram), or the supervisor's
# watch of his chat or mail. The calendar watch reports events, not requests.
OPERATOR_INBOX = re.compile(r"\d{8}-\d{6}-\d{6}-from-(?:operator|supervisor-(?:gchat|mail)-watch)\.md")
DELIVERED = "delivered to the operator"
CYCLE_SPAN = timedelta(hours=2)   # longer than any live cycle's budget
RELABELLED = "operator source missing or invalid; recorded as self"


def operator_inbox(file) -> bool:
    return isinstance(file, str) and bool(OPERATOR_INBOX.fullmatch(file))


def _cycle_of(lives: list[dict], at) -> dict | None:
    """The live cycle a row made at `at` was made in: the first that ended at or after it (one runs at a time), within
    CYCLE_SPAN; a cycle that wrote no episode is not mistaken for the next one."""
    at = _at(at)
    if at is None:
        return None
    at = at.replace(microsecond=0)   # an episode's ts keeps whole seconds; a row made in its last second is its own
    ep = next((ep for ep in lives if _at(ep["ts"]) >= at), None)
    return ep if ep and _at(ep["ts"]) - at <= CYCLE_SPAN else None


def _cycle_source(ep: dict | None) -> bool:
    """A live cycle answering the operator (#238): an operator message in its inbox, or a message it delivered to
    him. What made a promise there one to the operator before the source was recorded on the row."""
    if not ep:
        return False
    if any(operator_inbox(n) for n in ep.get("inbox") or []):
        return True
    return any(isinstance(c, dict) and c.get("tool") == "send_message" and isinstance(c.get("result"), str)
               and c["result"].startswith(DELIVERED) for c in ep.get("tools") or [])


def _source_rows(rows: list[dict], episodes: list[dict], delivered, own: str, received=None) -> list[dict]:
    """An operator promise needs the conversation it was made in, a message I delivered to the operator (review
    I10), or the operator message in its inbox it answers (#238). `delivered(file)` confirms a delivered message, a
    note in its own folder is not one; `received(file)` confirms an operator message its inbox got. A cycle row with
    none of them (made before #238, when commit_to recorded it as self) is the operator's when that cycle answered
    him: it had his message in its inbox or delivered one to him."""
    talks = {ep["talk_id"] for ep in episodes if ep.get("kind") == "talk" and ep.get("role") == "user"
             and not ep.get("speaker") and isinstance(ep.get("talk_id"), str) and ep["talk_id"]}
    lives = sorted((ep for ep in episodes if ep.get("kind") == "live" and _at(ep.get("ts"))),
                   key=lambda ep: _at(ep["ts"]))
    out = []
    for row in rows:
        row = dict(row)
        if row.pop(CHECKED, False):   # judged when the supervisor copied it (ledger_mirror.py): kept as judged then
            out.append(row)
            continue
        if row.get("event") == "made":
            if row.get("made_to") == "self" and row.get("note") == RELABELLED and row.get("sourced"):
                row.update(made_to="operator")   # written as self by commit_to before #238: judged again below
                del row["note"]
            if row.get("made_to") == "operator" and row.get("sourced"):   # older rows predate the rule: kept as made
                talk, message, inbox = row.get("talk_id"), row.get("message_file"), row.get("inbox_file")
                valid = isinstance(talk, str) and talk in talks
                if not valid and isinstance(message, str) and _message_file(own).fullmatch(message):
                    valid = delivered(message)
                if not valid and operator_inbox(inbox) and received is not None:
                    valid = received(inbox)
                if not valid and row.get("source") == "cycle" and not (talk or message or inbox):
                    valid = _cycle_source(_cycle_of(lives, row.get("at")))
                if not valid:
                    row.update(made_to="self", note=RELABELLED)
            if row.get("made_to") == own:
                row["self_commitment"] = True
        out.append(row)
    return out


def _citation(evidence: str) -> tuple[str, int | None] | None:
    ts, sep, step = evidence.partition("#")
    if sep and (not re.fullmatch(r"[1-9]\d{0,8}", step)):
        return None
    if _at(ts) is None:
        return None
    return ts, int(step) if sep else None


def _matches(ts, cited: str) -> bool:
    return isinstance(ts, str) and (ts == cited or len(cited) == 16 and ts[:16].replace("T", " ") == cited.replace("T", " "))


def _evidence(episodes: list[dict], item: dict, evidence: str, record=None) -> tuple[str, dict | None]:
    """Why the cited evidence does not count ("" when it does) and the cited episode (_checked_evidence)."""
    return _checked_evidence(episodes, item, evidence, record)[:2]


def _checked_evidence(episodes: list[dict], item: dict, evidence: str, record=None) -> tuple[str, dict | None, bool]:
    """Why the cited evidence does not count ("" when it does), the cited episode, and whether a supervisor-tool step
    of it was checked against the supervisor's record. `record` (served.Record): every supervisor-tool step the
    citation covers must have been served and match the fingerprint kept then (#8): a whole-episode citation is not
    carried by its built-in steps past a supervisor-tool step that fails. Built-in and plugin steps cannot be checked
    that way and are read as before; evidence made only of them counts, unchecked."""
    citation = _citation(evidence)
    if citation is None:
        return "cite an episode ts, optionally #step", None, False
    ts, step = citation
    matches = [ep for ep in episodes if _matches(ep.get("ts"), ts)]
    if not matches:
        return "no such episode", None, False
    if len(matches) != 1:
        return "episode citation is ambiguous; use the exact ts", None, False
    ep = matches[0]
    ended, made = _at(ep.get("ts")), _at(item.get("made_at"))
    if ended is None or made is None or ended <= made:
        return "cited episode did not end after my commitment", ep, False
    calls = ep.get("tools")
    calls = calls if isinstance(calls, list) else []
    if step is not None:
        if step > len(calls):
            return "no such tool-log step", ep, False
        calls = [calls[step - 1]]
    good = [c for c in calls if isinstance(c, dict) and isinstance(c.get("tool"), str) and bool(c["tool"])
            and c["tool"] not in BOOKKEEPING
            and isinstance(c.get("result"), str) and not c["result"].startswith(FAILED)]
    if not good:
        return "cited evidence has no successful tool call", ep, False
    if record is None:
        return "", ep, False
    reasons = [record.check(ep, c) for c in good]   # None: not checkable (built-in, plugin, before the record)
    failed = [r for r in reasons if r]
    if failed:
        return failed[0], ep, False
    return "", ep, any(r == "" for r in reasons)


def recorded(name: str, as_of: datetime | None = None) -> tuple[list[dict], dict]:
    """The rows to fold for what the instance promised as the record holds it: the supervisor's own copy of its
    ledger and the rows not copied yet (ledger_mirror.merged), and which recorded ids its file no longer holds as
    they were. A promise or settle removed from the file is still here."""
    from . import ledger_mirror
    current = _rows(safe_read(INSTANCES / name, FILE, "", errors="replace")) if NAME.fullmatch(name) else []
    return ledger_mirror.merged(name, "commitments", current, as_of=as_of)


def ledger(name: str, now: datetime | None = None, rows: list[dict] | None = None) -> dict[str, dict]:
    """The instance's commitments folded and source-checked; `rows`: the events to fold instead of its file's (the
    `promises` gauge folds the supervisor's own copy with them: ledger_mirror.py)."""
    if not NAME.fullmatch(name):
        return {}
    if rows is None:
        rows = _rows(safe_read(INSTANCES / name, FILE, "", errors="replace"))
    rows = _source_rows(rows, _episodes(name), lambda file: bool(operator_message(name, file)), name,
                        _received(name))
    return _fold(rows, now if now is not None else datetime.now())


def _received(name: str):
    """An operator message the supervisor delivered to this instance's inbox, by its own message log (the inbox and
    memory/messages/ folders are the instance's); read once, on the first row that asks."""
    files: set | None = None

    def check(file: str) -> bool:
        nonlocal files
        if files is None:
            from . import messages
            files = {m.get("file") for m in messages.history() if m.get("to") == name
                     and m.get("from") in ("operator", "supervisor (gchat watch)", "supervisor (mail watch)")}
        return file in files
    return check


def _message_file(name: str) -> re.Pattern:
    return re.compile(r"\d{8}-\d{6}-\d{6}-from-" + re.escape(name) + r"\.md")


def operator_message(name: str, file: str) -> str:
    """A message this instance delivered to the operator, still waiting or already read (messages._archive moves it
    to operator-inbox/read/ under the same name), without following links."""
    if not NAME.fullmatch(name) or not _message_file(name).fullmatch(file):
        return ""
    for folder in ("operator-inbox", "operator-inbox/read"):
        text = safe_read(SUP_DIR, f"{folder}/{file}", "", errors="replace")
        head = text.split("\n\n", 1)[0]
        if re.search(r"(?m)^To: operator$", head) and re.search(r"(?m)^From: " + re.escape(name) + r"$", head):
            return text
    return ""


def coming_due(name: str, since: datetime | None, until: datetime) -> list[dict]:
    """Its unsettled commitments (open or overdue) due after `since` (None: any time) and by `until`, soonest first,
    each with `due_at` (local): what wakes the instance (autonomy.wake_gate, autonomy.commitment_wakes)."""
    out = []
    for item in ledger(name, until).values():
        at = _at(item["due"])
        if item["status"] in ("open", "overdue") and at is not None and at <= _local(until) \
                and (since is None or at > _local(since)):
            out.append({**item, "due_at": at})
    return sorted(out, key=lambda i: i["due_at"])


def overdue_reminders(name: str, since: datetime | None, until: datetime) -> list[dict]:
    """Unsettled promises whose latest daily due-time anniversary falls after the last cycle, with remind_at."""
    if since is None:
        return []
    out = []
    for item in ledger(name, until).values():
        due = _at(item["due"])
        if item["status"] not in ("open", "overdue") or due is None:
            continue
        days = (_local(until) - due) // timedelta(days=1)
        at = due + timedelta(days=days)
        if days >= 1 and at > _local(since):
            out.append({**item, "remind_at": at})
    return sorted(out, key=lambda i: i["remind_at"])


def _episodes(name: str) -> list[dict]:
    return metrics.live_episodes(name) + metrics._episodes(name, "talk") + metrics._episodes(name, "sleep")


UNCHECKED = "verified (its evidence is built-in or plugin calls, or older than the record: not checked)"


def _verification(item: dict, episodes: list[dict], judgments: list[dict], record=None) -> tuple[str, dict | None]:
    """"verified", UNCHECKED (verified, but no supervisor-tool step of the evidence could be checked against the
    supervisor's record: served.py), or why it is not."""
    if item.get("status") != "kept":
        return "not kept", None
    evidence = item.get("evidence")
    if not isinstance(evidence, str):
        return "no episode citation", None
    reason, ep, checked = _checked_evidence(episodes, item, evidence, record)
    if reason:
        return reason, ep
    if ep.get("kind") != "live":
        return "cited a conversation or a sleep: no judge graded it", ep
    verdict = next((j.get("criterion_met") for j in judgments if j.get("episode_ts") == ep["ts"]), None)
    if verdict is None:
        return "cited cycle not judged", ep   # unjudged evidence is a claim the record cannot check yet
    if verdict == "no":
        return "cited cycle judged no", ep
    return "verified" if checked or record is None else UNCHECKED, ep


def verify(name: str, item: dict) -> str:
    if not NAME.fullmatch(name):
        return "invalid instance name"
    from . import served
    return _verification(item, _episodes(name), metrics.judgments(name), served.Record(name))[0]


def _counts(items: list[dict], episodes: list[dict], judgments: list[dict], record=None) -> str:
    verdicts = [_verification(it, episodes, judgments, record)[0] for it in items if it["status"] == "kept"]
    verified, unchecked = verdicts.count("verified"), verdicts.count(UNCHECKED)
    counts = {s: sum(it["status"] == s for it in items) for s in (*STATUSES, "expired", "overdue", "open")}
    return (f"made {len(items)}; kept (verified) {verified}; kept (verified, evidence not checkable) {unchecked}; "
            f"kept (not verified) {counts['kept'] - verified - unchecked}; "
            + "; ".join(f"{s} {counts[s]}" for s in ("broken", "superseded", "void", "expired", "overdue", "open")))


def section(name: str) -> list[str]:
    items = list(ledger(name, rows=recorded(name)[0]).values())
    if not items:
        return []
    items = [it for it in items if it["made_to"] not in ("self", "own", name)]   # promises to self are no longer kept
    if not items:
        return []
    from . import served
    episodes, judgments, record = _episodes(name), metrics.judgments(name), served.Record(name)
    operator = [it for it in items if it["made_to"] == "operator"]
    others = [it for it in items if it["made_to"] != "operator"]
    cited = set()
    for it in items:
        if it["status"] == "kept":
            _, ep = _verification(it, episodes, judgments, record)
            if ep and ep.get("kind") == "live":
                cited.add(ep["ts"])
    # Count each cited cycle once even when it kept several promises.
    difficulties = {j["episode_ts"]: j["difficulty"] for j in judgments if j.get("episode_ts") in cited
                    and isinstance(j.get("difficulty"), (int, float)) and not isinstance(j["difficulty"], bool)
                    and 1 <= j["difficulty"] <= 5}
    mean = f"{sum(difficulties.values()) / len(difficulties):.2f}/5 (n={len(difficulties)})" if difficulties else "not judged"
    return ["## Promises I made and kept", "", f"- To the operator: {_counts(operator, episodes, judgments, record)}.",
            f"- To other instances: {_counts(others, episodes, judgments, record)}.",
            f"- Mean judged difficulty of cited cycles: {mean}.",
            f"- A cited supervisor-tool step is verified only if the supervisor served it in that run and its result "
            f"matches the fingerprint kept at the call; {served.NOT_CHECKED}.",
            "- Counted from the supervisor's own copy of my ledger: a promise or settlement later removed from my file "
            "is still counted."]


def report(names: list[str]) -> str:
    lines = []
    for name in names:
        rows, changes = recorded(name)
        items = ledger(name, rows=rows)
        lines.append(f"{name}: commitments")
        if not items:
            lines.append("  (none)")
            continue
        from . import served
        episodes, judgments, record = _episodes(name), metrics.judgments(name), served.Record(name)
        self_count = sum(it["made_to"] in ("self", "own", name) for it in items.values())
        if self_count:
            expired = sum(it["status"] == "expired" and it["made_to"] in ("self", "own", name) for it in items.values())
            lines.append(f"  to self (no longer kept): {self_count}; expired {expired}")
        for item in items.values():
            if item["made_to"] in ("self", "own", name):
                continue
            verification = (f"; {_verification(item, episodes, judgments, record)[0]}" if item["status"] == "kept"
                            else "")
            lines.append(f"  {item['id']}: {item['status']}{verification}; to {item['made_to']}; due {item['due']}; "
                         f"{item['promise']} — check: {item['criterion']}"
                         + (f"; evidence {item['evidence']}" if item["evidence"] else "")
                         + (f"; {item['note']}" if item.get("note") else "")
                         + ("; removed from its ledger since recorded" if item["id"] in changes["removed"]
                            else "; altered in its ledger since recorded" if item["id"] in changes["altered"] else ""))
        if any(it["status"] == "kept" and it["made_to"] not in ("self", "own", name) for it in items.values()):
            lines.append(f"  (kept evidence is checked against the calls the supervisor served; {served.NOT_CHECKED})")
    return "\n".join(lines)


# -- a broken promise reaches the operator (#239) -----------------------------------------------------------------
# A promise to the operator settled as broken was recorded and the instance moved on: he heard of the miss the next
# day, when he asked; the instance's next message, five minutes later, was about something else. After each live
# cycle or sleep, every operator promise settled broken that no later message of the instance to the operator names
# (by its id: settle_commitment asks for it) gets one message, sent by the supervisor in the instance's name. Never
# twice.
# The first run only marks where it starts: promises broken before it are not announced.
TOLD_DIR = "broken_told"


def _told_path(name: str):
    return SUP_DIR / TOLD_DIR / f"{name}.json"


def _told(name: str) -> dict:
    try:
        data = json.loads(_told_path(name).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and isinstance(data.get("told"), dict) else {}


def _save_told(name: str, data: dict) -> None:
    path = _told_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def broken_notice(name: str, item: dict) -> str:
    """The operator's line: the promise, what blocks it (the settlement's reason), and no new estimate (the
    supervisor has none; the instance gives one when it answers)."""
    due = _at(item["due"])
    return (f"Promise {item['id']} to you is broken: {item['promise']} (due {due:%Y-%m-%d %H:%M}). "
            f"What blocks it: {item['evidence']} New estimate: none given. "
            f"(Sent by the supervisor: {name} settled it as broken without telling you.)")


def tell_broken(name: str, deliver, now: datetime | None = None) -> list[str]:
    """Send `deliver(text)` once for each operator promise settled broken since the record began that no later
    message from the instance to the operator names. Returns the ids sent; the instance's own message is recorded as
    told by it, so neither is looked at again."""
    from . import messages
    now = now or datetime.now()
    state = _told(name)
    if not state:
        _save_told(name, {"since": now.isoformat(timespec="seconds"), "told": {}})
        return []
    since = _at(state.get("since")) or now
    broken = [it for it in ledger(name, now).values() if it["made_to"] == "operator" and it["status"] == "broken"
              and it["id"] not in state["told"] and (_at(it["settled_at"]) or since) >= since]
    if not broken:
        return []
    said = [(_sent_at(m), m.get("text") or "") for m in messages.history()
            if m.get("from") == name and m.get("to") == "operator"]
    sent = []
    for item in broken:
        settled, named = _at(item["settled_at"]), re.compile(re.escape(item["id"]) + r"(?!\d)")
        if any(at is not None and at >= settled and named.search(text) for at, text in said):
            state["told"][item["id"]] = "instance"
            continue
        # recorded before it is sent: a delivery that fails halfway is logged by the caller, never sent twice
        state["told"][item["id"]] = "supervisor"
        _save_told(name, state)
        deliver(broken_notice(name, item))
        sent.append(item["id"])
    _save_told(name, state)
    return sent


def _sent_at(row: dict) -> datetime | None:
    """When a logged message was sent, to the microsecond its file name keeps (the log's ts has whole seconds)."""
    m = re.match(r"(\d{8}-\d{6}-\d{6})-", str(row.get("file") or ""))
    if m:
        try:
            return datetime.strptime(m[1], "%Y%m%d-%H%M%S-%f")
        except ValueError:
            pass
    return _at(row.get("ts"))

