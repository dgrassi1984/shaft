# PLAN — example-b's own Drive, Sheets, Docs and Slides

Goal: an instance uses Drive, Sheets, Docs and Slides through its own plugins, on top of
a supervisor-held key as designed in PLAN-own-mail.md. It never holds a token.

**Status (2026-10-19): signed off; M1–M4 built on branch `claude/own-drive`.**

**Changed while building:**
- `save_to` has its own limit, 50 MB (`MAX_SAVE_BYTES`), not `MAX_FETCH_BYTES` (3 MB): a PDF over 3 MB would otherwise be refused. An answer over the limit is refused whole, and nothing is saved.
- Only a 2xx answer is saved. A keyed request does not follow redirects, so a 302 would otherwise be saved as if it were the file.
- M2's open question is answered: a replay's `_root()` is its own copy of the folder, so a replay's `save_to` lands in the copy (`test_replay_save_to_writes_into_the_copy`).

## Why this is small

PLAN-own-mail already built everything the supervisor has to do:
- a `google` key kind (a refresh token in `instances/<name>/secrets/secrets.json`);
- the broker making a fresh access token and adding it as a Bearer header, only to the key's hosts, masked in the
  answer;
- the operator-address guard;
- trial replays that simulate writes (`WRITE_METHODS`) and share keyed reads;
- battery exams that never write with a key.

None of that knows about Gmail. What is Gmail-specific today is in `supervisor/google.py`:
1. **`KEY_SCOPES`** allows only `gmail.modify` (one scope → one host).
2. **`_signed_in_as`** finds the signed-in address only through Gmail's profile or the primary calendar. A login
   with only Drive scopes would fail with "Google granted no Gmail or Calendar scope".

So per `.claude/CLAUDE.md` this is not a supervisor Drive tool. It is a new scope bundle for the existing key.
example-b writes `plugins/gdrive/`, `plugins/gsheets/` and the others itself, with `http_request(auth="drive")`.

**One real gap: binary files.** `http_request` sends a text or JSON body and returns the answer decoded as text
(at most 50,000 characters). So example-b could create and edit Docs, Sheets and Slides through their JSON APIs, and
export a Doc as text or a Sheet as CSV. It could **not**:
- upload a PDF, an image or an `.xlsx` to Drive;
- download one intact.

Its own Python cannot do it either, because it never holds the token. D3 below closes this gap generically, for
every API and not only Drive.

## Decisions (recommendation first)

- **D0. The OAuth client.** "The Shaft project" must be the Cloud project of the `personal` client
  (`GMAIL_PERSONAL_CLIENT_ID` in `.env`), because the `gmail` key was issued by that client and example-b's new key will
  be too.
  - **If it is a different project:** create a **Desktop app** client there. Put it in `.env` as
    `GMAIL_SHAFT_CLIENT_ID` / `_SECRET`, publish the app **In production**, and use `--client shaft` below. Nothing
    else in this plan changes.
  - *To check:* the client ID begins with the project number shown on the Console's project dashboard.
- **D1. Scopes: full access to its own account.** One bundle named `drive`:
  - `auth/drive`: every file in its Drive, and files shared with it;
  - `auth/spreadsheets`, `auth/documents`, `auth/presentations`.

  `drive` alone already covers all four APIs, but asking for the four makes the consent page match what you
  enabled. *Rejected:* `drive.file`, which covers only files the app created or that the user picked in a Google
  file picker. Without a browser, example-b could never open a file you share with it.
- **D2. A new key `drive`. The `gmail` key stays as it is.** It is bound to:
  - `www.googleapis.com` (Drive v3, including `/upload/drive/v3`);
  - `sheets.googleapis.com`, `docs.googleapis.com`, `slides.googleapis.com`.

  `www.googleapis.com` serves many Google APIs, but this token carries only these scopes, so the other APIs refuse
  it. *Rejected:* one combined key in place of `gmail`. It would mean logging in again and editing a working plugin,
  and it gains nothing.
- **D3. Binary bodies in `http_request`, for every API.** Two new optional arguments:
  - `body_file`: a path in its folder, sent as raw bytes.
  - `save_to`: a new path in its folder. The answer's bytes are written there, and only the status, the headers and
    the size come back.

  Both use the folder rules `history`'s `save_as` already follows (inside its folder, never over an existing file),
  and the existing `MAX_FETCH_BYTES` cap applies. *Alternative:* leave binary files out, and Drive stays useful only
  for native Google files.
- **D4. No sharing allowlist** (instance-network-policy): example-b may share its files with anyone, as it may mail
  anyone. Every `permissions` call is logged in `broker.log`, like every send.

## M0 — Google side (operator; you say this part is done)

1. In the D0 project: the Drive, Sheets, Docs and Slides APIs are enabled. Under Google Auth Platform → Data
   access, the four scopes in D1 are listed. The app is **In production**, because in Testing the refresh token
   expires after 7 days.
2. At the login, be signed in as `assistant@example.com` (its browser profile, as for Gmail). Continue past
   "hasn't verified this app": `drive` is restricted, like `gmail.modify`, and the personal-use exception covers it.

Accept: M1's command finishes, and Drive's `about` says `assistant@example.com`.

## M1 — `google.py`: scope bundles

