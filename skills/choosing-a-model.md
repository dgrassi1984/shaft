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

def run(question: str, model: str = "openrouter/z-ai/glm-5.3") -> str:
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
- Refusals tell me why: not on the menu, no qualifying provider, over my allowance. A scheduled job, a trial
  replay or a battery exam cannot consult.
- `record(kind="consults")` lists my consults of the last 7 days with their cost. `self/stats.md` shows how
  consulted cycles fared once I have some.

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
  feeling sure is not evidence that it won't. What decides is what the subtask needs: a hard proof or design to a
  strong model; a summary, extraction or format conversion to a cheap one.
- **Cheap first where a check exists.** When I can test the answer (code that runs, a fact I can look up), a cheap
  model plus my check often beats an expensive model alone.
- **Pace the day.** The allowance is for the whole day. One consult per cycle at most, unless the goal is worth
  more, keeps money for later goals.

## More allowance

If the allowance is the real limit on a goal worth more, I ask the operator with `send_message`. I say what I used
it for, what it achieved (cycles, verdicts, the cost per met goal from stats.md), and how much more I want. The
operator decides (`shaft limits <me> consult-cap <usd>`). I never route around the allowance.
