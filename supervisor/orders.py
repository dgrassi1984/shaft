"""Standing orders: what the operator decided stays decided (review I2, I4).

shaft re-asked "continue or retire?" four times after the operator said "Retire", and a correction came back once the
instance had closed it by writing a fact. An order is held here, outside the instance's reach:
SUP_DIR/orders/<name>.jsonl, append-only, one row per add or drop {id, text, added_at, by, source, dropped_at}; the
last row of an id is its state. Two authors (`by`):
  - operator: he adds and drops any order (`shaft orders`, the web UI's Config page);
  - instance: an order the instance heard from him (the broker's record_order, source: his conversation or message);
    it never drops one;
The orders in force are shown to the instance verbatim in every live cycle and conversation (broker.tool_specs), and
what it is about to send or change through the broker is checked against them by the judge (check): a message to the
operator or to another instance, a mail draft, a calendar change. One that goes against an order is refused with the
order quoted, before anything is written. Its own keys (`http_request`, `run`) are not checked: they have no single
text to check and no bound on their number. Refusals are counted per order (violations).

The same check holds the instance's corrections (memory/corrections.jsonl, read as supervisor/corrections.py reads it):
a correction is checked until the next SHAFT_CORRECTION_PASSES messages to the operator after it pass the check
against its text, whatever the instance's own ledger says about closing it. A message to the operator refused for
going against it starts the count again; a message the judge could not check (no verdict, or one that does not fit
CHECK_SCHEMA) does not count; a check of a draft, a calendar change or a message to an instance neither counts as a
pass nor restarts the count. Checks go in SUP_DIR/orders/<name>-checks.jsonl, from which the passes are
counted, as of a trial replay's cutoff, only the checks made by then. Logging failures after successful outward
writes do not change the action's answer. A trial replay writes nothing here: the orders
it records and the checks it makes are kept by its ReplayBroker and passed in as `extra` rows, so the rest
of that replay sees them as a live cycle would.
"""
from __future__ import annotations

import fcntl
import json
import re
from datetime import datetime
from pathlib import Path

from .core import jsonl_lines
from . import core

CORRECTION_PASSES, _ = core.knob(
    "SHAFT_CORRECTION_PASSES", "3", core.whole, "messages to the operator that must pass the order check against a "
    "correction before it stops being checked (0: corrections are not checked)",
    group="Runs and conversations", services=("runs", "api", "web"))
TEXT_MAX = 1000          # one order, verbatim
ACTIVE_MAX = 40
SOURCE_MAX = 200
CHECK_TIMEOUT_S = 90     # the instance's broker client gives a tool call 120 s (template shaft/broker.py)
ID = re.compile(r"o[1-9]\d*")
BY = ("operator", "instance")

CHECK_SYSTEM = """\
You check what an AI agent is about to send or change: a message, mail draft or calendar change, against standing \
orders and corrections from the operator. \
Decide only whether the content goes against one of them: it asks again about something an order already decided, \
does or proposes what an order rules out, or repeats the behaviour a correction named. Content that only mentions \
the same subject, reports keeping to an order, or asks about something the orders leave open does not go against \
it. Judge what it says and asks, never its tone. When you are not sure, it does not. order_id is the id of the \
order or correction it goes against ("" when none); reason, one or two sentences, quotes the part that goes against it."""
CHECK_SCHEMA = {"type": "object", "additionalProperties": False,
                "properties": {"violates": {"type": "boolean"}, "order_id": {"type": "string"},
                               "reason": {"type": "string"}},
                "required": ["violates", "order_id", "reason"]}


def _dir() -> Path:
    return core.SUP_DIR / "orders"


def path(name: str) -> Path:
    return _dir() / f"{name}.jsonl"


def checks_path(name: str) -> Path:
    return _dir() / f"{name}-checks.jsonl"


def _at(value) -> datetime | None:
    try:
        at = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return at.astimezone().replace(tzinfo=None) if at.tzinfo else at


def _lines(p: Path) -> list[dict]:
    try:
        text = p.read_text()
    except FileNotFoundError:
        return []
    out = []
    for line in jsonl_lines(text):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _fold(rows: list[dict], as_of: datetime | None = None) -> dict[str, dict]:
    """id -> its order as of `as_of` (now: every row): the last row of an id written by then."""
    items: dict[str, dict] = {}
    for row in rows:
        oid, added = row.get("id"), _at(row.get("added_at"))
        if not isinstance(oid, str) or not ID.fullmatch(oid) or added is None or not isinstance(row.get("text"), str):
            continue
        dropped = _at(row.get("dropped_at"))
        if as_of is not None and (added > as_of or dropped and dropped > as_of):
            continue   # not yet added then, or a drop that came later: the add row before it stands
        items[oid] = row
    return items


def _items(name: str, as_of: datetime | None = None, extra=()) -> dict[str, dict]:
    # Keep legacy ids reserved so a new order cannot inherit old refusal counts.
    return _fold(_lines(path(name)) + list(extra), as_of)