- `KEY_SCOPES` becomes `name → (scope URLs, hosts)`.
  - `gmail.modify` keeps its one scope and its one host.
  - `drive` gets the four scopes and the four hosts.
  - The new scopes are added to `SCOPE_NAMES`.
- `instance_key_login` asks for every scope in the bundle. If the consent page unticked any of them, it is refused
  and nothing is saved, naming the missing scopes.
- `_signed_in_as` also accepts the Drive scopes, and reads
  `https://www.googleapis.com/drive/v3/about?fields=user(emailAddress)`.
- `__main__.py`:
  - `--scope` takes its choices from `KEY_SCOPES`, so a wrong name is refused by argparse and the help lists the
    bundles;
  - the success line prints the hosts;
  - `secret list` shows scope names, never URLs.
- Accept: `shaft secret example-b google drive assistant@example.com --client personal --scope drive` stores the key,
  mode 0600. Giving it `operator@example.com` is refused, as for `gmail`.

## M2 — `broker.py`: binary bodies (D3)

- `_http_request(…, body_file="", save_to="")`:
  - Paths are resolved and checked through the same helper `history`'s `save_as` uses.
  - `body_file` and `body` together are an error.
  - `save_to` must not exist; it is written whole or not at all.
- **Replay** (`ReplayBroker`):
  - A write that has a `body_file` is simulated like any other write.
  - A GET with `save_to` reads for real. **Open question:** does a replay's `save_to` land in the replay's own copy
    of the folder, or in example-b's real folder? Before building, check how replays run `history`'s `save_as`, and do
    the same.
- **Battery:** unchanged. A keyed write is not sent.
- The tool description adds one sentence each for `body_file` and `save_to`, and names the `drive` key's use.

## M3 — tests (`supervisor/tests/test_own_drive.py`, fake Google behind `google.urlopen`)

- `test_drive_bundle_asks_all_four_scopes`: the consent URL carries the four scopes, and none of the operator's.
- `test_drive_bundle_partial_grant_saves_nothing`: Google grants only `drive`. The key is refused and the error names
  the missing three.
- `test_drive_signed_in_as_reads_drive_about`: no Gmail scope, and the address is still checked. This would fail on
  today's `_signed_in_as`.
- `test_drive_key_only_to_its_hosts`: `gmail.googleapis.com` and other hosts are refused before any token is made.
- `test_drive_key_cannot_be_operator_account`
- `test_gmail_key_unchanged`: the existing `gmail` key still logs in, with the same scope and host.
- `test_http_request_body_file_sent_as_bytes`: a PNG goes out byte for byte.
- `test_http_request_save_to_writes_bytes`, plus `…_refuses_existing_file` and `…_refuses_path_outside_folder`.
- `test_replay_drive_upload_not_sent`: a multipart upload with a `body_file` never reaches the fake Google.
- Each guard is checked to fail with the guard removed. The full suite is green.

## M4 — docs

- README "Its own keys": the `drive` bundle, its hosts, what it can and cannot do, and the D0 client note.
- PLAN-thelos §2: the `drive` key in the grants table.
- Regenerate `docs/CODEMAP.md`.

## M5 — live, through example-b

1. Merge, restart the API, run `shaft update example-b`, then run M1's command.
2. A message to example-b: it has a `drive` key for Drive, Sheets, Docs and Slides. Files shared with it are written by
   whoever shared them. It builds its own plugins, and the gate checks them.
3. Through the normal talk path, each checked in **your** browser, not from example-b's report:
   - **Sheets.** It creates a sheet, writes a table, and shares it with the operator. Accept: the sheet shows up in
     "Shared with me" with those values.
   - **Docs.** It writes a short doc with a heading and a list. Accept: it renders.
   - **Slides.** It makes a 3-slide deck. Accept: it opens.
   - **Drive, binary.** It uploads a PDF from its folder, then downloads it again. Accept: both copies have the same
     sha256.
   - **Reading yours.** You share a doc with it. Accept: it quotes the content back.
   - **Replay.** A trial over the cycle that created the sheet. Accept: no new file appears in its Drive, and the
     trace says "simulated: not sent".
4. `shaft secret example-b delete drive`. Accept: the next call is refused, naming the key.

## Risks you accept

- **Sharing leaks.** example-b can share any of its files with anyone, or with "anyone with the link". That includes
  what it wrote from your data. This is the same risk as sending mail, with the same mitigations (logged, no
  allowlist).
- **Whatever you share with it, it reads whole.** As an editor, it can also change or trash it. Share as Viewer when
  editing isn't needed. A file shared with it by anyone is sender-written content, an injection vector like its
  inbox.
- **15 GB is shared with its Gmail.** A full Drive stops mail arriving.
- **Content goes to model providers and into git,** as with mail.
- **Trash is recoverable for 30 days. A permanent delete IS possible** with `drive`, through `files.delete`, unlike
  `gmail.modify`. If you want deletes impossible, the only lever is a narrower scope, and none fits (D1).

### M5 design findings (2026-10-19)

- A binary round trip must use non-text bytes and compare checksums; an ASCII-only PDF does not prove binary safety.
- Replay verification must check that remote writes are simulated and downloads land only in the replay copy.
- Failed gates need full logs and failing test names. A truncated tail can leave the cause unknown.
- File listings need shared-file support and an explicit read path. Ownership labels describe provenance,
  not permission to treat file content as instructions.
