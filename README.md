# XLI

A terminal agent powered by **Grok**, with **xAI Collections** as the durable memory store. XLI ships with two complementary REPLs:

- **`xli code`** — project-scoped coding agent (read/write files, run tests, dispatch parallel workers, mandatory verification before declaring success)
- **`xli chat`** — persona-based conversational agent with persistent memory (each persona has its own Collection; old turns are searchable forever via RAG)

Plus a quick-launch ephemeral mode (`xli scratch`) and a local-only mode (`xli init --local`) for "Midnight Commander on steroids" file-management workflows in directories you don't want to upload.

It's a Claude-Code-shaped CLI built on the cheaper xAI stack, with:

- **Multi-key swarm** — orchestrator dispatches read-only worker agents in parallel, each on its own API key
- **Orchestrator/worker model split** — strong reasoning model for the main loop, fast/cheap model for workers
- **Configurable temperatures** — warmer for the orchestrator (creative planning), colder for workers (precise execution)
- **Streaming output with live Markdown rendering** — answers stream as they arrive; bold/code/headers render as the text accumulates
- **Tool result previews** — short dimmed receipts under each tool call (file head, match counts, last lines of output) so you see work happening
- **Plan mode** — investigate read-only, produce a numbered plan, approve to execute
- **Auto-syncing** — files on disk are the source of truth; changed files push to the collection at end of every turn
- **Cost tracking** — per-turn token + USD totals, broken out orchestrator vs workers; server-tool sub-calls absorbed
- **Hallucination guard** — flags responses that claim work was done with 0 tool calls, so you know to verify
- **xAI server-side tools** — `web_search`, `x_search`, `code_execute` (Python sandbox) callable as ordinary tools
- **Self-provisioning keys** — single management key in env, all chat keys auto-created and revocable
- **Auto-expiring keys** — chat keys default to 180-day expiry, rotatable in place

> **Status: alpha.** Works end-to-end against a real xAI account. Expect rough edges. File issues against this repo for any breakage.

