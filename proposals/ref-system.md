# Proposal: Three Knowledge Layers for XLI — `/ref`, `/doc`, `/get`

**Status:** Design discussion — nothing built yet.
**Audience:** A second agent reviewing this with the user.
**Goal of the discussion:** Pressure-test the architecture before code. Specific open questions are at the bottom.

---

## Context (assume zero prior knowledge)

XLI is a terminal coding/chat agent built on top of Grok (xAI) with xAI Collections as the durable memory store. It already has:

- A project-scoped code agent (`xli code`) with read/write/bash tools, parallel worker dispatch, plan mode, and RAG search over the project's collection.
- A persona-based chat agent (`xli chat`) where each persona is a Collection-backed long-running conversation with persistent memory.
- Three xAI server-side tools: `web_search`, `x_search`, `code_execute` (sandboxed Python), accessed via the Responses API as one-shot sub-calls.

The user wants three new affordances that share an architectural pattern (markdown files + opt-in subscription + agent-readable) but serve **distinct purposes**:

| Slash | Type | Storage | Purpose |
|---|---|---|---|
| `/ref <persona>` | **Memory** | `~/.xli/chat/<persona>/` (already exists) | Pull a chat persona's recall into the current session — cross-pollinate code work with prior planning conversations |
| `/doc <name>` | **Knowledge** | `~/.config/xli/docs/<name>.md` | Attach framework rules, project guides, design specs, CLAUDE.md-style static context |
| `/get <intent>` | **Capability — invocation** | `~/.config/xli/plugins/<id>.md` | Live data fetch — agent matches the user's intent against subscribed plugins and invokes the right one (e.g. `/get the weather` → finds `openweather` plugin → calls it) |
| `/lib ...` | **Capability — management** | same as above | Curate the plugin library: `/lib add` (wizard), `/lib list`, `/lib search <q>`, `/lib subscribe <id>`, `/lib remove <id>` |

This proposal focuses primarily on **`/get` (the plugin layer)** because that's the largest design surface. `/ref` is small (just attaching a persona's collection_id to `search_project`'s collection list). `/doc` is small (just markdown injected into the system prompt or surfaced as a sub-collection). The plugin layer is where the meat is.

Two existing projects on disk are perfect seed material for `/get`:

1. **`~/argus-byo/providers/plugins/`** — 94 self-describing JS plugins, all using the same `ProviderRegistry.register({...})` shape. Each declares: `id`, `name`, `description`, `categories`, `authType`, `configFields`, `endpoints`, and an `async query(config, params)` function. Categories span government/compliance (SEC EDGAR, CourtListener, OFAC sanctions, FARA, OpenSecrets, ~15 total), finance (FRED, Yahoo Finance, Alpha Vantage, ~8), news feeds (~12), social (Bluesky, Mastodon, Reddit, Twitter, ~17), cloud storage (~8), OCR (3), satellite imagery, blockchain — almost entirely **read-only research APIs**.

2. **`~/x-follower-scanner/` (xflo)** — A Chrome extension using the X v2 API with OAuth2 PKCE + Bearer token. Currently does follower analysis. With proper scopes the same auth flow could add **DM read/write, tweet posting, search**. This is **credentialed write access** — a category argus-byo doesn't really cover.

## The proposal in one sentence

**Each external API/service becomes a markdown file the agent can semantically search and act on**; bash + code_execute do the actual HTTP calls. No new processes, no MCP servers, no installation friction. The plugin lib grows with user-curated knowledge files.

## How `/get` works

