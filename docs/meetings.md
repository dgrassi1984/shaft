# Meetings

The supervisor stores each meeting under `.supervisor/meetings/<id>/`. Each folder has a `meeting.json` metadata
file, a `sidecar.json` file for recorder data, and either an `upload/` directory or assembled `audio.<ext>`.
The audio extension is `m4a`, `caf`, or `wav`. A meeting without an account alias is unfiled.

The app creates a meeting in state `uploading` and sends numbered chunks of up to 8 MiB. The server records the
indices received so the app can resume a partial upload. To finish, the app sends the expected size, SHA-256,
and chunk count. A mismatch leaves the chunks available for retry. A successful finish queues the meeting and
removes the chunk directory. See the [meeting API](app-api.md#20-meetings) for request and response details.

## Record on iPhone or iPad

Open **Meetings** from the chat switcher on iPhone or the sidebar on iPad. Tell the people in the meeting
that you are recording, then tap **Record**. SHAFT records one channel from the microphone. iOS keeps
other apps' audio and phone call audio out of the recording. End voice mode before recording a meeting.

Start with SHAFT in the foreground. The microphone keeps recording when the screen locks or SHAFT
goes into the background. A phone call or Siri can pause the recording. The app notes the gap and
resumes in the same file if iOS allows it. The transcript marks each gap at the point where audio
paused and shows how long the gap lasted. If the microphone cannot resume, SHAFT keeps the partial
recording and shows why it stopped.

Tap **Stop** to save the recording and send it to the paired SHAFT server over the tailnet. Meetings
shows upload progress and errors. Open SHAFT again to resume a pending upload. The app keeps a
confirmed recording on the device for seven days and prunes it on a later foreground pass.

## Choose the microphone on the Mac

The Mac records from the system default input unless you choose another in **Meeting → Microphone** (in
the menu bar and in the SHAFT menu bar icon). The choice applies to online and in-person recordings and is
kept across launches. If the chosen microphone is not connected when you start, SHAFT records from the
system default and says so while recording. The sidecar names the input recorded from as `input_device`, its
Core Audio UID as `input_device_uid`, and the choice as `input_chosen` (`default` when none was chosen). A
microphone unplugged and plugged back in during a recording is picked up again by its UID.

## Set up the Listen worker

On an Apple Silicon Mac with `uv`, run these commands from the SHAFT checkout.

```sh
brew install ffmpeg
uv venv --python 3.12 .venv-listen
uv pip install --python .venv-listen/bin/python \
  'pyannote.audio==4.1.1' 'mlx-whisper==0.4.3' 'soundfile==0.14.0' 'numpy==2.5.5'
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
channel. Listen diarises each channel separately; speaker labels begin with `M` for the microphone and `S` for
system audio. It then transcribes the meeting as one conversation: it brings both channels of an online recording
to the same speech loudness and mixes them, and cuts the speech into chunks of up to 28 seconds that start and end
in pauses. A short turn is transcribed with the speech around it, not cut out on its own: Whisper invents phrases
such as "Thank you" for a short clip padded with silence. Each chunk detects its language and is prompted with what was said just before it and with
the names of the people in the speaker gallery and the terms in `SHAFT_LISTEN_VOCABULARY` (product names, jargon:
anything Whisper should spell as written). Listen gives each word to the speaker whose diarised turn covers it
and, online, whose channel carries it loudest against that channel's own speech; it changes speaker inside a
phrase only on strong evidence. A speaker's time and voice clips come from their diarised turns. A channel's labels
are numbered by their first turn of at least 0.4 seconds, so a label keeps its number when the meeting is reprocessed.

A ready meeting has `transcript.json` and `transcript.md` in its folder. Segment and word times are seconds from
the start of the recording. To mark an interruption, the recorder can add a `gaps` array to `sidecar.json` with
`start`, `end`, and `reason` for each gap. The Markdown transcript shows these gaps in time order.
Listen marks a meeting `failed` if decoding fails or it detects no speech or transcribes no words. With at
least 10 seconds of detected speech, it also requires at least one word per 30 seconds of speech. Use
`shaft meetings show <id>` to read the error.

## Speaker names and voiceprints

The Listen worker stores cluster voiceprints in `speakers.npz` in the meeting folder and up to three clean
5–10 second clip ranges per label in `transcript.json`. The paired app plays these clips from the original
audio channel. To enrol a voiceprint, the operator names a label in the paired app or runs `shaft meetings name`.
Naming a label updates both transcript files without running the speech engines again.
Reprocess older meetings to generate voiceprints and clip ranges.

The gallery lives under `.supervisor/speakers/`. It stores people in `people.json`, their confirmed voiceprints
in `embeddings/<person>.npz`, and forgotten person IDs in `tombstones.json`. Each gallery row records its source
meeting, label, and channel kind. Voiceprints stay on the supervisor Mac and are never shown to instances.

Listen compares each label with the mean voiceprint for the same channel kind, using all of a person's samples
when that kind is unavailable. It uses the current gallery every time it processes a meeting. The minimum
cosine similarity is `SHAFT_LISTEN_MATCH` (default `0.6`, uncalibrated until measured on real meetings).
If the top two people score within `0.05`, the label stays unknown.

Forgetting a person removes their voiceprints and saved clip ranges. Existing transcript names remain unless
the operator chooses `--anonymise`, which replaces them with labels such as `Speaker S2`. The worker preserves
anonymisation for those meeting labels on reprocessing. To delete a recording and its outputs, delete the
meeting. The paired app can also remove gallery samples learned from that meeting when it deletes it.

## Instance access

File a meeting with `shaft meetings file <id> <alias>`. Grant an instance access with
`shaft meetings grant <instance> <alias>` or from the web Access page. Use a configured Google account alias
for both commands. An unfiled meeting is invisible to every instance. Revoking a grant takes effect on the next
broker call. A transcript page already delivered to an inbox stays there.

The broker offers three tools to granted instances. It checks the grant on each call and allows 30 meeting calls
per run, including continuation pages. Unknown, malformed, failed, and ungranted meetings receive the same
`NotVisible` response.

| Tool | Result |
|---|---|
| `meeting_search(text="", person="", after="", before="")` | One row per matching ready meeting, newest first. Each row has an ID, title, recording date, duration in seconds, and people. Search text matches the title or transcript. Dates are inclusive and use `YYYY-MM-DD`. |
| `meeting_read(id, page=1)` | Up to 6,000 characters of marked transcript. Each page includes the title, date, account, people, and gaps. Unknown labels appear as `Speaker S2 (unknown)`. |
| `meeting_propose_speaker(meeting, speaker, name, why)` | Creates a pending proposal. Only the operator can confirm the name and enrol the voiceprint. |

Set `WATCH_MEETINGS` to `inbox` to receive the first transcript page when a granted meeting first becomes ready.
Use `wake` to also request a cycle. The default is `off`. Filing or refiling a ready meeting sends the note to
watching instances with a grant for the new account that have yet to receive it.

Run `shaft meetings proposals` to list pending speaker names. The paired-device API accepts confirmation or
rejection. [Meeting API](app-api.md#206-speaker-proposals)

## Point an instance at a meeting

1. In the paired app, open **Meetings** and select the recording. If it is unfiled, choose an **Account** and click **Save**.
2. Open the instance's settings. Under **Access**, turn on **Meetings** for the same account.
3. In **Meetings**, click **Copy Reference** and paste the line into the instance's chat.

The instance uses `meeting_read` with the ID in the reference to read the transcript once the meeting is ready.
The recorder files new meetings automatically when there is exactly one configured Google account.

## CLI

| Command | Result |
|---|---|
| `shaft meetings list` | Lists meetings newest first. |
| `shaft meetings show <id>` | Prints the meeting metadata. |
| `shaft meetings reprocess <id>` | Queues a ready or failed meeting again using its stored audio. |
| `shaft meetings delete <id>` | Deletes the meeting folder and its contents. |
| `shaft meetings import FILE [--title T] [--mode online\|inperson]` | Copies local audio into a new queued meeting. The file stem supplies the default title. The default mode is `inperson`. |
| `shaft meetings file <id> <alias>` | Files a meeting under a configured Google account. |
| `shaft meetings grant <instance> <alias>` | Grants access to ready transcripts filed under the account. |
| `shaft meetings revoke <instance> <alias>` | Revokes access to the account's transcripts. |
| `shaft meetings proposals` | Lists pending speaker name proposals. |
| `shaft meetings name <id> <label> <name>` | Enrols the label's voiceprint under the name. |
| `shaft speakers list` | Lists people and their gallery sample counts. |
| `shaft speakers rename <pid> <name>` | Renames a person in the gallery and saved transcripts. |
| `shaft speakers forget <pid> [--anonymise]` | Deletes the person's voiceprints. With `--anonymise`, replaces their name in saved transcripts. |

Each subcommand accepts `--json` after the subcommand name.