> **Note on the rename:** the command formerly called `xli chat` is now `xli code` (it's a code agent, not a chat agent). `xli chat` is now the persona-based conversational REPL.

---

## Requirements

- **Python 3.11+**
- An **xAI account** with:
  - A **management API key** (created in the xAI console under Team Settings)
  - At least one team you have admin access to (auto-discovered)
- **Linux / macOS** (Windows untested)
- `openai>=1.50` is recommended (server-tool calls use the Responses API).

You do not need to manually create chat API keys — XLI will provision them.

---

## Install

```bash
git clone <your-repo-url> xli
cd xli
python3 -m venv venv
./venv/bin/pip install -e .
```

Symlink onto your `PATH`:

```bash
sudo ln -s "$(pwd)/venv/bin/xli" /usr/local/bin/xli
```

---

## First-time setup

### 1. Export your management key

```bash
export XAI_MANAGEMENT_API_KEY=xai-...your-management-key...
```

(Add to your shell rc.) The management key is the only privileged credential; never stored on disk by XLI.

### 2. Run `xli setup`

```bash
xli setup
```

Writes config, discovers your team_id, provisions 1 primary + 8 worker keys, sets 180-day expiration, auto-detects best models. See `xli setup --help` for tuning.

### 3. (Optional) Configure pricing

Edit `~/.config/xli/config.json` and add `pricing` with USD-per-million-token rates from your xAI dashboard:

```json
"pricing": {
  "grok-4.20-reasoning":          {"input_per_million": 5.00, "output_per_million": 15.00},
  "grok-4-1-fast-non-reasoning":  {"input_per_million": 0.10, "output_per_million": 0.40}
}
```

Without `pricing`, token counts still display; cost numbers are simply omitted (XLI never fabricates a price).

### 4. Verify

```bash
xli status
```

---

## The four modes

| Mode | Command | Use case |
|---|---|---|
| **Code agent** (project-scoped) | `xli code [TARGET]` | Working on a real project — files get uploaded, full RAG, mandatory verification. The "Claude Code"-style flow. |
| **Chat / personas** | `xli chat [NAME]` | Conversation with persistent memory. Each persona is a Collection-backed long-running conversation. Different personalities, mid-session switching. |
| **Local-only project** | `xli init --local [--snapshot]` | File management in a directory whose contents you don't want uploaded — PDFs, audio, archives, scanned docs, photo libraries, anything that isn't really "text to upload." No Collection, no sync. `--snapshot` caches a path+size index (every file, content type irrelevant) so the agent can grep paths instead of walking the live tree. |
| **Ephemeral scratch** | `xli scratch [NAME]` | One-off tasks in a fresh `~/.xli/scratch/<name>/` dir. Convenient for "rename these files", "find duplicates", quick experiments. |

---

## Code REPL — `xli code`

### Initialize a project

```bash
xli init                    # name = current dir basename
xli init my-app             # explicit name (becomes the collection label on xAI)
xli init my-app --path /some/other/dir
xli init --local            # no Collection, no upload, no sync (search_project disabled)
xli init --local --snapshot # local + cache path/size index at .xli/index.txt for fast grep
xli new my-app              # create the dir AND init it
```

`xli init` creates `.xli/` in the project root (containing `project.json` + `manifest.json`) and uploads every text file to a fresh xAI Collection. `.gitignore` is honored; you can add an `.xliignore` for extra patterns. Defaults skip `node_modules/`, `.next/`, `venv/`, `__pycache__/`, build outputs, and many more.

### Open the REPL

```bash
xli code                    # uses cwd
xli code my-app             # by registered project name (works from anywhere)
xli code /some/path         # by path
xli code --yolo             # auto-approve every bash command (use sparingly)
```

The REPL syncs on entry, then drops you in:

```
╭──────────────────────────────────────────────────────────╮
│ XLI v0.1.0  ·  my-app                                    │
│ orchestrator: grok-4.20-reasoning  ·  worker: grok-4-1-fast-non-reasoning │
│ collection: collection_abc123  ·  pool: 9 key(s)         │
│ type /help for slash commands                            │
╰──────────────────────────────────────────────────────────╯

›
```

Talk to it like Claude Code:

```
› read app.py and explain it
› refactor the auth module into separate files
› investigate every .py file in src/ in parallel and summarize each
```

After every turn that mutates files, dirty paths sync to the collection automatically.

### Code REPL — slash commands

| Command | Effect |
|---|---|
| `/help` | Show this list |
| `/exit`, `/quit` | Leave the REPL |
| `!<shell>` | Run a shell command **locally** (no chat turn, no tokens). Output streams to your terminal. Use for `clear`, `ls`, ad-hoc utilities. |
| `/sync` | Force a full sync now |
| `/reset` | Clear conversation history (keeps system prompt) |
| `/plan` | Enter plan mode — read-only investigation, produces a numbered plan |
| `/execute` | Exit plan mode and carry out the plan with full tools |
| `/cancel` | Exit plan mode without executing |
| `/cost` | Print pricing table + which active models are covered |
| `/models` | Show current orchestrator + worker models and temperatures |
| `/temp <0.0..2.0>` | Override orchestrator temperature for the next turn only |
| `/yolo` / `/safe` | Toggle bash confirmation gate |
| `/status` | Show project state (collection, pool, mode flags, plan/yolo status) |
| `/projects` | List registered projects (current marked ●) |

**Plan mode example:**

```
› /plan
plan mode ON — next turn will investigate read-only and produce a plan.

[plan] › refactor the auth module to use the new token format
... agent reads files, greps, then outputs a numbered plan ...

[plan] › /execute
plan approved — executing
... agent now has write_file/edit_file/bash and carries out the plan ...
```

### Tools available to the agent

**File ops (project-scoped):** `read_file`, `write_file`, `edit_file`, `list_dir`, `glob`, `grep`, `bash`

**RAG search:** `search_project` (hybrid retrieval over the project's Collection — disabled in `--local` mode)

**xAI server-side tools** (Responses API, sub-calls):
- `web_search` — Live web search via xAI Live Search; returns answer + citations
- `x_search` — Search posts on X (Twitter), with handle/date filters
- `code_execute` — Run Python in xAI's sandbox (NumPy/Pandas/Matplotlib/SciPy preinstalled)

**Worker dispatch:** `dispatch_subagent` — fire one or more read-only investigators in parallel, each on its own API key

---

## Chat REPL — `xli chat`

Persona-based conversation with persistent memory. Each persona is a Collection-backed XLI project under the hood — recent turns inline as history, older turns RAG-searchable forever.

### Persona lifecycle

```bash
xli chat --new bob          # create persona "bob"; opens $EDITOR on its prompt
xli chat bob                # start a session as bob
xli chat                    # most-recently-used persona (or auto-create "default")
xli chat --list             # all personas
xli chat --edit bob         # re-open prompt in $EDITOR
xli chat --delete bob       # delete prompt + state dir (with y/N confirm)
```

### Storage layout

- `~/.config/xli/personas/<name>.md` — the persona's system prompt (config — hand-editable)
- `~/.config/xli/personas/.last-used` — tracks the default persona for naked `xli chat`
- `~/.xli/chat/<name>/` — per-persona project root (Collection-backed)
- `~/.xli/chat/<name>/turns/<ts>.md` — one markdown file per conversation turn (synced to the Collection)

### Memory model

- **Short-term:** the most recent **20 turns** load inline as history at session start (free recall)
- **Long-term:** older turns stay on disk and in the persona's Collection, reachable via the `search_project` tool ("what did we talk about regarding the migration last week?")

The persona's system prompt is appended with a small fixed footer telling the model it has memory + the standard tools. Edit only the persona's voice; tool awareness is automatic.

### Chat REPL — slash commands

| Command | Effect |
|---|---|
| `/help` | Show this list |
| `/exit`, `/quit` | Leave the REPL |
| `!<shell>` | Run a shell command locally (no chat turn, no tokens) |
| `/persona <name>` | Switch persona (saves current state, loads new one's prompt + last-N turns) |
| `/personas` | List personas (current/last-used marked ●) |
| `/edit` | Open current persona's prompt in `$EDITOR` (takes effect next session) |
| `/forget` | Wipe current persona's transcript (with y/N confirm) — next sync propagates deletes to Collection |
| `/sync` | Sync turn-files to the Collection now |
| `/yolo` / `/safe` | Toggle bash confirmation gate |

---

## Scratch — `xli scratch`

Ephemeral local-only project under `~/.xli/scratch/<name-or-timestamp>/`, drops you straight into chat.

```bash
xli scratch                 # auto-named with timestamp
xli scratch foo             # named "foo"
xli scratch --no-chat       # just create the project, don't enter REPL
xli scratch --yolo          # pass --yolo to the REPL
```

For snapshotting an existing big directory whose contents shouldn't be uploaded (audio, video, photo libraries, PDFs, exes, archives, scanned docs, anything binary or private), don't use scratch — instead run `xli init --local --snapshot` directly inside that directory.

---

## Shell passthrough — `!cmd`

Both REPLs (`xli code` and `xli chat`) support `!<command>` for running shell commands **locally** without burning a chat turn. Example:

```
› !clear
› !ls -la
› !pwd
› !git log --oneline -5
```

Output streams straight to your terminal (escape codes pass through, so `clear` and color output work). No history change, no tokens spent. The model never sees these.

This is for utility commands you want to run yourself — when you want the agent to run something instead, just describe the task and let it use the `bash` tool with intent gating.

---

## Command reference

### Project lifecycle

| Command | What it does |
|---|---|
| `xli init [NAME] [--path PATH] [--collection-id ID] [--no-sync] [--force] [--local] [--snapshot]` | Initialize a project. `--local` skips Collection upload entirely; `--snapshot` caches a path/size index. |
| `xli new <NAME> [--path PATH]` | Create a directory + initialize it in one step. |
| `xli scratch [NAME] [--no-chat] [--yolo] [--force]` | Ephemeral local-only project under `~/.xli/scratch/`, drops into chat. |
| `xli sync [PATH] [--dry-run]` | Push local changes to the collection (auto-runs after every mutating turn). |
| `xli code [TARGET] [--yolo]` | Project-scoped code REPL. `TARGET` can be a path or registered project name. |
| `xli chat [NAME] [--new N \| --list \| --edit N \| --delete N] [--yolo]` | Persona-based conversation REPL with persistent memory. |
| `xli status [PATH]` | Show config, key pool, models, temperatures, project state, cost-tracking state. |
| `xli projects [FILTER]` | List every registered project; filter by substring. |

### Setup + key management

| Command | What it does |
|---|---|
| `xli config` | Write a config template if missing (chmod 600). |
| `xli setup [--workers N] [--expire-days N] [--force]` | One-shot first-time setup. Idempotent. |
| `xli bootstrap [--count N] [--prefix LABEL] [--expire-days N] [--force] [--revoke] [--yes]` | Lower-level: provision N keys with a label prefix, or `--revoke` to delete by prefix. |
| `xli keys list` | Show every chat key with days-until-expiration and status. |
| `xli keys rotate [--label LABEL]` | Rotate the secret on one (or all) keys; key ID stays the same. |
| `xli keys expire --days N [--label LABEL]` | Update the `expireTime` on one (or all) keys. |
| `xli keys revoke [--prefix LABEL] [--yes]` | Delete keys by label prefix (server-side + local config). |
| `xli models list` | Show models the team has access to. |
| `xli models recommended` | Heuristic best-of-class picks (no commit). |
| `xli models set [--orchestrator NAME] [--worker NAME]` | Pin orchestrator and/or worker model. |

### Housekeeping

| Command | What it does |
|---|---|
| `xli gc [--dry-run] [--yes]` | Find orphan xAI Collections (project deleted from disk) and offer to delete them. |
| `xli help` | Grouped command reference. |

---

## Configuration

### `~/.config/xli/config.json`

The single global config. `chmod 600` enforced. Example:

```json
{
  "_comment": "...",
  "orchestrator_model": "grok-4.20-reasoning",
  "worker_model": "grok-4-1-fast-non-reasoning",
  "model": "grok-4-1-fast-reasoning",
  "orchestrator_temperature": 0.7,
  "worker_temperature": 0.3,
  "team_id": "c13e6a5c-...",
  "keys": [
    {
      "api_key": "xai-...",
      "label": "primary-1",
      "api_key_id": "...",
      "expire_time": "2026-10-26T00:00:00Z"
    }
  ],
  "max_tool_iterations": 20,
  "max_worker_iterations": 10,
  "max_parallel_workers": 8,
  "max_file_bytes": 1000000,
  "pricing": {},
  "models_detected_at": "2026-04-29T00:00:00Z"
}
```

**Important: `management_api_key` is NOT in this file.** Read from `XAI_MANAGEMENT_API_KEY` env only.

`keys[0]` is always the primary (used for sync + main agent). Workers round-robin through the rest.

### `~/.config/xli/personas/<name>.md`

Persona system prompts (one file per persona). Hand-editable.

### `~/.config/xli/projects.json`

Global project registry. Auto-maintained by `xli init`.

### `<project>/.xli/`

Per-project state, created by `xli init`:

- `project.json` — collection ID (empty for `--local`), name, created timestamp, `local_only`, `extra_ignores`, `conversation_id` (xAI prompt-cache key)
- `manifest.json` — `relpath → {sha256, mtime, file_id, last_synced}` for diff-based sync
- `index.txt` — paths+sizes index when initialized with `--snapshot`
- `repl_history` — that project's REPL command history

### `<project>/.xliignore` (optional)

`.gitignore`-syntax extra patterns to skip during sync.

---

## Architecture

**Sync.** On startup the REPL walks the project (respecting `.gitignore` + `.xliignore`), diffs against the collection by sha256, and uploads/updates/removes deltas. Mutating operations fan out across `max_parallel_workers` threads with 429 backoff per op. Empty files and binaries are skipped automatically. After every turn that mutates files, dirty paths flush. Local disk is the source of truth.

**Streaming.** Orchestrator chat completions stream via `stream=True`. Content deltas render through a `rich.Live` widget that re-renders Markdown progressively (headers, bold, code blocks, lists). Tool calls stream silently — they materialize as discrete `→ tool_name` events with green/red badges and dimmed result previews. The user sees both the answer flowing AND the work happening.

**Swarm.** Tool calls in a single batch are classified as parallel-safe (reads, greps, search_project, web_search, x_search, code_execute, dispatch_subagent) or sequential (writes, edits, bash). Parallel-safe calls fan out via a thread pool. Each `dispatch_subagent` worker pulls a chat key from a round-robin pool, runs its own contained tool loop with **read-only tools only**, returns a tight summary. Workers cannot write, edit, or dispatch further workers.

**Server-side tools.** `web_search`, `x_search`, and `code_execute` are local function tools that internally fire one-shot Responses-API sub-calls (xAI moved these to Responses API only; Chat Completions Live Search is deprecated). Sub-call usage is tracked and absorbed into the turn's stats.

**Plan mode.** Tool list is restricted to read-only investigation tools, a preamble is injected. Agent investigates and outputs a concrete numbered plan. `/execute` toggles back and replays "Execute the plan above" — agent now has the full toolset.

**Hallucination guard.** After every turn, if the orchestrator's text contains a past-tense action verb (`verified`, `created`, `wrote`, `tested`, `installed`, `fixed`, etc.) but `tool_calls == 0`, a yellow `⚠ model said "X" but called 0 tools — verify before trusting` warning surfaces under the turn line.

**Cost.** Token usage from every completion is absorbed into per-turn stats. Server-tool sub-calls (Responses API) include their own usage which is added to the orchestrator's tally. If `pricing` is configured, USD cost is computed per call with the actual model used. Orchestrator and worker spend tracked separately.

**Personas.** Each persona is a Collection-backed XLI project at `~/.xli/chat/<name>/`. Recent turns load inline as history; older turns sync as `turns/<ts>.md` files and become RAG-searchable. Switching personas mid-chat saves the current state and reopens fresh on the new persona.

**Registry + GC.** `xli init` writes an entry to `~/.config/xli/projects.json` (path → collection_id). `xli gc` cross-references the registry against the cloud's collection list and your filesystem to identify orphans.

---

## Security model

**One privileged credential, env-only.** `XAI_MANAGEMENT_API_KEY` is read from the environment, never stored on disk. It can create new chat keys, rotate them, and manage your collections.

**Chat keys are scoped, expiring, rotatable.** Each key created by `xli setup` / `xli bootstrap` has an `expireTime` (default 180 days) and named ACLs (`api-key:model:*`, `api-key:endpoint:*`). All chat keys are revocable via `xli keys revoke` (server-side + local).

**Bash gating.** The agent's `bash` tool requires an honest `intent` declaration on every call (`read-only`, `modifies-project`, `modifies-system`, `network`). Riskier intents are gated on a y/N confirmation prompt unless `--yolo`/`/yolo` is set. Workers may only run `read-only` bash.

**File perms.** `~/.config/xli/config.json` is `chmod 600`. Persona prompts and transcripts inherit normal user perms.

**Public-release safety:**
- Don't paste real API keys into chat logs
- Rotate keys periodically (`xli keys rotate`)
- Default 180-day expiry caps the blast radius if config.json leaks

---

## Troubleshooting

**`xli setup` couldn't auto-detect models.** Some xAI accounts don't expose `/v1/models`. Workaround: `xli models set --orchestrator <name> --worker <name>`. Defaults work for most accounts.

**`web_search`, `x_search`, or `code_execute` errors with `'OpenAI' object has no attribute 'responses'`.** Your `openai` package is too old. Upgrade: `./venv/bin/pip install -U 'openai>=1.50'`.

**Sync fails with "Empty stream received".** Caused by 0-byte files. Already auto-skipped at the walk step. If you still see this, check whether the file size changed mid-sync.

**Sync uploaded files I didn't expect (e.g., `.next/` build output).** Update to the latest XLI — its default ignore list now covers `.next/`, `.nuxt/`, `.svelte-kit/`, `.turbo/`, `.vercel/`, `.astro/`, `out/`, `target/`, `.cache/`, `coverage/` plus the previous Python/Node defaults. Re-run `xli sync` and the build files will be deleted from the Collection automatically.

**Yellow `⚠ model said "created" but called 0 tools` warning.** The model claimed work without using any tools — likely a hallucination. Verify before trusting; ask follow-up questions to confirm the work actually happened.

**Worker output looks weird.** Workers return summaries inside a `--- worker[label] · model · iters · tokens ---` header; the headers can wrap on narrow terminals. Cosmetic only.

**Bootstrap throttled (429).** Retries with exponential backoff up to 5 times. If you're rapidly creating many keys, slow down or wait a few minutes.

**Markdown rendering looks weird mid-stream.** Code blocks render as plain text until ``` closes (then snap to highlighted). That's a quirk of streaming Markdown — no clean fix.

---

## File layout

```
xli/
  __init__.py
  __main__.py
  cli.py            REPLs (code + chat) + every subcommand
  agent.py          Agent + WorkerAgent + streaming + tool previews + parallel batch executor
  tools.py          Tool implementations + JSON schemas + classification sets
  server_tools.py   Wrappers around xAI Responses-API server tools (web_search, x_search, code_execute)
  sync.py           Project walker, manifest diff, parallel upload/update/remove with 429 backoff
  client.py         Clients factory (xai_sdk + openai → api.x.ai)
  pool.py           ClientPool (round-robin chat-key acquisition)
  config.py         GlobalConfig, ProjectConfig, KeyPair, template
  bootstrap.py      Management-API REST helpers (create/rotate/expire/revoke/discover/pick_best_models)
  cost.py           Cost + token formatters (no fabricated rates)
  manifest.py       Per-file sha256 / mtime / file_id record
  ignore.py         .gitignore + .xliignore + binary-skip walker
  registry.py       Global project registry
  persona.py        Persona file management for `xli chat`
  transcript.py     Per-turn conversation persistence for personas
```

---

## Uninstall / reset

```bash
# Local state
rm -rf ~/.config/xli/
rm -rf ~/.xli/                # personas, scratch projects, chat transcripts

# Per-project state (run in each project dir)
rm -rf .xli/

# Server-side: revoke every key XLI created
xli bootstrap --revoke --prefix worker --yes
xli bootstrap --revoke --prefix primary --yes
# (Delete xli-prefixed collections via the xAI dashboard, or with `xli gc`)
```

---

## License

(To be filled in based on your release intent — MIT and Apache-2.0 are common defaults; AGPL-3.0 if you want strong copyleft on hosted use.)
