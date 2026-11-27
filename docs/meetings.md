# Meetings

The supervisor stores each meeting under `.supervisor/meetings/<id>/`. Each folder has a `meeting.json` metadata
file, a `sidecar.json` file for recorder data, and either an `upload/` directory or assembled `audio.<ext>`.
The audio extension is `caf` (the apps), `m4a` (older Mac builds) or `wav`. Apple's IMA4 `.caf` carries an empty packet table that ffmpeg refuses; before decoding or cutting a clip, the server renames that chunk to `free` in place (lossless: constant-size packets need no table), so the file's bytes differ from the uploaded `sha256` by those four. A meeting without an account alias is unfiled.

The app creates a meeting in state `uploading` and sends numbered chunks of up to 8 MiB. The server records the
indices received so the app can resume a partial upload. To finish, the app sends the expected size, SHA-256,
and chunk count. A mismatch leaves the chunks available for retry. A successful finish queues the meeting and
removes the chunk directory. See the [meeting API](app-api.md#20-meetings) for request and response details.

## Record on iPhone or iPad

Open **Meetings** from the chat switcher on iPhone or the sidebar on iPad. Tell the people in the meeting
that you are recording, then tap **Record**. SHAFT records one channel from the microphone. iOS keeps
other apps' audio and phone call audio out of the recording. End voice mode before recording a meeting.

Start with SHAFT in the foreground. The microphone keeps recording when the screen locks or SHAFT
goes into the background. A phone call or Siri can pause the recording. The app waits for it to end (up to
three hours), notes the gap and resumes in the same file if iOS allows it. Paused time is not in the audio, so the
gap records its clock length (`wall_seconds` in the sidecar): the transcript marks each gap where audio paused and
says how long nothing was recorded. If the microphone cannot resume, SHAFT keeps the partial recording and says why
it stopped: a notification when SHAFT is in the background, and a banner above the chat until Meetings is opened.
The banner above the chat and the meeting's toolbar both have **Stop**. The Live Activity shows elapsed time
on the Lock Screen and Dynamic Island. Tap **Stop** on the Lock Screen or in the expanded Dynamic Island.
The activity shows when recording is paused and briefly shows the reason if recording stops early.

Say "Record a meeting with SHAFT" or "Stop recording my meeting with SHAFT" to Siri, or use
**Record Meeting** and **Stop Meeting Recording** in Shortcuts or Control Center. On supported iPhones,
assign either control to the Action button. **Record Meeting** opens SHAFT before using the microphone.

The iPhone records IMA4 audio in a `.caf` file (24 kHz, about 0.7 MB a minute): if iOS ends SHAFT during a
recording, the file stays readable up to that moment and is sent at the next launch, marked as cut short.

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

Record from the Meeting menu, the menu bar icon or the Meetings window (⌥⌘M), which has **Record Online**,
**Record In Person** and **Stop Recording** while one runs (also in the open meeting's toolbar).
The same Siri phrases and Shortcuts actions work on Mac. **Record Meeting** defaults to **Online**.
Choose **In Person** in Shortcuts to record only the microphone.

The Mac records IMA4 audio in a `.caf` file (two channels at the microphone's rate, about 2.9 MB a minute at
48 kHz). Quitting SHAFT while recording finishes the file first; if SHAFT crashes, the file stays readable up to
that moment, and at the next launch SHAFT trims it, writes a sidecar that says it was cut short, and sends it.
A recording that holds no readable audio is kept in `~/Library/Application Support/SHAFT/Meetings/`, never sent,
and listed in the Meetings window and the Meeting menu with **Show in Finder**. While recording, the Mac does not
go to sleep on its own; closing the lid still sleeps it, and the time asleep becomes a gap: up to a minute is
filled with silence, a longer one is only marked, with its clock length (`wall_seconds`), as the iPhone's are. The recording says so while it runs when the
microphone has heard nothing for 30 seconds, when the system audio stays silent (the permission), when audio
cannot be written, and when the audio device cannot be restarted; a warning goes once its cause clears. If the
microphone's rate changes during the recording (AirPods switching to call mode), the file keeps its rate and SHAFT
resamples. A recording stopped while another uploads is sent after it. What a recording could not
keep stays shown after it was sent. **Stop Sending** pauses an upload; a new recording can start while one uploads.

The apps send a recording without a title. Listen names it after the calendar event the recording overlaps most
(timed, not declined) on the primary calendar of the Google account it is filed under, or of every logged-in
account when it is not filed yet; until then, or when no event overlaps, it is called `Meeting <date> at <time>`.
A meeting created unfiled is filed under the account whose calendar holds that event (`account_from: calendar`),
unless an event as long on another account's calendar makes it ambiguous, or the operator chose its account (unfiled
included) in the app or with `shaft meetings file`. Meetings from before this are never filed by Listen.
A title the operator sets is never replaced, nor is a meeting filed under another account while Listen read the
calendar. A calendar that cannot be read is logged in the Listen log and only costs the title. The event's
invitees' names join the prompt (below).

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
such as "Thank you" for a short clip padded with silence. Each chunk detects its language; a chunk read unsure (mean
log-probability below −1) is read again in the meeting's other language and the likelier reading kept. It is
prompted with the terms in `SHAFT_LISTEN_VOCABULARY`, the people in the speaker gallery, the event's invitees and the
meeting's own vocabulary (in that order, because Whisper keeps the end of a long prompt: past 400 characters the
general terms go first), then with the words kept from the chunk before it (none after 30 seconds of silence).

