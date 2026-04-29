# XLI

A terminal coding agent powered by **Grok** with **xAI Collections** as the project context store. Your project's files are mirrored to a private collection per project, so the agent always has full scope (via hybrid RAG search) without uploading the whole tree on every turn.

It's a Claude-Code-shaped CLI built on the cheaper xAI stack, with:

- **Multi-key swarm** — main orchestrator agent dispatches read-only worker agents in parallel, each on its own API key
- **Orchestrator/worker model split** — strong reasoning model for the main loop, fast/cheap model for workers
- **Plan mode** — investigate read-only, produce a numbered plan, approve to execute
- **Auto-syncing** — files on disk are the source of truth; changed files are pushed to the collection at end of every turn
- **Cost tracking** — per-turn token + USD totals, broken out orchestrator vs workers
- **Self-provisioning keys** — single management key in env, all chat keys auto-created and revocable
- **Auto-expiring keys** — chat keys default to 180-day expiry, rotatable in place

> **Status: alpha.** It works end-to-end against a real xAI account. You should expect rough edges. File issues against this repo for any breakage.

---

## Requirements

- **Python 3.11+**
- An **xAI account** with:
  - A **management API key** (created in the xAI console under Team Settings)
  - At least one team you have admin access to (auto-discovered)
- **Linux / macOS** (Windows untested)

You do not need to manually create chat API keys — XLI will provision them for you.

---

## Install

```bash
git clone <your-repo-url> xli
cd xli
python3 -m venv venv
./venv/bin/pip install -e .
```

The `xli` command is now available at `./venv/bin/xli`. Symlink it onto your `PATH` if you want it global:

```bash
sudo ln -s "$(pwd)/venv/bin/xli" /usr/local/bin/xli
```

---

## First-time setup (do this once)

### 1. Export your management key

The management key is the **only** privileged credential — it can create, rotate, and revoke other API keys, and it manages your collections. It is **never** stored on disk by XLI. Add this to your shell rc (`~/.bashrc`, `~/.zshrc`):

```bash
export XAI_MANAGEMENT_API_KEY=xai-...your-management-key...
```

Re-source your shell, then verify:

```bash
xli status
# should show "mgmt key: ✓ from env XAI_MANAGEMENT_API_KEY"
```

### 2. Run `xli setup`

```bash
xli setup
```

This single command:

- Writes a config template at `~/.config/xli/config.json` (chmod 600) if missing
- Auto-discovers your `team_id` and caches it
- Creates **1 primary chat key + 8 worker chat keys** via the xAI Management API (default; tune with `--workers N`)
- Sets a **180-day expiration** on each (tune with `--expire-days N`)
- Saves all chat keys to your config (revocable / rotatable later)
- Attempts to auto-detect the best orchestrator and worker models

Output:

```
✓ wrote config template at /home/you/.config/xli/config.json
· XAI_MANAGEMENT_API_KEY found in environment
✓ team_id discovered + cached: c13e6a5c-...
creating primary key (expires in 180d)…
  ✓ primary-1  →  xAI: xli-primary-1  (expires in 180d)
discovering available models on this team…
✓ auto-detected from N model(s):
    orchestrator: grok-4
    worker:       grok-4-1-fast-non-reasoning
creating 8 worker key(s) (expires in 180d)…
  ✓ worker-1  →  xAI: xli-worker-1  (expires in 180d)
  ...
setup complete — pool size: 9 key(s)
next: xli init in your project dir, or xli new <name> to start fresh
```

### 3. (If model auto-detection failed) Set models manually

Some xAI accounts don't expose `/v1/models`. If `xli setup` reports it couldn't auto-detect, set them yourself:

```bash
xli models set --orchestrator grok-4 --worker grok-4-1-fast-non-reasoning
```

You can pick any models your account has access to — see your xAI dashboard.

### 4. (Optional) Configure pricing for cost tracking

Edit `~/.config/xli/config.json` and add a `pricing` map with USD-per-million-tokens rates from your xAI dashboard:

```json
"pricing": {
  "grok-4":                       {"input_per_million": 5.00, "output_per_million": 15.00},
  "grok-4-1-fast-non-reasoning":  {"input_per_million": 0.10, "output_per_million": 0.40}
}
```

Without `pricing`, token counts still display; cost numbers are simply omitted (XLI never fabricates a price).

---

## Per-project workflow

### Initialize a project

`cd` into any directory you want the agent to work on, then:

```bash
xli init                    # name = current dir basename
xli init my-app             # explicit name (becomes the collection label on xAI)
xli init my-app --path /some/other/dir
```

Or create a brand-new project from anywhere:

```bash
xli new my-app              # creates ./my-app, inits with name=my-app
xli new my-app --path ~/Projects
```

