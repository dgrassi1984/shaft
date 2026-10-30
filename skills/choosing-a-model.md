# Consulting another model for a bounded subtask (my consult allowance, the menu, how to decide)

I think with my own model. For one bounded piece of work I may ask another model, paid from my **consult
allowance**. That allowance is $3 a day unless the operator set mine otherwise; it is separate from my cloud and
coder budgets. The operational envelope shows what is left and the menu with prices ($ per million input/output
tokens). A model marked unavailable has no provider that keeps no data; calls to it are refused.

## How

The supervisor gives me the transport, not the policy. Choosing when, which model and what to send is mine, built
the way I build any tool: a plugin (skills/writing-plugins.md). In a plugin:

```python
from shaft import broker

def run(question: str, model: str) -> str:
    reply = broker.consult(model, [{"role": "user", "content": question}], max_tokens=2000, caller="ask_model")
    answer = reply["choices"][0]["message"]["content"]
    cost = reply["shaft"]
    return f"{answer}\n\n[{model}: ${cost['usd']:.4f}; consult left ${cost['cap_usd'] - cost['spent_usd']:.2f}]"
```

- `broker.consult(model, messages, max_tokens=None, caller="", **options)` posts to
  `/llm/consult/v1/chat/completions`. The reply is OpenAI-style plus a `shaft` block with what the call cost.
- `max_tokens` defaults to 8000 and cannot go higher. The supervisor reserves the worst case before the call
  (prompt plus `max_tokens` at the model's price), so a large `max_tokens` blocks more of my allowance while the
  call runs.
- A plugin is stopped after 120 s. A slow model writing 8000 tokens can take longer, and a call cut off is still
  paid. I keep `max_tokens` to what the answer needs, and use the fast models for quick questions.
- Text in, text out. An image, audio or file is refused, because its cost cannot be reserved in advance; I send
  what it says instead. A `reasoning` budget is capped at `max_tokens`.
- Refusals tell me why: not on the menu, no qualifying provider, over my allowance. A scheduled job, a trial
  replay or a battery exam cannot consult.
- `record(kind="consults")` lists my consults of the last 7 days with their cost. `self/stats.md` shows how
  consulted cycles fared once I have some.

## A cheaper model is rarely cheaper than doing it myself

The menu's prices are not the comparison that matters. What matters is what this subtask costs on a helper
against what it costs my own model, done inline.

- **My own model already holds most of my context, cached.** Re-reading it costs a small fraction of the input
  price (on a cloud model, about nine tenths of what I read is cached; new tool results and cache misses pay
  full price). A helper usually starts cold: everything I send it is billed at its full input price, and its
  answer then comes back into my context, where my own model reads it too.
- **The menu shows list input and output prices, nothing else.** It does not show cache prices, and it does not
  show that some models always think before answering: their thinking is billed as output, often many times the
  answer's length.
- **The arithmetic:** a helper is cheaper only if the whole consult costs less than doing the work myself. The
  consult is tokens sent × its input price + (its thinking + its answer) × its output price, plus what my own
  model spends writing the question, reading the answer and checking it. Prices as of 2026-10-30, $ per million
  input/output/cached input: MiMo-V2.6-Pro, a main cloud model, 0.435/0.87/0.0036; GLM-5.3 0.07/1.40/0.069;
  MiniMax M3.1 Flash 0.15/0.60/0.015. GLM-5.3 and MiniMax Flash always think, at the highest effort unless told
  otherwise, and rank about level with MiMo on general benchmarks: they are peers at a different price shape,
  not a lower tier. Against MiMo, both are cheaper for reading a large block of new text; MiniMax Flash is also
  cheaper per token written, but only while its thinking stays short (a `reasoning` effort of `low` helps).
  GLM-5.3 writes at a higher price than MiMo.
- **On the local model my own thinking costs $0.** Every consult costs more than doing it myself. What it buys is
  ability, not savings.

So saving money is a weak reason to consult. The two strong ones are:

- **Ability:** a bounded subtask my model gets wrong or cannot do (a hard proof, a design choice, a diagnosis
  that keeps failing) goes to a stronger model.
- **Independence:** before I act on a high-stakes claim, a model from another family can review it. Its review
  tells me what to check; it is not the check.

Recurring work that deterministic code can do is cheaper still as a plugin or a job: it pays no model at all on
each run.

## When it is worth it (what is known)

- **One model per bounded subtask, never per turn.** Each switch resends the context and loses the prompt cache,
  so per-turn switching costs more than it saves.
- **Send only what the subtask needs.** My whole memory does not go to another model: the question, the evidence
  it needs, and the answer format I want.
- **Check the answer before I act on it.** Another model's claim is evidence, not truth: I verify it against the
  record or a tool result the way I verify my own (skills/checking-a-fact.md). Two models agreeing is not a check.
- **Escalate a failed subtask once.** If a subtask failed its check, I may retry it once with a stronger model,
  keeping what already worked. I do not keep asking until I like the answer.
- **My own confidence is not a gate.** Feeling unsure is not evidence that a stronger model will do better, and
  feeling sure is not evidence that it won't. What decides is what the subtask needs and what my record shows:
  a kind of subtask my model has failed at before goes to a stronger one.
- **The cheapest helper that is good enough, where a check exists.** Once a consult is worth it (for ability,
  independence or a real saving), and I can test the answer (code that runs, a fact I can look up), a cheaper
  model plus my check often beats an expensive model alone. A consult made to save money must also pass the
  arithmetic above.
- **Pace the day.** The allowance is for the whole day. One consult per cycle at most, unless the goal is worth
  more, keeps money for later goals.

## More allowance

If the allowance is the real limit on a goal worth more, I ask the operator with `send_message`. I say what I used
it for, what it achieved (cycles, verdicts, the cost per met goal from stats.md), and how much more I want. The
operator decides (`shaft limits <me> consult-cap <usd>`). I never route around the allowance.
