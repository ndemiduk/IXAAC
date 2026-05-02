# Proposal: `/2ndeye` — user-invoked cross-vendor critique

**Status:** Design discussion — nothing built yet.
**Audience:** A second agent (or future-self) reviewing this with the user.
**Goal:** Pin down a small, scoped, principled answer to "should iXaac support non-xAI providers?" — a question the user has previously answered "no" but with reservations. Spoiler: the answer is "yes, but as a tool, not a feature."

---

## Context (assume zero prior knowledge)

iXaac is **xAI-ecosystem-first** ([feedback_xai_ecosystem_first.md](../../.claude/projects/-home-birdman-Projects-iXaac/memory/feedback_xai_ecosystem_first.md)). Default to Grok models; non-Grok providers belong only as opt-in plugin seams. The user has been explicit about wanting to resist drift toward cross-vendor defaults.

This stance has been hard to reconcile with iXaac's other load-bearing thesis: **"vendor provides primitives; user composes the system"** (the curate-your-own philosophy at the heart of [README.md](../README.md)). If users compose their own systems, why aren't they free to compose with any model?

The reconciliation, settled 2026-05-02:

- **Default model selection is xAI-first.** Not optional.
- **Non-xAI invocation is allowed, but only as a scoped, user-invoked tool — never as a feature, never on the agent's tool palette, never auto-invoked.**

This proposal is the implementation of the second bullet.

## The motivating insight (the user's framing)

Two observations the user made that reframed my thinking:

1. **Other vendor ecosystems try to "sticky" you in.** OpenAI, Anthropic, Google all have observable incentives to keep users from looking elsewhere. xAI/Grok demonstrably does the opposite — Grok itself has recommended other AI tools to the user when they were the right fit for the task. This isn't marketing; it's a behavioral pattern of the model.

2. **A `/2ndeye` mechanism *expresses* Grok's truth-seeking values rather than betraying them.** If Grok is willing to recommend other tools, it would presumably welcome a workflow where the user can spot-check Grok's work against another model. Refusing to support that on principle would be more vendor-loyal than Grok itself.

The X-first identity is reinforced by contrast: iXaac being explicitly cross-vendor-friendly *only in the critique direction* is a feature of the X-first identity, not an exception to it.

---

## The shape

```
/2ndeye <question>            # bundles current conversation + question, sends to configured non-Grok model
/2ndeye --last 3 <question>   # just the last 3 turns
/2ndeye --since <mark>        # ties into /mark — review since a marked point
```

The reply prints inline as Markdown, attributed clearly:

```
[2ndeye · claude-sonnet-4-6]
> The diff in plugin.py looks correct, but there's an edge case
> where response_shape is None instead of empty string …
```

That's it. No tool, no agent palette entry, no auto-invocation. The user types `/2ndeye`, the user reads the response, the user decides what to do with it. The main agent doesn't even know it happened (the secondary call is out-of-band).

---

## Configuration

Add a `secondary_ai` block to `~/.config/xli/config.json`:

```json
"secondary_ai": {
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "api_key_env": "ANTHROPIC_API_KEY"
}
```

If the block is absent, `/2ndeye` errors with a setup hint pointing at this docs section. iXaac never auto-configures it; the user opts in by editing config.

API keys can be:
- Pulled from the named env var (simplest, current implementation pattern), OR
- Stored in the existing plugin vault under the `secondary_ai` key (preferred when the user wants secrets off-environment)

---

## Implementation discipline

This is a load-bearing constraint and the most likely thing future scope creep will erode:

- **One file:** `xli/secondary_ai.py` with a single `query(messages, question, *, scope=...) -> str` function.
- **One slash command:** `/2ndeye` and its argument variants. No companion CLI subcommand (no `xli 2ndeye` — the slash is the only entry point, because the use case is mid-conversation review, not standalone analysis).
- **One config block:** as above. NOT a multi-provider abstraction layer. NOT a "preferred model per task" registry.
- **No SDK dependencies:** OpenAI and Anthropic both expose chat-completions-shaped HTTP APIs. `urllib` only, mirroring [xli/tools/plugins/plugin_call.py](../xli/tools/plugins/plugin_call.py).
- **Backends as if-statements:** `if provider == "openai": ...` / `if provider == "anthropic": ...`. Two branches today; if a third gets added later, fine — but resist building a registry pattern for two providers.
- **Never on the agent's tool palette.** This is the line that protects xAI-first. The agent has zero ability to call the secondary model. Only the human user can.
- **Never auto-invoked.** Not from `/debug`, not from hooks, not from any post-turn validator. User types it; user gets it.

If a future PR proposes "wouldn't it be nice if the agent could call /2ndeye when it's uncertain?" — push back. That's the failure mode. The whole point of "tool not feature" is that the human is in the loop on every cross-vendor request.

---

## Privacy edge — make the disclosure visible

`/2ndeye` ships the conversation (or scoped slice) to a second vendor. For OSS work, fine. For private code, sensitive prompts, or proprietary context, the user is consenting to that exposure with each invocation.

The user knows this in the abstract. The question is whether the *moment of disclosure* is visible. Two complementary mechanisms:

1. **One-time-per-session warning.** First `/2ndeye` of a REPL session prints a yellow notice: `⚠ /2ndeye sends conversation to <provider>. Type /2ndeye-quiet to suppress this notice for this session.` Subsequent invocations are silent.

2. **`--confirm` flag for explicit opt-in mode.** A user who wants the warning every time (working with sensitive context) can configure `secondary_ai.always_confirm: true`, making `/2ndeye` always prompt `[y/N]` before sending.

These aren't paternalism — the user knows what `/2ndeye` does. They're visibility on a real boundary that's easy to forget when you're deep in a conversation.

---

## Connection to the verifier subagent sequencing