This creates `.xli/` in the project root containing `project.json` (collection ID + name) and `manifest.json` (per-file sha256 + xAI file ID), then uploads every text file in the tree to a fresh xAI Collection. `.gitignore` is honored; you can add an `.xliignore` for extra patterns.

### Open the REPL

```bash
xli chat                    # uses cwd
xli chat my-app             # by registered project name (works from anywhere)
xli chat /some/path         # by path
```

The REPL syncs the project on entry (uploads anything that changed since last run), then drops you into an interactive prompt:

```
╭──────────────────────────────────────────────────────────╮
│ XLI v0.1.0  ·  my-app                                    │
│ orchestrator: grok-4  ·  worker: grok-4-1-fast-non-reasoning │
│ collection: collection_abc123  ·  pool: 9 key(s)         │
│ /exit · /sync · /reset · /plan · /execute · /cancel · /cost │
╰──────────────────────────────────────────────────────────╯

›
```

Talk to it like Claude Code:

```
› read app.py and explain it
› refactor the auth module into separate files
› investigate every .py file in src/ in parallel and summarize each
```

After every turn that mutates files, dirty paths are pushed to the collection automatically.

### REPL slash commands

| Command | Effect |
|---|---|
| `/exit`, `/quit` | leave the REPL |
| `/sync` | force a full sync now |
| `/reset` | clear conversation history (keeps system prompt) |
| `/plan` | enter plan mode — read-only investigation, produces a plan |
| `/execute` | exit plan mode and carry out the plan with full tools |
| `/cancel` | exit plan mode without executing |
| `/cost` | print the pricing table and which active models are covered |

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

---

## Command reference

### Project lifecycle

| Command | What it does |
|---|---|
| `xli init [NAME] [--path PATH] [--collection-id ID] [--no-sync] [--force]` | Initialize a project. `NAME` defaults to cwd basename; `--path` defaults to cwd. |
| `xli new <NAME> [--path PATH]` | Create a directory + initialize it in one step. |
| `xli sync [PATH] [--dry-run]` | Push local changes to the collection (auto-runs after every mutating turn). |
| `xli chat [TARGET]` | Start the REPL. `TARGET` can be a path or a registered project name. |
| `xli status [PATH]` | Show config, key pool, models, project state, cost-tracking state. |
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
| `xli models list` | Show models the team has access to (requires `/v1/models` working — not all accounts). |
| `xli models recommended` | Heuristic best-of-class picks (no commit). |
| `xli models set [--orchestrator NAME] [--worker NAME]` | Pin orchestrator and/or worker model. |

### Housekeeping

| Command | What it does |
|---|---|
| `xli gc [--dry-run] [--yes]` | Find orphan xAI Collections (project deleted from disk) and offer to delete them. |

---

## Configuration

Two files, one persistent state directory per project.

### `~/.config/xli/config.json`

The single global config. `chmod 600` enforced. Example:

