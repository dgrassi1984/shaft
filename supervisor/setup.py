"""What the Mac app's setup shows and says about a new instance (docs/app-api.md §18). Stdlib only.

  access(name)        what the operator has granted it, shaped for the app's Access step: from web_config.access (the
                      readers the CLI verbs use) plus the shared browser, the web-search key and a Telegram bot —
                      whether a key is set, never its value.
  telegram_status(name)  where its Telegram stands for the app's Access step: its bot, whether an account is paired
                      and whether the Telegram service runs (§18.5).
  opening_text(name)  the hidden opening the setup sends last (POST /app/v1/setup/hello/<n> → web_chat.opening): who
                      the operator is, its drive, what it may reach, and the request to introduce itself.
"""
from __future__ import annotations

from . import core


def access(name: str) -> dict:
    """{instance, folder, browser: {installed, run}, web_search, telegram, mail, calendar, drive, chat, meetings, meetings_unfiled,
    claude_notes, shortcuts, shortcuts_available, shortcuts_problem, google_accounts}: meetings lists granted filed accounts
    when present; claude_notes is whether its inbox takes notes from the operator's Claude sessions; shortcuts are the
    granted Shortcuts ({id, name, present}), shortcuts_available this Mac's ({id, name}) and shortcuts_problem why
    they could not be listed, or null (§18.6)."""
    from . import browser, google, telegram, web_config
    a = web_config.access(name)
    filed = [g for g in a["meetings"] if g["account"] is not None]
    env = core.secrets_env()
    return {"instance": name, "folder": a["shared"] or None,
            # one Chromium for every instance; an instance browses from inside `run` (supervisor/browser.py)
            "browser": {"installed": browser.version(), "run": "run" not in a["tools_off"]},
            "web_search": bool(env.get("SCRAPEBADGER_API_KEY")),
            "telegram": bool(env.get(telegram.token_key(name))),
            "mail": [{"account": g["account"], "label": g["label"], "draft": bool(g.get("draft"))} for g in a["mail"]],
            "calendar": [{"account": g["account"], "calendar": g["calendar"], "name": g["name"],
                          "write": bool(g.get("write"))} for g in a["calendar"]],
            "drive": [{"account": g["account"], "reach": g["reach"]} for g in a["gdrive"]],
            "chat": [{"account": g["account"]} for g in a["gchat"]],
            **({"meetings": filed} if filed else {}),
            "meetings_unfiled": {"account": None} in a["meetings"],
            "claude_notes": a["claude_notes"],
            "shortcuts": a["shortcuts"], "shortcuts_available": a["shortcuts_available"],
            "shortcuts_problem": a["shortcuts_problem"],
            "google_accounts": [{"alias": alias, "address": data.get("address") or "",
                                 "scopes": [google.SCOPE_NAMES[s] for s in google.SCOPES
                                            if s in google.scopes({"scope": data.get("scope")})]}
                                for alias, data in sorted(a["google_accounts"].items())]}


def telegram_status(name: str) -> dict:
    """{instance, token, bot, paired, account, service, problem, pairing}: whether `name` has a bot token (`bot`: its
    username, when Telegram told it), whether an account is paired with SHAFT's bots (one for all), who, whether the
    Telegram service runs (it has recorded itself, after asking Telegram about every bot), why not when a bot is there
    (`problem`, launchd's words), and whether a pairing code is out and not expired. Never a token or a code."""
    from datetime import datetime
    from . import autonomy, telegram
    from .version import service
    st = telegram.load()
    token = name in telegram.tokens()
    running = bool(service("telegram", stale=False))
    paired = st.get("paired") or {}
    pending = st.get("pairing") or {}
    try:
        open_code = bool(pending) and datetime.fromisoformat(pending["until"]) > datetime.now()
    except (KeyError, TypeError, ValueError):
        open_code = False
    return {"instance": name, "token": token, "bot": ((st.get("bots") or {}).get(name) or None) if token else None,
            "paired": bool(paired),
            "account": (paired.get("username") or str(paired.get("user_id", ""))) or None if paired else None,
            "service": running, "problem": autonomy.service_problem("telegram") if token and not running else None,
            "pairing": open_code}


def _granted(a: dict) -> list[str]:
    """Each grant of access() as a line for the instance."""
    lines = []
    if a["folder"]:
        lines.append(f"a folder of theirs, read-only, which you see as shared/ ({a['folder']})")
    if a["browser"]["installed"] and a["browser"]["run"]:
        lines.append("a real browser (Chromium, with your own profile), from inside your `run` tool")
    if a["web_search"]:
        lines.append("web search (the web_search tool)")
    lines += [f"their Gmail label {g['label']} ({g['account']})" + (", where you may leave drafts for them to send"
                                                                      if g["draft"] else "") for g in a["mail"]]
    lines += [f"their calendar {g['name']} ({g['account']})" + (", which you may also write to" if g["write"] else "")
              for g in a["calendar"]]
    lines += [f"the Drive of {g['account']}: " + ("its own files and those shared with it" if g["reach"] == "all"
                                                  else "its own files") for g in a["drive"]]
    lines += [f"the Google Chat of {g['account']}" for g in a["chat"]]
    if a.get("claude_notes"):
        lines.append("notes from their own Claude sessions in your inbox (Kind: claude): what they worked on with Claude, "
                     "written by Claude for you")
    if a["shortcuts"]:
        lines.append("their Shortcuts " + ", ".join(g["name"] for g in a["shortcuts"]) + ", which you may run on their "
                     "Mac with shortcut_run, as them")
    if a["telegram"]:
        lines.append("a Telegram bot of your own, through which they can talk with you")
    return lines


def opening_text(name: str) -> str:
    """The setup's hidden opening, in English: the instance reads it as the first message of its conversation, and the
    operator sees only its reply."""
    from . import opconfig
    operator = core.instance_config(name).get("OPERATOR_NAME", "").strip()
    drive, _ = opconfig.drive(name)
    if drive is None:
        try:
            drive = (core.TEMPLATE / "drive.md").read_text()
        except OSError:
            drive = "(no drive.md: the default drive applies)"
    granted = _granted(access(name))
    who = (f"Your operator is {operator}. They have" if operator else "Your operator (they have not told you their name "
           "yet) has")
    reach = ("They have granted you:\n" + "\n".join(f"- {g}" for g in granted) if granted else
             "Nothing else is granted yet: only your own folder and tools.")
    return (f"This message comes from SHAFT's setup, not from your operator: they will not see it, only your reply.\n\n"
            f"{who} just created you, {name}, and set what you are for, in your drive.md:\n\n{drive.strip()}\n\n"
            f"{reach} They can grant more later from the app.\n\n"
            f"Please introduce yourself to them briefly — who you are and how you mean to help — and then ask two or "
            f"three questions to start.")