def listing(name: str, as_of: datetime | None = None, dropped: bool = False, extra=()) -> list[dict]:
    """The orders in force (with `dropped`: every order), oldest first. `extra`: rows after the ledger's (a replay's
    own recorded orders, never written)."""
    items = _items(name, as_of, extra).values()
    return sorted((o for o in items if o.get("by") in BY and (dropped or not o.get("dropped_at"))),
                  key=lambda o: int(o["id"][1:]))


def clean(text) -> str:
    """The order's text as it is kept and shown: verbatim, trimmed. Refused empty or over TEXT_MAX."""
    text = str(text or "").strip()
    if not text:
        raise ValueError("an order needs its text")
    if len(text) > TEXT_MAX:
        raise ValueError(f"an order is at most {TEXT_MAX} characters ({len(text)})")
    return text


def in_force(orders: list[dict], text: str) -> dict | None:
    """The order in force with this text (case aside): the same order is never added twice."""
    return next((o for o in orders if not o.get("dropped_at") and o["text"].casefold() == text.casefold()), None)


def add(name: str, text: str, by: str, source: str, now: datetime | None = None) -> tuple[dict, bool]:
    """Record an order: (the order, whether a row was appended). Operator takeovers keep the same id."""
    text = clean(text)
    if by not in BY:
        raise ValueError(f"by must be one of {BY}")
    source = str(source or "").strip()[:SOURCE_MAX]
    now = now or datetime.now()
    p = path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        row, new = added(name, _items(name), text, by, source, now)
        if new:
            f.seek(0)
            text = f.read()
            f.write(("\n" if text and not text.endswith("\n") else "") + json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
    return row, new


def added(name: str, items: dict[str, dict], text: str, by: str, source: str, now: datetime) -> tuple[dict, bool]:
    """The row recording `text` given the orders `items` (id -> order, as _fold): (the order already in force with
    that text, False) or (a new or operator takeover row, True).
    Written by add; kept in memory by a replay."""
    active = [o for o in items.values() if o.get("by") in BY and not o.get("dropped_at")]
    if same := in_force(active, text):
        if by == "operator" and same.get("by") != "operator":
            return {**same, "by": "operator", "source": source,
                    "added_at": now.isoformat(timespec="seconds")}, True
        return same, False
    if len(active) >= ACTIVE_MAX:
        raise ValueError(f"{name} already has {ACTIVE_MAX} standing orders: drop one first (shaft orders {name})")
    number = max((int(oid[1:]) for oid in items), default=0) + 1
    return {"id": f"o{number}", "text": text, "added_at": now.isoformat(timespec="seconds"), "by": by,
            "source": source, "dropped_at": None}, True


def drop(name: str, oid: str, now: datetime | None = None) -> dict:
    """Drop a standing order by id."""
    p = path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        items = {key: row for key, row in _items(name).items() if row.get("by") in BY}
        order = items.get(str(oid).strip())
        if order is None:
            raise ValueError(f"{name} has no order {oid!r}" + (f"; orders: {', '.join(items)}" if items else ""))
        if order.get("dropped_at"):
            raise ValueError(f"order {oid} of {name} was already dropped at {order['dropped_at']}")
        row = {**order, "dropped_at": (now or datetime.now()).isoformat(timespec="seconds")}
        f.seek(0)
        text = f.read()
        f.write(("\n" if text and not text.endswith("\n") else "") + json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
    return row


def line(o: dict) -> str:
    by = "from the operator" if o["by"] == "operator" else "heard from the operator, recorded by you"
    return f"{o['id']} ({by}, {o['added_at'][:16].replace('T', ' ')}): {o['text']}"


# -- corrections held open by the check ------------------------------------------------------------------------------
def _passes(rows: list[dict], as_of: datetime | None = None) -> dict[str, int]:
    """Correction id -> the messages that passed the check against it since it was last gone against, counting only
    the checks made by `as_of` (a replay must not be shown a correction cleared by later messages)."""
    out: dict[str, int] = {}
    for row in rows:
        if "tool" in row and (row["tool"] != "send_message" or row.get("to") != "operator"):
            continue  # other outward checks neither advance nor restart operator-message passes
        if as_of is not None and ((ts := _at(row.get("ts"))) is None or ts > as_of):
            continue
        if row.get("violates") and row.get("order_id"):
            out[row["order_id"]] = 0
        elif row.get("violates") is False and not row.get("error"):
            for cid in row.get("corrections") or []:
                out[cid] = out.get(cid, 0) + 1
    return out


def corrections(name: str, root: Path, as_of: datetime | None = None, extra=()) -> list[dict]:
    """The instance's corrections still checked: recorded by `as_of`, not refused by it (not_accepted), with fewer
    than CORRECTION_PASSES messages passing by `as_of`. `extra`: check rows after the log's (a replay's own checks,
    never written). A ledger that cannot be read holds none and says so in the log."""
    if not CORRECTION_PASSES:
        return []
    from . import corrections as ledger
    try:
        items = ledger._fold(core.safe_read(root, ledger.FILE, ""))
    except (OSError, ValueError, TypeError) as e:
        core.log(f"{name}: corrections ledger unreadable, no correction checked on messages ({type(e).__name__}: {e})")
        return []
    passes = _passes(_lines(checks_path(name)) + list(extra), as_of)
    out = []
    for it in items.values():
        when = _at(it["when"])
        if it["closed_by"] == "not_accepted" or when is None or (as_of is not None and when > as_of):
            continue
        if (n := passes.get(it["id"], 0)) < CORRECTION_PASSES:
            out.append({**it, "passes": n})
    return out


# -- the check -------------------------------------------------------------------------------------------------------
def case(text: str, orders: list[dict], held: list[dict]) -> str:
    parts = ["## Standing orders\n" + ("\n".join(
                 f"- [{o['id']}] {o['text']}"
                 for o in orders) or "(none)"),
             "## Corrections\n" + ("\n".join(f"- [{c['id']}] what was wrong: {c['what_was_wrong']} | what the operator "
                                            f"said: {c['what_operator_said']}" for c in held) or "(none)"),
             "## Outgoing content or change\n" + text]
    return "\n\n".join(parts)


def check(name: str, text: str, orders: list[dict], held: list[dict], exclude=()) -> dict:
    """The judge's verdict on outgoing content: {violates, order_id, reason, judge}. Raises judge.JudgeError when it gives
    none within CHECK_TIMEOUT_S, or names an id that is not one of the orders and corrections it was shown."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as Late
    from . import judge
    model = judge.pick(exclude)
    pool = ThreadPoolExecutor(1, thread_name_prefix="orders-check")
    asked = pool.submit(judge.ask, CHECK_SYSTEM, case(text, orders, held), CHECK_SCHEMA, purpose=f"orders {name}",
                        effort="low", model=model)
    pool.shutdown(wait=False)          # a late answer is still logged (judge_usage.jsonl), never waited for
    try:
        out = asked.result(timeout=CHECK_TIMEOUT_S)
    except Late:
        raise judge.JudgeError(f"the judge {model} gave no verdict within {CHECK_TIMEOUT_S} s") from None
    if not (isinstance(out, dict) and type(out.get("violates")) is bool and isinstance(out.get("order_id"), str)
            and isinstance(out.get("reason"), str)):    # no verdict: never read as "keeps to them", never a pass
        raise judge.JudgeError(f"the judge {model} gave no verdict fitting the schema: {str(out)[:200]!r}")
    ids = {o["id"] for o in orders} | {c["id"] for c in held}
    if out["violates"] and out["order_id"] not in ids:
        raise judge.JudgeError(f"the judge {model} named {out['order_id']!r}, not one of {sorted(ids)}")
    return {"violates": out["violates"], "order_id": out["order_id"] if out["violates"] else "",
            "reason": out["reason"], "judge": model, **judge.independence(model, exclude)}


def log_check(name: str, row: dict) -> None:
    """One row per outward check: what it was checked against and the verdict (or the error)."""
    p = checks_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def refusal(verdict: dict, orders: list[dict], held: list[dict], what="message to the operator") -> str:
    """What the instance is told: the order (or correction) quoted, the judge's reason, and that it may rewrite."""
    oid = verdict["order_id"]
    order = next((o for o in orders if o["id"] == oid), None)
    quoted = (f"standing order {line(order)}" if order else
              next(f"correction {c['id']} (what was wrong: {c['what_was_wrong']}; what the operator said: "
                   f"{c['what_operator_said']})" for c in held if c["id"] == oid))
    done = "not sent" if what.startswith("message") else "not written" if what == "mail draft" else "not done"
    return (f"{done}: the judge found this {what} goes against {quoted}. Why: {verdict['reason']} "
            "Rewrite it so it keeps to that and retry, or leave it.")


def shown(orders: list[dict], held: list[dict]) -> str:
    """The block the instance is shown every run (broker.tool_specs): the orders in force (listing), verbatim, and the
    corrections its covered outward tools are still checked against (corrections)."""
    text = (" STANDING ORDERS in force; only the operator drops orders: "
            + " | ".join(line(o) for o in orders) if orders else " No standing orders now.")
    if held:
        text += (f" Corrections checked on messages, mail drafts and calendar changes (each until "
                 f"{CORRECTION_PASSES} messages to the operator in a row pass): " + " | ".join(
                     f"{c['id']} ({c['passes']} passed): {c['what_operator_said']}" for c in held))
    return text + (" Messages to the operator or another instance, mail drafts and calendar changes are checked "
                   "against these; one that goes against them is refused with the order quoted."
                   if orders or held else "")


def violations(name: str, as_of: datetime | None = None, extra=()) -> dict[str, int]:
    """Order id -> refusals across all outward checks, including replay checks when supplied."""
    counts: dict[str, int] = {}
    for row in _lines(checks_path(name)) + list(extra):
        if as_of is not None and ((ts := _at(row.get("ts"))) is None or ts > as_of):
            continue
        if row.get("violates") is True and ID.fullmatch(str(row.get("order_id", ""))):
            oid = row["order_id"]
            counts[oid] = counts.get(oid, 0) + 1
    return counts