```json
{
  "_comment": "...",
  "orchestrator_model": "grok-4",
  "worker_model": "grok-4-1-fast-non-reasoning",
  "model": "grok-4-1-fast-reasoning",
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

**Important: `management_api_key` is NOT in this file.** It's read from `XAI_MANAGEMENT_API_KEY` env var only.

`keys[0]` is always the primary (used for sync + main agent). Workers round-robin through the rest. Each entry can include an `api_key_id` (server-side ID, needed for rotate/expire) and `expire_time` (ISO timestamp).

### `~/.config/xli/projects.json`

The global registry. Auto-maintained by `xli init`. Used by `xli projects` and `xli chat <name>` resolution.

### `<project>/.xli/`

Per-project state, created by `xli init`:

- `project.json` — collection ID, name, created timestamp, optional `extra_ignores`
- `manifest.json` — `relpath → {sha256, mtime, file_id, last_synced}` for diff-based sync
- `repl_history` — that project's REPL command history

### `<project>/.xliignore` (optional)

`.gitignore`-syntax extra patterns to skip during sync. Defaults already exclude `.git/`, `.xli/`, `venv/`, `node_modules/`, `__pycache__/`, etc.

---

## Architecture

**Sync.** On startup, `xli chat` walks the project (respecting `.gitignore` + `.xliignore`), diffs against the collection by sha256, and uploads/updates/removes deltas. After every turn that mutates files, dirty paths are flushed in the same way. Local disk is the source of truth; the collection is a mirror.

**Swarm.** Tool calls in a single batch are classified as parallel-safe (reads, greps, search, worker dispatch) or sequential (writes, edits, bash). Parallel-safe calls fan out via a thread pool, capped by `max_parallel_workers`. Each `dispatch_subagent` worker pulls a chat key from a round-robin pool, runs its own contained tool loop with **read-only tools only**, and returns a tight summary. Workers cannot write, edit, or dispatch further workers.

**Plan mode.** When toggled on, the orchestrator's tool list is restricted to read-only investigation tools and a preamble is injected. The agent investigates and outputs a concrete numbered plan. `/execute` toggles plan mode off and replays "Execute the plan above" — the orchestrator now carries out the work with the full toolset.

**Cost.** Token usage from every completion is absorbed into the turn's stats. If `pricing` is configured, USD cost is computed as `(prompt_tokens / 1M) × in_rate + (completion_tokens / 1M) × out_rate`. Orchestrator and worker spend are tracked separately.

**Registry + GC.** `xli init` writes an entry to `~/.config/xli/projects.json` (path → collection_id). `xli gc` cross-references the registry against the cloud's collection list and your filesystem to identify orphans.

---

## Security model

**One privileged credential, env-only.** `XAI_MANAGEMENT_API_KEY` is read from the environment, never stored on disk. It can create new chat keys, rotate them, and manage your collections.

**Chat keys are scoped, expiring, rotatable.** Each key created by `xli setup` / `xli bootstrap` has an `expireTime` (default 180 days) and named ACLs (`api-key:model:*`, `api-key:endpoint:*`). All chat keys are revocable via `xli keys revoke` (server-side + local).

**Rotation without re-linking.** `xli keys rotate` calls the xAI rotation endpoint, which returns a new secret for the same `api_key_id`. The local config is updated in place. Nothing referencing the key needs to change.

**File perms.** `~/.config/xli/config.json` is `chmod 600`.

**Public-release safety:**
- Don't paste real API keys into chat logs (xAI auto-flags accounts when secrets appear in third-party model inputs)
- Rotate keys periodically (`xli keys rotate`)
- Default 180-day expiry caps the blast radius if config.json leaks

---

## Troubleshooting

**`xli setup` says it couldn't auto-detect models.** Some xAI accounts don't expose the `/v1/models` endpoint or reject chat keys for that endpoint specifically. Workaround: set models manually with `xli models set`. The defaults (`grok-4-1-fast-reasoning` + `grok-4-1-fast-non-reasoning`) work for most accounts.

**`xli sync` fails with "Unknown field 'xli_sha256'".** Your collection was created before XLI declared its metadata fields. The sync code falls back to no-fields update automatically (you'll see a warning); long-term, re-init the project for clean schema-aware sync:

```bash
rm -rf .xli/
xli init
xli gc                   # the old collection is now an orphan; delete it
```

**Bootstrap throttled (429).** `xli bootstrap` retries with exponential backoff up to 5 times. If you're rapidly creating many keys, slow down or wait a few minutes.

**Worker output looks wrapped weirdly.** Workers return summaries inside a header; the headers can wrap on narrow terminals. Cosmetic only.

**Worker keys keep failing chat completions.** Check `xli keys list` — they may have expired (180-day default). `xli keys rotate` mints fresh secrets for the same key IDs.

**Model auto-detection picks something weird.** It's a name-based heuristic. Override with `xli models set`.

---

## File layout

```
xli/
  __init__.py
  __main__.py
  cli.py            REPL + every subcommand (init/sync/status/chat/config/setup/bootstrap/keys/models/projects/new/gc)
  agent.py          Agent + WorkerAgent + parallel-safe tool batch executor
  tools.py          Tool implementations + JSON schemas + classification sets
  sync.py           Project walker, manifest diff, upload/update/remove with field fallback
  client.py         Clients factory (xai_sdk + openai → api.x.ai)
  pool.py           ClientPool (round-robin chat-key acquisition)
  config.py         GlobalConfig, ProjectConfig, KeyPair, template
  bootstrap.py      Management-API REST helpers (create/rotate/expire/revoke/discover)
  cost.py           Cost + token formatters (no fabricated rates)
  manifest.py       Per-file sha256 / mtime / file_id record
  ignore.py         .gitignore + .xliignore + binary-skip walker
  registry.py       Global project registry
```

---

## Uninstall / reset

To completely wipe XLI state and start fresh:

```bash
# Local state
rm -rf ~/.config/xli/
# Per-project state (run in each project dir)
rm -rf .xli/

# Server-side: revoke every key XLI created
xli bootstrap --revoke --prefix worker --yes
xli bootstrap --revoke --prefix primary --yes
# (You can also delete xli-prefixed collections via the xAI dashboard, or with `xli gc` if any registry entries remain)
```

---

## License

(To be filled in based on your release intent — MIT and Apache-2.0 are common defaults; AGPL-3.0 if you want strong copyleft on hosted use.)
