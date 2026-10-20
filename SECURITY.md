# Security

SHAFT runs self-modifying LLM instances. Treat their code and the content they read as untrusted.

## What can go wrong

Instances run their own Python and plugins under macOS `sandbox-exec`. The sandbox restricts filesystem access and network connections; it is a security boundary, not a guarantee that generated code is safe. See `supervisor/sandbox.py:113`, `supervisor/sandbox.py:172`, `supervisor/sandbox.py:228` and the launch path in `supervisor/runner.py:435`.

Instances can reach the public internet through the supervisor's egress proxy. It rejects non-public addresses, including localhost, LAN and tailnet addresses, and connects to the IP it checked after resolving the hostname. The instance's sandbox permits selected local service ports, including the proxy. Public internet access can still send data out or perform actions on external services (`supervisor/egress.py:44`, `supervisor/egress.py:126`, `supervisor/sandbox.py:138`).

Instances do not receive the supervisor's API keys. The supervisor reads the root `.env`; the broker holds credentials and adds them to authorised requests. The sandbox denies access to an instance's secret store and filters inherited environment variables (`supervisor/core.py:263`, `supervisor/broker.py:473`, `supervisor/broker.py:1097`, `supervisor/sandbox.py:24`, `supervisor/sandbox.py:215`). These controls protect configured credentials; they cannot prevent an operator from putting a secret into a message or a readable file.

Instances can spend money on cloud models and coding hand-offs. Per-instance daily controls include `SHAFT_CLOUD_DAILY_USD` and `SHAFT_CODER_DAILY_USD`. Instance-requested trials also have `SHAFT_TRIALS_PER_DAY`, `SHAFT_TRIAL_DAILY_USD` and `SHAFT_TRIAL_LOCAL_UNITS_PER_DAY`. Cloud and coder checks compare recorded spending before starting work, so do not treat these as exact billing ceilings (`supervisor/cloudllm.py:54`, `supervisor/cloudllm.py:229`, `supervisor/coder.py:39`, `supervisor/coder.py:139`, `supervisor/trial.py:45`, `supervisor/trial.py:738`).

The operator can grant read access to Gmail threads with selected labels and to selected calendars. The supervisor holds OAuth credentials and checks grants; Gmail's OAuth scope itself is not restricted to a label (`supervisor/mail.py:56`, `supervisor/broker.py:1295`, `supervisor/calendar.py:1`, `supervisor/google.py:3`). Mail and calendar text can contain prompt injection: a sender can try to make an instance follow instructions in material it reads. Granting read access exposes that content to the instance.

The operator can also authorise an instance-owned Google account. Its Gmail scope allows reading, sending, labelling and trashing mail. Its Drive scope covers its files and files shared with it, and allows sharing, trashing and permanent deletion of its own files. This is broader authority than operator-mail label grants. The broker refuses an instance-owned Google key that belongs to a registered operator account (`supervisor/google.py:38`, `supervisor/google.py:47`, `supervisor/broker.py:1153`). Prompt injection can therefore lead to actions using the instance's granted authority even though the instance never sees the credential.

## Network access to the supervisor

The web UI and API bind to localhost and are exposed remotely through Tailscale Serve, for the tailnet only. Do not expose them through a public proxy or Tailscale Funnel (`supervisor/api.py:531`, `supervisor/api.py:609`, `supervisor/web.py:950`, `supervisor/web.py:989`).

The chat API requires a bearer key and an allowed Tailscale login. Private web UI pages require an allowed login and a paired browser cookie. The inbox endpoint is an explicit exception: its narrower `SHAFT_INBOX_KEY` works without a login, but the endpoint still has the same network boundary (`supervisor/api.py:223`, `supervisor/api.py:244`, `supervisor/web.py:322`, `supervisor/web.py:344`).

## Reporting a vulnerability

Open a private GitHub security advisory on this repository. Include the affected revision, reproduction steps, expected and observed behaviour, and the security boundary involved. Do not include real credentials or private instance data, and do not open a public issue with exploit details.