**Storage:**
- `~/.config/xli/plugins/<id>.md` — plugin definitions (config; hand-editable; shareable)
- `~/.config/xli/auth/<id>.env` — `KEY=value` credentials per plugin (separate so plugins are shareable without secrets)
- Per-project subscription: `<project>/.xli/plugins.txt` lists active plugin IDs (keeps the agent's mental scope bounded — 94 plugins in scope is too noisy for one task)
- Per-persona subscription: `~/.xli/chat/<name>/.xli/plugins.txt`

**Each plugin file contains:**
- YAML frontmatter: `id`, `name`, `description`, `categories`, `auth_type`, `risk` (see below)
- Markdown body: prose description, endpoint table, auth flow notes, gotchas, example calls
- A code block with the original `query()` JS body from argus-byo (or equivalent) — *reference example*, not executable

**Two new agent tools when plugins are subscribed:**
- `plugin_search(intent)` — semantic search over active plugins by name/description/category. Returns top matches as one-liners. **Triggered by `/get <intent>` slash**, but also auto-callable by the agent when the user mentions external data.
- `plugin_get(name)` — pulls the full markdown for one plugin into the agent's context.

The agent reads the plugin doc, then composes a `bash` call (curl) or `code_execute` snippet (Python `requests`) to actually hit the API.

**CLI plumbing:** `xli plugin list / add / remove / import`. The `import` flow points at a directory (e.g., `xli plugin import ~/argus-byo/providers/plugins/`), parses each plugin (regex on the `register({...})` block — every argus-byo plugin uses the same shape), and writes one markdown file per connector.

## How `/ref <persona>` works (smaller scope)

`xli chat` already creates one Collection per persona at `~/.xli/chat/<name>/`. The Collection has a `collection_id`. `xli code`'s `search_project` tool already accepts a `collection_ids` list — it just defaults to `[project.collection_id]`.

`/ref bob` in `xli code` adds `bob`'s collection_id to the active list, so `search_project` now RAGs over both the current project AND bob's conversation history. `/ref` (no arg) lists what's currently attached. `/unref bob` removes it.

Implementation: ~30 lines. No new storage, no new tools. The whole feature is a session-level mutation of the search_project tool's parameters.

## How `/doc <name>` works (smaller scope)

Markdown files at `~/.config/xli/docs/<name>.md`. Two delivery modes:

1. **Inline injection (small docs):** the doc's content is appended to the agent's system prompt at session start. Good for ≤10kB rule docs ("you must always run pytest after edits", "use httpx not requests in this codebase", etc.).
2. **Collection-backed (big docs):** large reference material (a framework's full docs, a long spec) is uploaded once to a per-doc Collection and exposed via `search_project` like personas. Same pattern.

`xli doc list / new / edit / delete` for management. Per-project/persona subscription via `<project>/.xli/docs.txt`.

This is essentially **shared CLAUDE.md files** — but versioned and reusable across projects.

## The four design decisions (mostly about plugins)

### 1. Invocation depth — three tiers, ship in order

- **L1 (read-and-bash, ship first):** agent reads the plugin doc, composes `curl` from scratch. Zero new infrastructure — works with the existing `bash` tool today.
- **L2 (templated):** each plugin has a `## Usage` section with parameterized commands (`curl ... ${VAR}`). Agent fills params and bashes. Less hallucination on call shape.
- **L3 (executable):** a new `plugin_call(name, params)` tool that takes structured params and makes the HTTP call. Effectively a markdown-defined MCP server. Cleanest, biggest schema commitment.

### 2. Auth model — encrypted vault, keyring-backed by default

**All credentials live in a single encrypted vault**, not plaintext .env files. Plaintext is a backup-leak, git-commit, screen-share footgun.

- **Storage:** `~/.config/xli/vault.enc` — one Fernet-encrypted (AES-128-CBC + HMAC) JSON keyed by plugin id: `{"openweather": {"OPENWEATHER_KEY": "..."}, "google-drive": {"CLIENT_ID": "...", "REFRESH_TOKEN": "..."}}`.
- **Master key (default):** stored in OS keyring via Python's `keyring` library — macOS Keychain, Linux Secret Service, Windows Credential Locker. Zero user-managed key files.
- **Master key (fallback):** for headless / SSH / CI environments where keyring isn't available, read from `XLI_VAULT_KEY` env var, or from `~/.config/xli/.vault-key` (chmod 400). One of these must be set or XLI refuses to decrypt.
- **CLI:** `xli auth set <plugin> KEY=value`, `xli auth show <plugin>` (with confirm prompt), `xli auth clear <plugin>`, `xli auth list <plugin>`.
- **Wizard integration:** when `/lib add` or `xli plugin add` runs, credential prompts write straight into the encrypted vault. User never sees a plaintext .env.
- **Plugin invocation flow:** XLI decrypts in-memory, exports the relevant keys as env vars for the subprocess running curl, returns. Plaintext only exists during the call.

**Threat coverage:** ✅ disk theft, ✅ backup leak, ✅ accidental git commit, ⚠️ master-key compromise (mitigated by OS keyring), ⚠️ process memory during call (unavoidable, same as any credential use), ⚠️ prompt injection (mitigated by existing bash-intent gating).

OAuth plugins (gdrive, github, gmail, slack, xflo) need refresh-token rotation in addition to static secrets. Open question — see open question #2 below.

### 3. Subscription model

Plugins are global. Projects/personas opt in. The agent only sees subscribed plugins unless told otherwise (an "all plugins" override is a future debate).

### Anti-hallucination guardrails (design rule, not optional)

The biggest risk with `/get` isn't slow plugins or auth complexity — it's the model **fabricating plugin output**. The agent might claim "I called the weather plugin and it returned 72°F" when it never invoked anything, or hallucinate a plugin name that doesn't exist. This is unacceptable. If a plugin doesn't exist or fails, the user must know.

Four layered defenses:

**1. Structural — `plugin_search` returns explicit empty state.**
When the user types `/get the weather` and `plugin_search` finds no match, the tool response is a hard-coded marker the agent must surface:
```
NO_PLUGIN_MATCH for intent="the weather"
Categories of subscribed plugins: news, finance, government
Suggest: install a weather plugin, use web_search, or write one with `xli plugin --new weather`
```
That's the only "result" the agent gets — not silence it can spin into fiction.

**2. System prompt — explicit anti-fabrication rule.**
When plugins are subscribed, the prompt gains:
> "You have plugin_search and plugin_get tools. **Never fabricate plugin output.** If plugin_search returns NO_PLUGIN_MATCH, say no plugin was found. If a plugin call fails or returns an error, surface the error — do not synthesize what the response 'should have been.' Plugin output only comes from real tool results."

**3. Detection — extend the existing 0-tools-claim warning.**
XLI already flags responses that claim work was done with `tool_calls == 0`. Extend it: track `plugin_invocations: int` in TurnStats. When the response mentions plugin-style data ("the API returned", "weather is X°", "search found N results") but `plugin_invocations == 0`, surface a yellow warning. False positives tolerable — same trade-off as the existing detector.

**4. Generalized — same rule for `/ref` and `/doc`.**
If the agent says "according to your react-rules doc, you should X" but no doc was attached this session, that's fabrication. The subscription state is queryable; cross-check claims against attached refs/docs.

**This is a hard design rule.** A plugin layer that lies is worse than no plugin layer at all.

### 4. Risk axis (critical for write-enabled plugins)

Plugins declare a `risk:` field in frontmatter:

- **`risk: low`** — read-only public APIs (sec-edgar, courtlistener, gdelt, news feeds, weather, geocoding). Agent calls freely.
- **`risk: medium`** — credentialed reads, rate-limited writes (S3 GET, gist create, Dropbox upload). One-line summary before execution.
- **`risk: high`** — public-facing writes (post tweet, send DM, push to main, send email, S3 DELETE). **Gated like bash's `modifies-system` intent** — confirmation prompt per call, narrated intent before execution. Worst categories don't auto-approve even in YOLO mode.

This reuses XLI's existing bash-intent gating machinery, which already prompts users for `modifies-system` and `network` operations.

## Scale considerations (the catalog will reach the hundreds)

argus-byo alone is 94 plugins. xflo adds one. Real-world growth: a few hundred plugins within a year as the user adds connectors for any service they touch. The architecture has to hold at that scale, which forces a few specific design choices:

**1. Subscription is mandatory, not optional.** At 300 plugins the agent **cannot** have the full catalog in scope per turn — retrieval gets noisy, the agent mis-routes, and prompt context bloats. Default subscription is empty; users opt in 5–20 plugins per project/persona based on what that work actually needs. There is **no "all plugins available" mode** because it's worse, not better, at scale.

**2. `plugin_search` becomes RAG over the catalog, not grep.**
- Sub-200 plugins: keyword grep over frontmatter descriptions is fine.
- 200+ plugins: build a per-user "plugin catalog" xAI Collection that holds the descriptions of every installed plugin (not subscribed — installed). `plugin_search(intent)` hits that Collection for semantic matching. Two-step: (a) RAG returns top candidates from the global catalog, (b) the user can subscribe a candidate to make it usable in this project.

**3. Top-K with scores, not auto-pick.** Even with semantic search, "weather" might semi-match a `weather-stations-fda` (food-safety) plugin. `plugin_search` returns top 3-5 candidates with scores and one-line descriptions; the agent reads them and picks — or asks the user when matches are weak. **Auto-picking the top-1 silently is a fabrication risk** (agent confidently uses the wrong plugin). System prompt: "If plugin_search returns multiple candidates with low scores or ambiguous fit, ask the user which one — don't guess."

**4. Categories matter for human browsing.** `xli plugin list` at scale is unusable as a flat list. Group by frontmatter `categories:` field. `xli plugin list --category finance` to browse. `xli plugin list --installed` to see only what you actually have.

**5. Subscribed sets are themselves curation work.** Maintaining "this code project's active plugins" becomes its own activity. Worth a `xli plugin profile <name>` concept later — preset subscription bundles ("data-journalism": sec-edgar + courtlistener + opensanctions + propublica-nonprofits + ...).

**Why this matters for hallucination prevention:** at 300 plugins, the model is more likely to *think* a plugin exists for almost anything ("of course there's a plugin for X"), and the temptation to fabricate output gets stronger. Every guardrail in the previous section becomes more important, not less. Combined with strict subscription, the surface area where fabrication can happen stays small even as the catalog grows.

**Speed model — pay per use, not per availability.** The agent's tool palette gains exactly **two** entries (`plugin_search`, `plugin_get`) regardless of catalog size — ~200 tokens of schema, fixed. The catalog itself lives on disk + one searchable Collection at scale; nothing about it enters the model's context until the agent invokes a search for a specific intent. A plugin invocation is ~1k tokens of context (top-K candidates + chosen plugin's doc) + the actual API call. Compared to MCP — which loads every server's tools into context every turn whether they're used or not — this design has zero overhead for unused plugins. **A 300-plugin catalog costs the same per turn as a 5-plugin catalog, when the agent doesn't reach for one.**

## Authoring path — making this real for non-developers

The argus-byo bulk import is great for seeding 94 connectors in one shot, but the everyday case is **adding one plugin in 30-60 seconds**. Almost every plugin reduces to a curl call with auth + params, so an interactive wizard is feasible:

```
$ xli plugin add
  id: openweather
  name: OpenWeatherMap
  description: Current weather by city or coords
  category: weather
  risk: low
  url: https://api.openweathermap.org/data/2.5/weather?q={city}&appid=${OPENWEATHER_KEY}
  auth type: query
  env var: OPENWEATHER_KEY
  parameter: city (string, required, "City name e.g. 'Seattle, US'")

✓ wrote ~/.config/xli/plugins/openweather.md
✓ created ~/.config/xli/auth/openweather.env
✓ subscribe to current project? y
```

Three entry paths coexist:

- **Wizard** (`xli plugin add`) — the everyday path; one plugin at a time, interactive
- **Bulk import** (`xli plugin import <dir>`) — one-shot conversion from existing catalogs (argus-byo, OpenAPI specs, postman collections later)
- **Editor template** (`xli plugin add --template`) — opens `$EDITOR` on starter markdown for users who'd rather hand-write

Both path-types output the same markdown shape, so they're interchangeable downstream. Hand-edit a wizard-generated plugin if you want; convert a hand-written plugin to wizard format with no migration needed.

**Why this matters for adoption:** the plugin layer is only as alive as the catalog. If adding a plugin is "fork the repo, write JS, run a test harness, send a PR" then the catalog stays small. If it's "answer 6 prompts, paste an API key" then users build their own catalogs the same way they build their own bash aliases. That's the difference between MCP-as-product and plugins-as-personal-toolkit.

## Why this beats MCP for the user's workflow

MCP servers are heavyweight (one process per server), professionally curated (you use what's published), and a pain to manage at count. The user's argus-byo + xflo work shows they already build their own connectors — they want a system that scales with that habit.

Markdown plugins are: trivial to add, trivial to edit, trivial to share, and the agent reads them in-band. **Breadth scales with curiosity, not infrastructure.**

The trade-off: MCP gives stronger schema guarantees (RPC over a well-typed contract); markdown plugins depend on the agent reading correctly. For an LLM-first tool that's an acceptable trade.

---

## Open questions for the discussion

These are the decision points where I want a second opinion before code is written:

1. **Is L3 worth the schema commitment?** L1 (read-and-bash) plus L2 (templated curl) covers most use cases. L3 (`plugin_call(name, params)`) is the cleanest UX but it's effectively rewriting MCP in markdown. Argument for L3: less hallucination on call shape, structured retry semantics, native pagination. Argument against: you've reinvented an RPC protocol and now have to maintain it.

2. **Auth granularity.** `<plugin-id>.env` is per-plugin. But OAuth flows (xflo) need a token store with refresh. Do we add a `plugin_oauth(<plugin>)` flow that handles PKCE + token refresh in-band, or is that out of scope for v1? If out of scope: how does the user get a fresh xflo token without leaving the agent?

3. **Argus-byo bulk import — what does the parser look like?** Three options: (a) regex on the `register({...})` block (works because every plugin uses the exact same shape; brittle to formatter changes), (b) actual JS AST via Node subprocess (clean but requires Node), (c) have argus-byo project itself emit a JSON manifest (cleanest but requires touching argus-byo). The user prefers self-contained imports.

4. **High-risk gating granularity.** Per-call confirmation gets annoying fast for chatty plugins (e.g., posting a thread of 5 tweets in sequence = 5 prompts). Alternatives: session-level "I trust this plugin for the next N calls" toggle, or "trust this plugin for any call matching shape X". What's the right escape hatch that doesn't trade safety for UX?

5. **Plugin versioning.** When SEC EDGAR adds a new endpoint or X v2 deprecates one, how does the plugin get updated without breaking subscriptions? Add a `version:` frontmatter field + a `xli plugin upgrade` command? Or just `git pull` whatever plugin source the user trusts?

6. **Discoverability vs noise.** With 94 plugins from argus-byo plus future imports, `plugin_search` could return a lot. Should plugins have hand-curated `tags:` for filtering? Should `plugin_search` use a per-project Collection of plugin metadata for semantic search? Or is keyword grep over frontmatter enough?

7. **`/get` vs implicit invocation.** Should the user always type `/get <intent>` to trigger plugin search, or should the agent auto-call `plugin_search` whenever the user mentions external data ("what's the weather in NYC?" → agent calls plugin_search internally → finds weather plugin → uses it, no slash needed)? Implicit is more "alive" but harder to debug; explicit is predictable but more typing.

8. **Three layers, three tools, or one tool?** The agent currently has `search_project`. With this proposal it might gain `plugin_search` and `plugin_get`, plus implicit collection-attach for `/ref`, plus doc-aware system prompts for `/doc`. Should these be three orthogonal tool sets, or unified under one `knowledge_search(query, type=memory|knowledge|capability)`? Cleaner but more abstract.

9. **The "alive" framing — is the user overstating it?** The user's claim is this could make XLI feel "more alive than Claude Code or Codex" because it's not bound to a fixed tool palette. Is that real, or is the perceived breadth just "the model writes more curl commands"? Where's the meaningful capability delta vs. just letting an agent loose with `web_search` + `bash`?

---

## What I'd do differently if I were starting today

The biggest risk is **scope creep into rebuilding MCP in markdown for the plugin layer**. L1 is very cheap and could be shipped this week. L3 is months of design + maintenance to get right. Recommend: ship L1 + the bulk argus-byo import, see whether the user actually uses it for non-trivial workflows, *then* decide on L2/L3 based on observed pain points.

Second-biggest risk: high-risk write-enabled plugins. The xflo/X-API case (DMs + tweets) is high-stakes enough that getting the gate UX wrong has real consequences. Worth a separate small spike on the gating UX before any high-risk plugin ships.

Third: build `/ref` and `/doc` first because they're tiny (each is ≤50 lines), they share the architectural pattern, and shipping them validates the whole "knowledge layer" framing before we commit to the heavier plugin work.
