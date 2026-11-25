# Meetings

The supervisor stores each meeting under `.supervisor/meetings/<id>/`. Each folder has a `meeting.json` metadata
file, a `sidecar.json` file for recorder data, and either an `upload/` directory or assembled `audio.<ext>`.
The audio extension is `m4a`, `caf`, or `wav`. A meeting without an account alias is unfiled.

The app creates a meeting in state `uploading` and sends numbered chunks of up to 8 MiB. The server records the
indices received so the app can resume a partial upload. To finish, the app sends the expected size, SHA-256,
and chunk count. A mismatch leaves the chunks available for retry. A successful finish queues the meeting and
removes the chunk directory. See the [meeting API](app-api.md#20-meetings) for request and response details.

## Set up the Listen worker

On an Apple Silicon Mac with `uv`, run these commands from the SHAFT checkout.

```sh
brew install ffmpeg
uv venv --python 3.12 .venv-listen
uv pip install --python .venv-listen/bin/python \
  'pyannote.audio==4.0.4' 'mlx-whisper==0.4.3' 'soundfile==0.13.1' 'numpy==2.3.5'
```

Accept the conditions for the gated model, then set the supervisor's `HF_TOKEN` with
`shaft secrets set HF_TOKEN`. Listen uses pyannote's [Community-1 speaker diarization
model](https://huggingface.co/pyannote/speaker-diarization-community-1) under CC BY 4.0 and OpenAI's MIT-licensed
[Whisper](https://github.com/openai/whisper/blob/main/LICENSE) large-v3-turbo weights
[converted by MLX Community](https://huggingface.co/mlx-community/whisper-large-v3-turbo).
The engines download their models and run on the Mac.

Run `shaft listen on` to install and start the launchd job. Use `shaft listen status` to check it and
`shaft listen off` to stop it. The log is `.supervisor/com.shaft.listen.log`.

## Processing and transcripts

Listen runs at background priority and processes the oldest queued meeting first, one at a time.
It marks the meeting `processing` before decoding the audio. After an interruption it queues the meeting
once on restart. A second interruption marks the meeting `failed`.

Online recordings use channel 1 for the microphone and channel 2 for system audio. In-person recordings use one
channel. Listen diarises each channel separately, detects the language of each turn, and removes microphone
turns that match overlapping system turns. Speaker labels begin with `M` for the microphone and `S` for system audio.

A ready meeting has `transcript.json` and `transcript.md` in its folder. Segment and word times are seconds from
the start of the recording. To mark an interruption, the recorder can add a `gaps` array to `sidecar.json` with
`start`, `end`, and `reason` for each gap. The Markdown transcript shows these gaps in time order.
Listen marks a meeting `failed` if decoding fails or it detects no speech or transcribes no words. With at
least 10 seconds of detected speech, it also requires at least one word per 30 seconds of speech. Use
`shaft meetings show <id>` to read the error.

## CLI

| Command | Result |
|---|---|
| `shaft meetings list` | Lists meetings newest first. |
| `shaft meetings show <id>` | Prints the meeting metadata. |
| `shaft meetings reprocess <id>` | Queues a ready or failed meeting again using its stored audio. |
| `shaft meetings delete <id>` | Deletes the meeting folder and its contents. |
| `shaft meetings import FILE [--title T] [--mode online\|inperson]` | Copies local audio into a new queued meeting. The file stem supplies the default title. The default mode is `inperson`. |

Each subcommand accepts `--json` after the subcommand name.
