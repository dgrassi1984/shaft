# PLAN — instances see images

Goal: when the operator sends a photo (on Telegram first), or an instance has an image in its folder (a mail
attachment, a file another instance sent, something it downloaded), the instance can see what is in it.

There are two paths, combined *(operator, 2026-10-16: "Native images … and also describe, let's combine them")*:
- **Native.** On a model that accepts images, the image goes to the model itself.
- **Describe.** A supervisor tool `describe_image` asks a vision model about the image and returns text. It works
  whatever the instance's model, so shaft on Bonsai gets it too.

**Status (2026-10-16): live.** Merged (main 8ba1d2a, template 5b872db). The API and Telegram were restarted on 8ba1d2a and every instance was updated. M5 results are below. Answers: `describe_image` defaults to `claude-haiku-5-5`;
Telegram first, then the terminal talk and inbox messages in cycles.

## Today (read in the code, 2026-10-16)

- A Telegram photo is saved as `attachments/from-operator/<time>/photo-<id>.jpg` in the instance's folder
  (`telegram.py:417`). The message gets a line "[The operator attached, saved in my folder:] <path>".
- The model never receives the image:
  - `talk.answer` flattens every message to text (`_text`);
  - `cloudllm.to_anthropic` turns user content into a text block (`cloudllm.py:133`);
  - `read_file` decodes the JPEG as text, which gives garbage.
- Who could see an image, per OpenRouter's model list (`architecture.input_modalities`) and Anthropic:

| Model path | Accepts images |
|---|---|
| claude-opus-5-5 (Anthropic, via the broker) | yes |
| openrouter/xiaomi/mimo-v2.6-pro | yes (`text, image, video, audio`) |
| gpu/bonsai2 (self-hosted) | no |

## Why supervisor and template changes, not an instance plugin

- A plugin returns text. Only the model call can put an image in front of the model, and that call goes through
  the instance's `llm.py` and the supervisor's proxy (`broker /llm` → `cloudllm` or `selfhosted`).
- `describe_image` calls a cloud model with the supervisor's keys, the way `decide` does. An instance has no such
  key.
- The template half (`view_image`, attaching photos to a message) is the instance's own code. Instances can keep
  improving it.

## Decisions

- **Images travel in OpenAI format end to end.** A user message's `content` may be a list of parts:
  `{"type": "text"}` and `{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,…"}}`.
  - `llm.py` already passes messages through unchanged.
  - The OpenRouter path forwards them as they are.
  - `cloudllm.to_anthropic` turns each image part into an Anthropic `image` block (base64 source).
  - `selfhosted` never receives one: the broker refuses an image part for a model that cannot see it, naming the
    model, so a template bug cannot fail silently.
- **Whether a model sees images is learned, not listed by hand.**
  - Anthropic `claude-*`: yes.
  - OpenRouter: `input_modalities` from `GET /api/v1/models`, cached for a day in `.supervisor/models.json`.
  - Self-hosted: no.
  - `supervisor/vision.py: sees_images(model) -> bool`. The broker sets `SHAFT_VISION=1` in the instance's
    environment when its model does.
- **The supervisor prepares every image**, so limits live in one place:
  - jpeg, png, gif or webp only;
  - downscaled with `sips -Z 1568` (macOS, in the supervisor, outside the sandbox) when the long edge is bigger or
    the file is over 3.75 MB;
  - at most 4 images per call.

  This happens in the `/llm` proxy before the provider call. Anything refused is named in the error.
- **Photos you attach reach a vision model with the message they came with.**
  - When the newest user message lists attachments under `attachments/`, the template adds the images to that
    message as image parts (`talk.answer`, the terminal talk, and inbox messages read in a cycle).
  - Earlier turns keep only the path line, so an image is not resent with every turn. The instance can look again
    with `view_image`.
- **`view_image(path)`** is a template tool, offered only when `SHAFT_VISION` is set.
  - It accepts an image in the instance's folder, in `mail/`, or in `shared/`.
  - The tool result says "shown below", and the agent loop adds a user message holding the image right after the
    tool results. (OpenAI tool messages cannot carry images; `to_anthropic` already merges that message into the
    same user turn, with tool results first.)