In the apps, **Words and People…** on a meeting sets its own vocabulary (names and terms, comma-separated) and how
many people spoke besides the operator, then processes it again: no more voices than that are told apart on the
far end (online) or in the room (in person). The calendar alone never caps the voices, since two people can share
one invitation; when a call has more unnamed far-end voices than its invitation has other people, the meeting says
so and suggests Merge into…. The invitees' names are the first naming suggestions, and a speaker who never spoke
five seconds alone gets their longest clean stretch as a clip. Listen gives each word to the speaker whose diarised turn covers it
and, online, whose channel carries it loudest against that channel's own speech; it changes speaker inside a
phrase only on strong evidence. A speaker's time and voice clips come from their diarised turns. A channel's labels
are numbered by their first turn of at least 0.4 seconds, so a label keeps its number when the meeting is reprocessed.

A ready meeting has `transcript.json` and `transcript.md` in its folder. The first successful transcription marks
the meeting `published` in `meeting.json`. Older `ready` meetings count as published and get the flag when queued
again. Reprocessing keeps the last good transcript available in the app and to granted instances, even if the new
attempt fails. A successful attempt replaces `transcript.json` atomically. Segment and word times are seconds from
the start of the recording. To mark an interruption, the recorder can add a `gaps` array to `sidecar.json` with
`start`, `end`, and `reason` for each gap. The Markdown transcript shows these gaps in time order.
On success, Listen saves a `warnings` list in `transcript.json` and `meeting.json`. The list contains the recorder's
`problems` and adds a positive `seconds_lost` count if the recorder has not already reported it.
Listen marks a meeting `failed` if decoding fails or it detects no speech or transcribes no words. With at
least 10 seconds of detected speech, it also requires at least one word per 30 seconds of speech. Use
`shaft meetings show <id>` to read the error.

## Speaker names and voiceprints

The Listen worker stores cluster voiceprints in `speakers.npz` in the meeting folder and up to three clean
5–10 second clip ranges per label in `transcript.json`. The paired app plays these clips from the original
audio channel. To enrol a voiceprint, the operator names a label in the paired app or runs `shaft meetings name`.
Naming or merging labels requires a `ready` meeting. Naming a label updates both transcript files without running
the speech engines again.
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
for both commands. To allow access to unfiled meetings, use `shaft meetings grant <instance> --unfiled`.
Use `shaft meetings revoke <instance> --unfiled` to withdraw it. Filing or revoking changes visibility on the next
broker call. A transcript page already delivered to an inbox stays there.

