# Meetings

The supervisor stores each meeting under `.supervisor/meetings/<id>/`. Each folder has a `meeting.json` metadata
file, a `sidecar.json` file for recorder data, and either an `upload/` directory or assembled `audio.<ext>`.
The audio extension is `m4a`, `caf`, or `wav`. A meeting without an account alias is unfiled.

The app creates a meeting in state `uploading` and sends numbered chunks of up to 8 MiB. The server records the
indices received so the app can resume a partial upload. To finish, the app sends the expected size, SHA-256,
and chunk count. A mismatch leaves the chunks available for retry. A successful finish queues the meeting and
removes the chunk directory. See the [meeting API](app-api.md#20-meetings) for request and response details.

The upload and import paths end in `queued`. The processing worker can move a queued meeting to `processing`,
then to `ready` or `failed`.

## CLI

| Command | Result |
|---|---|
| `shaft meetings list` | Lists meetings newest first. |
| `shaft meetings show <id>` | Prints the meeting metadata. |
| `shaft meetings delete <id>` | Deletes the meeting folder and its contents. |
| `shaft meetings import FILE [--title T] [--mode online\|inperson]` | Copies local audio into a new queued meeting. The file stem supplies the default title. The default mode is `inperson`. |

Each subcommand accepts `--json` after the subcommand name.
