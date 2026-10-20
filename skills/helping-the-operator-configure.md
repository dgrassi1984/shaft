# Helping the operator configure SHAFT: look it up in shaft-docs/, give the exact commands, never run them

Use this when the operator asks how to set something up, change it, grant it, or turn it on or off in SHAFT: a mail
label or calendar for an instance, a shared folder, Telegram, the web UI or the API, a model or a budget, a scheduled
job, a tool switched off, a Google account, anything in `shaft settings` or an instance's `config.env`.

## Where the answer is

`shaft-docs/` in my folder is the supervisor's copy of SHAFT's own documentation: `README.md` and the user docs. It is
refreshed at every start of mine, so it is current, and I can read it but not change it. I read it with `read_file`
and `list_files`. Nothing else is the source: not my memory of an old answer, not a guess from a command's name.

- `shaft-docs/cli.md`: every `shaft` command, one line each. Start here.
- `shaft-docs/configuration.md`: supervisor settings (`shaft settings`), every knob, an instance's `config.env`.
- `shaft-docs/integrations.md`: Telegram, Google accounts, the operator's mail and calendar, an instance's own
  Gmail and Drive, scheduled jobs, images, the browser.
- `shaft-docs/web-and-api.md`: the terminal client, the API, the web UI.
- `shaft-docs/safety.md`: the sandbox, what each grant opens to an instance.
- `shaft-docs/local-model.md`, `shaft-docs/architecture.md`, `shaft-docs/README.md`: the local model, the layout,
  the overview.

The longer files take more than one `read_file` (it returns one page; `offset` reads on). To go straight to the part
I need, `run` `grep -nb 'shaft mail' shaft-docs/*.md` (or `grep -nb '^## ' shaft-docs/integrations.md` for the
sections): the number after the line number is about the character offset to pass to `read_file`. I read the whole
section, including the lines on when a change takes effect and what it does not undo.

If `shaft-docs/` is missing or empty, I say so and do not answer from memory.

## What I answer

1. **The commands**, each in its own code block, exactly as the docs write them. What I do not know for certain is
   a `<placeholder>`, with one line saying what goes there. What I do know I fill in: my own name, and the mail
   labels and calendars my prompt says I am granted. An account alias, a label or an instance I have not seen is a
   placeholder, never a guess.
2. **What each command does**, in one line, including what the docs say about when it takes effect (my next call,
   my next run) and what it leaves behind.
3. **How to check it worked**: the command the docs give to show the current state (`shaft mail <name>`,
   `shaft status`, …) and what it should print.
4. **Where it is written**: `shaft-docs/<file>`, the section heading.

If the docs also describe a way to do it in the web UI, I say that too.

If a change gives an instance more reach (a mail label, a calendar, a shared folder, its own Google account), I add
one line from `shaft-docs/safety.md` saying what it opens.

## What I never do

- **Run or change it myself.** I tell the operator what to run; the operator runs it. Not with `run`, not with a
  plugin, not with `set_config`, not by editing a file, even when I could reach the setting.
- **Ask for a secret.** No password, key, token or pairing code goes into a chat with me. A step that needs one
  (a Google login, a Telegram bot token, an API key) is the operator's to do on their Mac, and I say so.
- **Invent.** If the docs do not cover it, I say so plainly, name the files I checked, and point to the closest thing
  they do describe. I never make up a command, a flag or a setting name.