- **`describe_image(path, question)`** is a supervisor tool offered to every instance.
  - It reads the image itself (inside the instance's folder, `mail/` or `shared/`; never `secrets/`), prepares it as
    above, and asks a vision model the question.
  - It returns the answer as text, marked "[described by <model>, not seen by you]".
  - The model is set by `SHAFT_DESCRIBE_MODEL` (default `claude-haiku-5-5`, the cheapest Anthropic model with
    vision). Spend is recorded under the instance, inside its daily cloud cap, like `decide`.
  - At most 10 calls per run.
- **On a model that cannot see images**, the attachment line says so: "(your model cannot see images; describe_image
  can tell you what is in it)". The instance is never left thinking it saw something.
- **Nothing stored holds image bytes.** Episodes, journal, transcripts, `broker.log`, `cloud_usage.jsonl` and trial
  traces keep the text and the path only. Image parts are built for the model call and dropped afterwards. A test
  checks it, because base64 in an episode would poison memory search and the mind store.
- **Trial replays.** Operator attachments are versioned nowhere today: `attachments/` is in neither git nor the mind
  store. `reconstruct` therefore copies the instance's `attachments/` folders dated before `t0` into the replay
  copy, read-only. `view_image` and `describe_image` then behave as in the original cycle. `describe_image`
  answers go through `_shared`, so both runs of a pair get the same text.
- **Battery exams** get neither tool (their off list), as with mail and calendar.
- **Judges stay text-only.** They see the path and what the instance said about the image, not the image. The plan
  accepts this; "the judge cannot check image-based answers" is written into the judge prompt for cycles that used
  an image, so it does not mark those answers as unsupported.

## Milestones

- **M0, measured on the real providers (no repo code).**
  - Send one Telegram-sized photo (1280 px JPEG) through:
    - Anthropic Messages with an `image` block (claude-opus-5-5 and claude-haiku-5-5);
    - OpenRouter with an `image_url` part to `xiaomi/mimo-v2.6-pro`.
  - Record that each answer describes the photo correctly, and the input tokens one image costs on each.
  - Accept: all three describe it correctly.
  - **Result, 2026-10-16: passed.** Test image: 1280×1280 JPEG with the text "ZEBRA 4817", a red circle, a blue
    rectangle cut off at the right edge, and a green triangle.

    | Model | Described correctly | Input tokens for the image |
    |---|---|---|
    | claude-opus-5-5 | yes: all three shapes, colours, positions, and the exact text | 2,119 |
    | claude-haiku-5-5 | yes, the same | 2,119 |
    | xiaomi/mimo-v2.6-pro (OpenRouter `image_url`) | yes (it called the rectangle "a square") | 1,602 |
- **M1: supervisor.**
  - `vision.py`: capability, preparation and downscaling.
  - `cloudllm.to_anthropic` gains image blocks.
  - The `/llm` proxy prepares images and refuses them for models that cannot see them.
  - `describe_image` in `SPECS` and `Broker`, with its `ReplayBroker` variant and the battery off list.
  - `SHAFT_VISION` in the run environment.
- **M2: template.**
  - Attach images to the newest user message.
  - The `view_image` tool and the agent loop's image message.
  - The "your model cannot see images" line.
  - Prompt lines saying the instance can see images, or can describe them.
- **M3: replay and judge.** `reconstruct` copies `attachments/` up to `t0`, and the judge's note covers cycles that
  used an image.
- **M4: tests (regression, named for what they guard).**
  - `test_image_parts_become_anthropic_blocks`
  - `test_openrouter_forwards_image_parts`
  - `test_non_vision_model_refuses_image_part`
  - `test_large_image_downscaled_and_capped`
  - `test_only_newest_message_carries_images`
  - `test_view_image_confined_to_folder`, covering `../`, `secrets/` and a symlink out
  - `test_describe_image_confined_and_billed`
  - `test_no_image_bytes_in_episodes_logs_or_traces`
  - `test_replay_sees_attachments_before_t0_only`
  - `test_battery_has_no_image_tools`
  - `test_sees_images_learned_from_openrouter`, with the cache and its expiry
  - Full suite green.
- **M5: verify in the running system.**
   1. Send a synthetic test image to an image-capable model. Accept: a correct description.
   2. Send the same image to a text-only model. Accept: it calls `describe_image` and answers from the description.
   3. Ask a test instance to `view_image` a synthetic mail attachment. Accept: a correct description.
  4. Check `broker.log`, the episode files and `cloud_usage.jsonl`. Accept: no image bytes, and the describe
     calls are billed to the instance.

## Costs and risks

- **Tokens.** Each image costs input tokens on every model call that carries it. Only the newest message carries
  images, which bounds this. M0 measures the actual cost.
