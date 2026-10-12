"""SHAFT agent entry point. Normally started by the supervisor (bin/shaft), not directly."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from . import config as config_mod
from . import live, prompts, recovery, reflect, sleep, talk
from .llm import Client, LLMError
from .state import State


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shaft")
    sub = parser.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("talk", help="chat in the terminal")
    t.add_argument("--show-thinking", action="store_true", help="stream my reasoning, dimmed")
    sub.add_parser("reflect", help="run one reflection cycle")
    sub.add_parser("live", help="run one autonomous cycle with tools")
    sub.add_parser("sleep", help="consolidate memory, personality and goals")
    sub.add_parser("config", help="print the resolved configuration")
    pr = sub.add_parser("probe", help="answer one question in my own voice, without tools (used by evals)")
    pr.add_argument("question")
    args = parser.parse_args(argv)

    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    run_log = cfg.root / "logs" / "run.log"
    run_log.parent.mkdir(parents=True, exist_ok=True)
    with run_log.open("a") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {args.cmd} {cfg.describe()}\n")

    if args.cmd == "config":
        print(cfg.describe().replace(" ", "\n"))
        return 0

    state, client = State(cfg.root), Client.from_config(cfg)
    if args.cmd in ("live", "sleep", "reflect"):   # a conversation never waits: talk does this at its end
        n = recovery.run(cfg, state, client)   # conversations that ended without a journal entry
        if n:
            print(f"(wrote {n} missing journal entr{'y' if n == 1 else 'ies'} for earlier conversations)",
                  file=sys.stderr)
    try:
        if args.cmd == "talk":
            talk.run(cfg, state, client, show_thinking=args.show_thinking)
        elif args.cmd == "reflect":
            print(reflect.run(cfg, state, client))
        elif args.cmd == "live":
            print(live.run(cfg, state, client))
        elif args.cmd == "sleep":
            print(sleep.run(cfg, state, client))
        elif args.cmd == "probe":
            system = prompts.system_prompt(state, cfg, prompts.PROBE_MODE)
            reply = client.complete([{"role": "system", "content": system},
                                     {"role": "user", "content": args.question}],
                                    cfg.MAX_TOKENS_TALK, purpose="probe")
            print(reply.content.strip())
    except (LLMError, ValueError) as e:
        print(f"{args.cmd} failed, nothing saved: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