There's a three-step roadmap captured in [project_verifier_subagent.md](../../.claude/projects/-home-birdman-Projects-iXaac/memory/project_verifier_subagent.md):

1. **`/debug`** — same-model fresh-context critic. Shipped per memory.
2. **`/2ndeye`** — manual cross-model critic. **This proposal.**
3. **Auto-triggered cross-model verification** — a future state where iXaac can decide on its own to run a verification pass after risky operations.

`/2ndeye` is the middle step deliberately. It ships the cross-model critique value without committing to auto-trigger until the user has used it enough to trust the pattern. If `/2ndeye` proves consistently useful and consistently low-noise, step 3 is a natural extension. If it turns out to be noisy or rarely useful, step 3 stays deferred and the design is no worse for it.

The "tool not feature" discipline of `/2ndeye` is what makes step 3 even thinkable later — without that scoping, step 3 would be an unbounded "any model can call any other model anytime" surface.

---

## Build phases

1. **Config schema.** Add `secondary_ai` block to [xli/config.py](../xli/config.py). Defaults: empty (feature off until configured).
2. **`xli/secondary_ai.py`.** Single `query(messages, question, scope=None) -> str`. Two backend branches (openai, anthropic). urllib only. Returns plain text.
3. **`/2ndeye` slash command.** REPL handler in [xli/cli/slash_commands.py](../xli/cli/slash_commands.py). Parse args (`--last N`, `--since <mark>`), assemble the message slice, call `query`, print result with attribution header.
4. **One-time-per-session warning.** Tracked on the REPL session object (in-memory bool); printed before the first `/2ndeye` call only.
5. **Cost tracking.** Surface tokens/$ for the secondary call in the same per-turn cost line iXaac already prints (with provider attribution: `2ndeye: 1.2k tok · ~$0.003`). Reuse [xli/cost.py](../xli/cost.py) where possible.
6. **`--confirm` flag + always_confirm config.** Optional opt-in for users who want explicit per-call gating.

Phases 1–3 are MVP. 4–5 are quality-of-life. 6 is for the privacy-conscious workflow.

---

## Open questions

1. **Should `/2ndeye` see the system prompt + tool definitions, or just user-visible turns?** Two arguments:
   - Including the system prompt gives the second model full context — better critiques, but also exposes iXaac's internal prompt engineering to the second vendor.
   - Excluding it keeps internals internal but means the second model is reviewing turns out of context; it might miss why the agent did what it did.
   Recommendation: **exclude by default, include behind `--full-context` flag.** Most critiques are about what the user can see; full-context is for when the user is auditing iXaac's behavior itself.

2. **Result archiving.** `/2ndeye` responses are inline only — they print and that's it. Should there be a `--save` flag that writes the response to a file (e.g., `.xli/2ndeye/<ts>.md`)? Useful for "I want to compare three models on the same question." Probably yes, but defer until requested.

3. **Multiple secondary providers configured at once.** A user might want both Anthropic and OpenAI configured and pick per call: `/2ndeye --provider openai <question>`. Easy to add but expands the surface. Recommendation: **defer.** Single configured secondary covers the use case; if the user wants comparison across providers, they reconfigure or run two REPLs.

4. **Streaming.** Current cross-vendor APIs all support streaming. Should `/2ndeye` stream the response to the terminal as it arrives (matching iXaac's main streaming behavior), or just print the final block? Recommendation: **stream**, for consistency with the main REPL UX. Implementation cost is small with both vendors.

5. **What's the exact prompt the second model sees?** Probably something like:
   ```
   You are reviewing a conversation between a user and another AI assistant.
   Your role is to provide a second-opinion critique.
   The user's question for you is: <question>
   The conversation:
   <messages>
   ```
   Worth iterating on after first use, but a simple framing like this should ship.

6. **Should `/2ndeye` itself be governed by a model-capability check?** If the secondary model is text-only and the conversation includes tool results that are huge / binary / images, send semantically degrades. Probably not worth solving up front; let it be an empirical issue.

---

## What this is *not*

- **Not a multi-provider model selection layer.** `xli models set --orchestrator gpt-4o` is not part of this proposal and never should be. Orchestrator and worker stay xAI. Only `/2ndeye` reaches outside.
- **Not a fallback when xAI is down.** Reliability isn't the use case; critique is.
- **Not an evaluator of agent quality.** It's a tool the user reaches for in a specific moment, not a metric pipeline.
- **Not an integration point for cross-vendor agentic workflows.** No "have Claude do this part and Grok do that part" orchestration. That's a different product.

---

## For agents reading this cold

1. The decision record for this design is in [project_2ndeye_design.md](../../.claude/projects/-home-birdman-Projects-iXaac/memory/project_2ndeye_design.md). The xAI-first principle that constrains this is in [feedback_xai_ecosystem_first.md](../../.claude/projects/-home-birdman-Projects-iXaac/memory/feedback_xai_ecosystem_first.md).
2. The "tool not feature" framing is **the load-bearing constraint**. If a future change proposes giving the agent any way to invoke `/2ndeye` itself, push back. That's not polish — it's the failure mode the constraint exists to prevent.
3. Don't generalize this into "iXaac supports any model." The slash command is the only entry point. Defaults stay xAI everywhere else.
4. The privacy disclosure is intentional surface, not vestigial. Don't strip it as cleanup; the moment-of-disclosure visibility is a real UX property.
5. iXaac is actively self-modifying ([project_self_maintenance_active.md](../../.claude/projects/-home-birdman-Projects-iXaac/memory/project_self_maintenance_active.md)). When an agent runs `/2ndeye` against the codebase it's editing, the second model is reviewing iXaac's own changes — a natural fit for the self-maintenance loop, but worth being explicit that this is allowed and intended.