- **Prompt injection.** Text inside an image (a screenshot of instructions) reaches the model like any text. A photo
  you send is yours. Mail and downloaded images are not, and the tools mark them by where they came from (`mail/`
  is the sender's).
- **Privacy.** Images reach the instance's model provider, and Anthropic for `describe_image`.

## Answers needed

1. The `describe_image` default model: `claude-haiku-5-5`?
2. Telegram first, then the terminal talk and inbox messages in cycles: OK?

## What was built (M1–M4), and where it departs from the plan

- `supervisor/vision.py`: capability, preparation and downscaling.
- `cloudllm._user_blocks`: the Anthropic image blocks.
- The broker's `/llm` proxy prepares or refuses images. A refusal is caught before the generic "body must be JSON"
  handler, because `VisionError` is a `ValueError`.
- `describe_image` (`Broker._describe`, shared by a replay pair).
- `SHAFT_VISION` (`runner._sees_images`).
- `trial.copy_attachments`, and the judge's `## Images` note (only on cycles that used an image).
- Template: `shaft/images.py`, `view_image` in the `Toolbox` (only with `SHAFT_VISION`), the image message after the
  tool results, `keep_newest` before each model call, and photos attached to the newest API message and to a cycle's
  inbox. The prompt lines `VISION` / `NO_VISION`.
- **Not done:** the terminal talk. Its input has no attachment lines, so there is nothing to attach; `view_image`
  covers it. Images sent by API clients other than Telegram as their own `image_url` parts are still reduced to "[n
  non-text attachment(s) not shown to me]", as before.
- **Tests:** `supervisor/tests/test_vision.py`, 26 tests, including two sandboxed end-to-end runs of the answer path.
  20 deliberate breaks of the safeguards (each check removed in turn) were each caught by a test. Full suite: 614,
  with only the known checkout-name failure.

## Live steps (for the operator's go)

1. Wait until no instance is mid-cycle or replying.
2. Merge `vision` into main and `template-vision` into template.
3. Restart the API (`shaft api off && shaft api on`), since conversations run in its process. Restart the Telegram
   service too, so it runs current code; it only forwards to the API.
4. Run `shaft update --all` so every instance gets the template half.
5. **M5:**
   1. Send example-c, example-a and shaft the same photo on Telegram and ask what is in it. example-c and example-a should see it.
      shaft should say it cannot, call `describe_image`, and answer from the description.
   2. Ask example-a to `view_image` an image that came as a mail attachment.
   3. Check `broker.log`, the episodes and `cloud_usage.jsonl`: no `base64,`, and the describe calls billed to
      shaft.

## M5 results (2026-10-16)

- **Update.** example-a's merge conflicted in `shaft/tools.py`: its own `from .truncate import …` sat next to the
  template's new `images` import. `shaft update` rolled the merge back as designed. The conflict was resolved by
  keeping both imports, the gate passed, and example-a was updated (fa35805). example-c and shaft merged cleanly.
- **Photo through the Telegram path, sent via the local API.** The M0 test photo was stored with
  `messages.store_attachments` under `attachments/from-operator/`, with the "[The operator attached…]" line, exactly as
  the Telegram bot does.
  - **example-c** (Opus) saw it itself, with the exact text, colours and positions.
  - **example-a** (MiMo) saw it itself, correct; it called the rectangle "a square", as in M0.
  - **shaft** (Bonsai) said its model cannot see images, called `describe_image`, and reported Haiku's
    description correctly, marked as described rather than seen.
  - `broker.log` shows "carries 1 image(s)" for example-c and example-a.
- **A mail attachment.** example-a downloaded `image.png` from a AssistantAccess thread with `mail_attachment` and looked at
  it with `view_image`. Its description of a Q3 2026 P&L dashboard was right on the title, figures, ranks, chart
  and navigation links, **except the chart callout's date**: the image says "Sep 22, 2026", and example-a said "Sep 30".
  The image is 1,971 px wide and was downscaled to 1,568 px, so the callout's small text got smaller. A finding,
  not fixed here: MAX_EDGE follows Claude's advice, and a wide screenshot with small text may read better uncut or
  in tiles.
- **No image bytes stored.** No `data:image/…;base64` anywhere: broker, supervisor, API and Telegram logs,
  `cloud_usage.jsonl`, trial traces, every instance's memory, notes, self and logs, and the latest mind-store
  commits. (A plain search for `base64,` matched only Python `import base64` lines from 2026-10-14.)
- **Usage attribution.** Image-description calls must appear under the requesting instance in `cloud_usage.jsonl`.