The broker offers three tools to granted instances. It checks the grant on each call and allows 30 meeting calls
per run, including continuation pages. Unknown, malformed, never-published, and ungranted meetings receive the same
`NotVisible` response.

| Tool | Result |
|---|---|
| `meeting_search(text="", person="", after="", before="")` | One row per matching published meeting, newest first. Each row has an ID, title, recording date, duration in minutes, people, and account. Search text matches the title or transcript. Dates are inclusive and use `YYYY-MM-DD`. |
| `meeting_read(id, page=1)` | Up to 6,000 characters of marked transcript. Each page includes the title, date, account, people, and gaps. If recording warnings exist, they appear on a `Recording problems:` line. Unknown labels appear as `Speaker S2 (unknown)`. |
| `meeting_propose_speaker(meeting, speaker, name, why)` | Creates a pending proposal. Only the operator can confirm the name and enrol the voiceprint. |

Set `WATCH_MEETINGS` to `inbox` to receive the first transcript page when a granted meeting first becomes ready.
The note includes the page's recording warnings when present.
Use `wake` to also request a cycle. The default is `off`. The apps set it under **Watches** in the instance's
settings, beside the mail, calendar and Google Chat watches. Filing or refiling a ready meeting sends the note to
watching instances with a matching grant that have yet to receive it. This includes the unfiled grant.

A proposal pushes a notification to the operator's paired devices. In the apps, the meeting shows it beside its
speaker with **Confirm** and **Reject**. `shaft meetings proposals` lists the pending ones too.
[Meeting API](app-api.md#206-speaker-proposals)

## Account suggestion

When an unfiled meeting becomes ready or a speaker's name changes, SHAFT checks the recognised people's
other published, filed meetings. SHAFT excludes anyone identified on the microphone in an online meeting,
including in unpublished transcripts, because that channel records the operator's side.
Each person with history contributes one vote, divided among accounts in
proportion to their meeting counts. An account needs at least two thirds of the votes and two distinct filed
meetings. The suggestion leaves the meeting unfiled. Use `shaft meetings file <id> --suggested` to accept it.
Dismissing a suggestion suppresses that alias on later checks.

## Notifications to the operator

When a meeting is ready, the paired devices get a **Meeting ready** notification with the names Listen recognised,
how many voices still need a name, and whether the meeting is unfiled. For an unfiled meeting, the notification includes
the suggested alias when available. It includes `recording incomplete` if there are recording warnings. On failure, devices get **Meeting failed** with the reason. Tapping either notification
opens the meeting in **Meetings**. A failure is sent once, and a reprocess that ends sends again.

## Changing a speaker's name

A named speaker has a **Change** menu in the meeting: **Not <name>** names that speaker differently in this
meeting (its voice moves to the new person); **Rename <name> in every meeting** renames the person in the gallery
and every saved transcript; **Forget <name>'s voice** removes their voiceprints, keeping or anonymising the name in
past meetings (the same as `shaft speakers forget`).

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
| `shaft meetings grant <instance> <alias>` | Grants access to published transcripts filed under the account. |
| `shaft meetings revoke <instance> <alias>` | Revokes access to the account's transcripts. |
| `shaft meetings proposals` | Lists pending speaker name proposals. |
| `shaft meetings name <id> <label> <name>` | Enrols the label's voiceprint under the name. |
| `shaft speakers list` | Lists people and their gallery sample counts. |
| `shaft speakers rename <pid> <name>` | Renames a person in the gallery and saved transcripts. |
| `shaft speakers forget <pid> [--anonymise]` | Deletes the person's voiceprints. With `--anonymise`, replaces their name in saved transcripts. |

Each subcommand accepts `--json` after the subcommand name.
