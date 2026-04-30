# iXaac

*Internal name: `xli` (the binary, the package, the on-disk directory). User-facing brand: **iXaac**. The codebase uses `xli`. Every command in this README starts with `xli` — that's what you type.*

A **personal AI substrate** — not a coding agent, not a chatbot, not an MCP-style platform. A terminal program built on **xAI's Grok** and **xAI Collections** that grows with what you curate: personas, reference docs, API plugins, multi-machine fabric. The model and primitives are commodities; **your curation is the load-bearing layer.**

> **Status: alpha.** Works end-to-end against a real xAI account. Expect rough edges. The architecture is settled; specific features are still in flux.

---

## Why this exists

There's a category gap in the AI tooling landscape:

- **Claude Code, Codex, Cursor** are vendor-curated coding agents. The tool palette is what the vendor ships, extended via MCP servers (heavyweight, professionally maintained, not yours).
- **ChatGPT, Claude.ai, Grok app** are vendor-hosted SaaS chatbots. Memory features exist but are vendor-controlled. No file ops, no multi-machine, no curation.
- **Frameworks like LangChain** are developer toolkits to *build* one product. Not a shell you live in.
- **Self-hosted RAG apps (Khoj, MemGPT)** focus on memory. Usually one machine, one purpose.

iXaac sits in the unoccupied gap: **a substrate that rewards investment**. Like Emacs, but for AI tooling. You write a few personas, a few reference docs, a few plugins for APIs you actually use, and the system becomes uniquely yours. Power users will love it; casual users will bounce — and that's fine.

The thesis in one sentence:

> **Vendor provides primitives; user composes the system.**

---

## Headline capabilities

- **Two complementary REPLs.** `xli code` for project work (read/write files, run tests, parallel workers, mandatory verification). `xli chat` for persona-based conversation with persistent memory.
- **Multi-machine fabric (XMPP + OMEMO + Tailscale).** Your phone becomes a thin client to your home iXaac, not to OpenAI/Anthropic/xAI cloud chat apps. Send OMEMO-encrypted notifications outbound (`xmpp_send`); receive commands and run agent turns inbound (`xli daemon --xmpp`). Verb dispatch + agent fallback on a JID whitelist; full project context, your tools, your plugins. See [Multi-machine fabric](#multi-machine-fabric).
- **Workspace registry.** Every directory `xli` runs in is auto-tracked in `~/.config/xli/workspaces.json` with a `kind` (`project` vs `snapshot`) and an optional alias. Used by the daemon to route phone messages to the right project, and by humans via `xli workspaces`.
- **The knowledge layer — four slash commands working in concert:**
  - `/ref <persona>` — attach another persona's memory to the current session (cross-session recall)
  - `/doc <name>` — attach a reference doc into the system prompt (rules, conventions, specs)
  - `/get <intent>` — find and invoke a subscribed plugin matching a natural-language intent
  - `/lib ...` — manage the plugin library (list, subscribe, unsubscribe, remove)
- **User-curated plugins.** Each plugin is a markdown file describing an API. Write one in 60 seconds; subscribe to it from any project. No MCP server to spawn.
- **Multi-key swarm.** Self-provisioning chat keys; orchestrator dispatches read-only worker agents in parallel, each on its own key.
- **Streaming with live Markdown rendering.** Headers, bold, code blocks, lists render as text accumulates.
- **Tool result previews.** 1–3 dimmed lines under each tool call (file head, match counts, last lines of output) so you see work happening.
- **Hallucination guard.** Yellow warning when the model claims work was done but called zero tools.
- **Plan mode.** Read-only investigation → numbered plan → approve → execute.
- **xAI server-side tools as first-class.** `web_search`, `x_search`, `code_execute` (Python sandbox) callable as ordinary tools.
- **Local-only mode + path snapshots.** "Midnight Commander on steroids" for directories you don't want uploaded — NAS, photo libraries, PDFs, archives. `--snapshot` caches a paths-and-sizes index for fast structural search.
- **Auto-syncing.** Files on disk are the source of truth; changed files push to the Collection at end of every turn.
- **Cost tracking.** Per-turn token + USD totals, broken out orchestrator vs workers; server-tool sub-calls absorbed.
- **Self-managing credentials.** One management key in env, all chat keys auto-created, auto-expiring (180 days), rotatable in place.
- **Configurable temperatures.** Warmer for orchestrator (creative planning), colder for workers (precise execution). One-shot `/temp` override.
- **Rich slash command surface.** `/help`, `/status`, `/sync`, `/reset`, `/plan`, `/execute`, `/cancel`, `/cost`, `/yolo`, `/safe`, `/models`, `/temp`, `/projects`, `/persona`, `/personas`, `/edit`, `/forget`, `/ref`, `/unref`, `/doc`, `/undoc`, `/lib`, `/get`, `/exit`, `!<shell>`.

---

## Requirements

- **Python 3.11+**
- **Linux / macOS** (Windows untested)
- An **xAI account** with:
  - A **management API key** (created in the xAI console under Team Settings)
  - At least one team you have admin access to (auto-discovered)
- `openai>=1.50` recommended (server-tool calls use the Responses API)

You do **not** need to manually create chat API keys — iXaac provisions them for you.

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

(Add to your shell rc.) The management key is the **only** privileged credential — it can create, rotate, and revoke other API keys, and it manages your collections. **Never stored on disk by iXaac.**

### 2. Run `xli setup`

```bash
xli setup
```

This single command:

- Writes a config template at `~/.config/xli/config.json` (chmod 600) if missing
- Auto-discovers your `team_id` and caches it
- Creates **1 primary chat key + 8 worker chat keys** via the xAI Management API (default; tune with `--workers N`)
- Sets a **180-day expiration** on each key
- Saves all chat keys to your config (revocable / rotatable later)
- Auto-detects the best orchestrator and worker models

### 3. (Optional) Configure pricing

Edit `~/.config/xli/config.json` and add `pricing` with USD-per-million-token rates from your xAI dashboard:

```json
"pricing": {
  "grok-4.20-reasoning":          {"input_per_million": 5.00, "output_per_million": 15.00},
  "grok-4-1-fast-non-reasoning":  {"input_per_million": 0.10, "output_per_million": 0.40}
}
```

Without `pricing`, token counts still display; cost numbers are simply omitted (iXaac never fabricates a price).

### 4. Verify

```bash
xli status
```

Should show: management key found, team_id cached, 9 chat keys in pool, models configured.

---

## The four operational modes

| Mode | Command | Use case |
|---|---|---|
| **Code agent** (project-scoped) | `xli code [TARGET]` | Working on a real project — files get uploaded, full RAG, mandatory verification. The "Claude Code"-style flow. |
| **Chat / personas** | `xli chat [NAME]` | Conversation with persistent memory. Each persona is a Collection-backed long-running conversation. Different personalities, mid-session switching. |
| **Local-only project** | `xli init --local [--snapshot]` | File management in a directory whose contents you don't want uploaded — PDFs, audio, archives, scanned docs, photo libraries, anything binary or private. No Collection, no sync. `--snapshot` caches a paths+sizes index. |
| **Ephemeral scratch** | `xli scratch [NAME]` | One-off tasks in a fresh `~/.xli/scratch/<name>/` dir. Convenient for "rename these files", "find duplicates", quick experiments. |

---

## Code REPL — `xli code`

### Initialize a project

```bash
xli init                    # name = current dir basename
xli init my-app             # explicit name (becomes the collection label on xAI)
xli init my-app --path /some/other/dir
xli init --local            # no Collection, no upload, no sync (search_project disabled)
xli init --local --snapshot # local + cache path/size index at .xli/index.txt
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
| `/reset` | Clear conversation history (keeps system prompt + attached docs) |
| `/plan` | Enter plan mode — read-only investigation, produces a numbered plan |
| `/execute` | Exit plan mode and carry out the plan with full tools |
| `/cancel` | Exit plan mode without executing |
| `/cost` | Print pricing table + which active models are covered |
| `/models` | Show current orchestrator + worker models and temperatures |
| `/temp <0.0..2.0>` | Override orchestrator temperature for the next turn only |
| `/yolo` / `/safe` | Toggle bash confirmation gate |
| `/ref [persona]` | Attach a persona's memory to `search_project` (no arg = list) |
| `/unref <persona>` | Detach a previously-attached persona |
| `/doc [name]` | Attach a reference doc into the system prompt (no arg = list) |
| `/undoc <name>` | Detach a previously-attached doc |
| `/lib [...]` | Plugin library: list / all / subscribe / unsubscribe / remove |
| `/get <intent>` | Find + invoke a subscribed plugin matching the intent |
| `/status` | Show project state (collection, pool, mode flags, attached refs/docs) |
| `/projects` | List registered projects (current marked ●) |

The prompt prefix shows attachments at a glance:
- `›` — nothing attached
- `+1d ›` — 1 doc attached
- `+1r ›` — 1 ref attached
- `+1r/2d ›` — 1 ref + 2 docs
- `[plan] +1r ›` — combine with mode tags

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

All the `/help`, `/exit`, `!<shell>`, `/yolo`, `/safe`, `/ref`, `/unref`, `/doc`, `/undoc`, `/lib`, `/get`, `/status`, `/sync` commands work here. Plus persona-specific:

| Command | Effect |
|---|---|
| `/persona <name>` | Switch persona (saves current state, loads new one's prompt + last-N turns) |
| `/personas` | List personas (current/last-used marked ●) |
| `/edit` | Open current persona's prompt in `$EDITOR` (takes effect next session) |
| `/forget` | Wipe current persona's transcript (with y/N confirm) |

### Model behavior caveat

**Reasoning models (`grok-4.20-0309-reasoning`, etc.) are weaker at following persona/style rules** than non-reasoning models like `grok-4`. The reasoning chain can rationalize past system-prompt rules. For persona-driven chat where vibe matters more than deep reasoning, `xli models set --orchestrator grok-4` is the right call. Reasoning models for complex code/research; non-reasoning for direction-following.

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

## The knowledge layer — `/ref`, `/doc`, `/get`, `/lib`

Three distinct content types, all user-curated, all sharing the architectural pattern (markdown files + opt-in subscription + agent-readable):

| Slash | Type | Storage | Purpose |
|---|---|---|---|
| `/ref <persona>` | **Memory** | `~/.xli/chat/<persona>/` (already exists) | Attach a chat persona's collection_id to `search_project` — cross-pollinate memory between sessions |
| `/doc <name>` | **Knowledge** | `~/.config/xli/docs/<name>.md` | Attach static rules/specs/CLAUDE.md-style content into the system prompt |
| `/get <intent>` | **Capability — invocation** | `~/.config/xli/plugins/<id>.md` | Find a subscribed plugin matching the intent, agent invokes via bash |
| `/lib ...` | **Capability — management** | same as `/get` | Curate the plugin library: list / all / subscribe / unsubscribe / remove |

### `/ref` — persona memory cross-pollination

Each persona's Collection becomes searchable from any session via `search_project`:

```
[my-project] › /ref bob
✓ attached bob's memory — search_project will now include their conversation history

[my-project] +1r › what did bob and I discuss about authentication last week?
... agent calls search_project, sees results from both project + bob's transcript ...
```

Session-only (not persisted across REPL restarts; future enhancement). `/unref bob` to detach. Workers inherit attached refs.

### `/doc` — static knowledge attachment

Reference docs are markdown files inlined into the system prompt:

```bash
xli doc --new react-rules    # opens $EDITOR on a starter template
# write something like:
#   # Conventions
#   - Always use functional components.
#   - Run `pnpm test` after every change.
xli doc --list               # see all docs
xli doc --edit react-rules   # re-open in $EDITOR
xli doc --delete react-rules # with y/N confirm
```

In-REPL attachment:

```
[Larry] › /doc react-rules
✓ attached doc react-rules (842 bytes inlined into system prompt)

[Larry] +1d › build me a Login component
... agent's response now reflects the rules ...

[Larry] +1d › /undoc react-rules
✓ detached
```

Soft cap warning at 20kB — for bigger reference material, build a persona + `/ref` instead (RAG retrieval, not in-context every turn).

### `/lib` — plugin library management

Plugins are markdown files at `~/.config/xli/plugins/<id>.md` describing external APIs. Each plugin has YAML-ish frontmatter (id, name, description, categories, risk, auth_env_vars) and a body with prose docs + curl examples.

```bash
xli plugin --new openweather  # opens $EDITOR on template
xli plugin --list             # global catalog
xli plugin --show openweather # print full markdown
xli plugin --edit openweather # edit
xli plugin --delete openweather
```

In-REPL:

```
[my-project] › /lib                     # subscribed plugins
[my-project] › /lib all                 # global catalog (● = subscribed here)
[my-project] › /lib subscribe openweather
[my-project] › /lib unsubscribe openweather
[my-project] › /lib remove openweather  # delete from catalog entirely
```

Subscription persists in `<project>/.xli/plugins.txt`. Workers inherit subscriptions.

### `/get` — intent-based plugin invocation

```
[my-project] › /get the weather in seattle
```

Internally, `/get <intent>` is rephrased to direct the agent: "Use plugin_search to find a subscribed plugin matching this intent, then plugin_get + bash to invoke it. If no plugin matches, tell me — do not fabricate output."

The agent calls `plugin_search("the weather in seattle")` → matches `openweather` → `plugin_get("openweather")` → reads the curl example → bash with intent=network → curl fires → result returns.

### Plugin risk levels

Each plugin declares `risk:` in frontmatter. Visual cue in `xli plugin --list` (green/yellow/red).

| Level | Examples | Behavior |
|---|---|---|
| `low` | sec-edgar, gdelt, weather, geocoding | Read-only public APIs. Agent calls freely. |
| `medium` | gist create, dropbox upload, S3 GET | Credentialed reads, rate-limited writes. |
| `high` | post tweet, send DM, push to main, send email | Public-facing writes. **Don't auto-trust even in YOLO mode.** Users should verify the call before approving. |

Currently the risk gating uses bash's existing intent gate (curl calls have `intent=network` which prompts y/N unless YOLO). A dedicated risk-aware gate is on the roadmap.

### Anti-hallucination guards

When `plugin_search` finds no match, it returns a structured `NO_PLUGIN_MATCH` marker that the system prompt explicitly tells the model **never to fabricate around**. Combined with the existing 0-tools-claim warning (the yellow flag when models claim work without using tools), the system is structurally suspicious of its own model's output.

---

## Multi-machine fabric

The thesis: **your phone is a thin client to your local iXaac, not to a cloud chat app.** Conversations app on the phone sends OMEMO-encrypted XMPP messages over Tailscale to a Prosody server you run; iXaac on that machine handles them with your full project context, your plugins, your tools. Replies come back the same way. xAI tokens still get spent on the agent turn (you're paying anyway), but the substrate, logs, and orchestration are entirely yours.

Two halves, intentionally split into separate XMPP identities:

| Half | JID | Purpose | Risk |
|---|---|---|---|
| **Send** (Phase 1) | `sender@<your-tailnet>` | Outbound notifications. The agent calls this via the `xmpp_send` plugin. | low — send-only |
| **Daemon** (Phase 2) | `daemon@<your-tailnet>` | Inbound listener. Decrypts messages from a JID whitelist; dispatches to verbs or to a one-shot agent run. | high — RCE-capable |

Splitting the identities means a leaked sender password lets an attacker spoof notifications (annoying), but doesn't let them impersonate the daemon to your phone (RCE). Different OMEMO state files, different rotation schedules, different blast radius.

### Phase 1 — `xmpp_send` (outbound)

A markdown plugin at `~/.config/xli/plugins/xmpp_send.md` plus a sender script at `~/.config/xli/bin/xmpp_send.py` (running in its own dedicated venv to avoid polluting iXaac's). The agent finds the plugin via `plugin_search` on intents like *"send me a notification when this is done."* Reads the doc, composes the right `xmpp_send.py` invocation via bash. OMEMO end-to-end encrypted (Signal-style ratchet, BTBV trust). Setup walkthrough lives in `xmpp_send.md`.

```bash
~/.config/xli/bin/xmpp_send.py "$XMPP_DEFAULT_RECIPIENT" "tests passed"
```

### Phase 2 — `xli daemon --xmpp` (inbound)

Long-running listener that decrypts incoming OMEMO messages from a JID whitelist and dispatches:

1. **Built-in `kill`** — daemon shuts down cleanly.
2. **Verb scripts** in `~/.config/xli/verbs/<name>.sh` — first word matches → run that script with the rest as args, reply with stdout. Base catalog: `disk`, `load`, `temp`, `branch`, `recent`, `wol`, `restart`. Drop new verbs in the directory; daemon picks them up at message time.
3. **Workspace prefix** — `[alias] message...` overrides the agent fallback target so `[isaac2] grep me the auth module` runs the agent against that specific workspace.
4. **Agent fallback** — anything that didn't match a verb spawns `xli ask` with the message as the prompt against the most-recently-active project workspace. Reply is the agent's output, OMEMO-encrypted back.

```bash
xli daemon --xmpp        # uses ~/.config/xli/daemon.toml; reads XMPP_DAEMON_PASSWORD from env
```

The daemon writes an append-only audit log at `~/.local/share/xli/daemon-audit.log` (JSONL: `{ts, from, body, status}`) — every received message, accepted or rejected.

Substrate: **local Prosody bound to Tailscale only.** Don't try this on conversations.im or other public servers — anti-spam policies bounce messages between mutually-rostered paid accounts, and you can't fix it from outside. With Prosody on your tailnet, you control the policy.

### Setup pointers

Full reproducible setup (Tailscale install, Prosody config, cert provisioning, sender install, account registration, Conversations on the phone) lives in `~/.config/xli/plugins/xmpp_send.md`. The daemon-specific config lives in `~/.config/xli/daemon.toml.example` with annotated comments — copy to `daemon.toml` and edit.

What's deferred from Solid v0:
- Approval flow for high-impact verbs (second-factor JID confirmation)
- systemd user-service unit (run as a service, restart on crash, auto-start at boot)
- Per-JID privilege tiers (read-only vs full)
- Job IDs for long-running verbs (currently sync; agent timeout 5 min)
- File/blob transfer for photo intake (camera → desktop OCR pipeline)
- Phase 3 agent-loop bot (research-level)

---

## CLI subcommand reference

### Project lifecycle

| Command | Effect |
|---|---|
| `xli init [NAME] [--path PATH] [--collection-id ID] [--no-sync] [--force] [--local] [--snapshot]` | Initialize a project. `--local` skips Collection upload entirely; `--snapshot` caches a paths+sizes index for fast structural search. |
| `xli new <NAME> [--path PATH]` | Create a directory + initialize it in one step. |
| `xli scratch [NAME] [--no-chat] [--yolo] [--force]` | Ephemeral local-only project under `~/.xli/scratch/`, drops into chat. |
| `xli sync [PATH] [--dry-run]` | Push local changes to the collection (auto-runs after every mutating turn). |
| `xli code [TARGET] [--yolo]` | Project-scoped code REPL. `TARGET` can be a path or registered project name. |
| `xli chat [NAME] [--new N \| --list \| --edit N \| --delete N] [--yolo] [--yes]` | Persona-based conversation REPL. |
| `xli status [PATH]` | Show config, key pool, models, temperatures, project state, cost-tracking state. |
| `xli projects [FILTER]` | List every registered xli-initialized project (cloud-Collection-tracked); filter by substring. |
| `xli ask --workspace W "PROMPT"` | One-shot agent run for non-interactive callers (used by the daemon for agent fallback). Prints reply to stdout. |

### Workspaces

Broader than `xli projects`: also includes directories you reference but don't `xli init` (e.g. archived snapshots). Auto-touched by every `xli` invocation (except stateless commands). Used by the XMPP daemon for agent-fallback dispatch routing.

| Command | Effect |
|---|---|
| `xli workspaces list [--projects \| --snapshots]` | List workspaces, sorted by last-active. |
| `xli workspaces add PATH [--snapshot] [--alias NAME] [--notes ...]` | Register a directory. Without `--snapshot` it's `kind=project`. |
| `xli workspaces project KEY` / `xli workspaces snapshot KEY` | Flip a workspace's kind. KEY can be alias or path. |
| `xli workspaces alias KEY [NEW_ALIAS]` | Set or clear an alias. Aliases are unique; setting a colliding alias clears the previous holder. |
| `xli workspaces remove KEY` | Forget about a workspace. |

### Multi-machine fabric

| Command | Effect |
|---|---|
| `xli daemon --xmpp [--config PATH]` | Run the inbound XMPP command listener. Reads `~/.config/xli/daemon.toml` by default; password from `$XMPP_DAEMON_PASSWORD`. Re-execs through the OMEMO venv (`~/.config/xli/bin/venv/`) so slixmpp/slixmpp-omemo are available. |

The outbound side (Phase 1) isn't a CLI subcommand — it's a plugin (`xmpp_send`) the agent calls when the user asks it to send a message. The `xmpp_send.py` script is at `~/.config/xli/bin/xmpp_send.py` if you want to invoke it manually.

### Knowledge management

| Command | Effect |
|---|---|
| `xli doc --new NAME` | Create a new reference doc; opens `$EDITOR`. |
| `xli doc --list` | List all docs. |
| `xli doc --edit NAME` | Edit a doc in `$EDITOR`. |
| `xli doc --delete NAME [--yes]` | Delete a doc. |
| `xli plugin --new ID` | Create a new plugin from template; opens `$EDITOR`. |
| `xli plugin --list` | List all installed plugins. |
| `xli plugin --show ID` | Print a plugin's full markdown. |
| `xli plugin --edit ID` | Edit a plugin in `$EDITOR`. |
| `xli plugin --delete ID [--yes]` | Delete a plugin. |

### Setup + key management

| Command | Effect |
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

| Command | Effect |
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

**Important: `management_api_key` is NOT in this file.** Read from `XAI_MANAGEMENT_API_KEY` env only. `keys[0]` is always the primary (used for sync + main agent). Workers round-robin through the rest.

### `~/.config/xli/personas/<name>.md`

Persona system prompts (one file per persona). Hand-editable.

### `~/.config/xli/docs/<name>.md`

Reference doc files. Hand-editable. Inlined into system prompts on `/doc`.

### `~/.config/xli/plugins/<id>.md`

Plugin descriptors. YAML-ish frontmatter (id, name, description, categories, risk, auth_env_vars) + markdown body (description, endpoints, usage examples, gotchas).

### `~/.config/xli/projects.json`

Global project registry. Auto-maintained by `xli init`.

### `<project>/.xli/`

Per-project state, created by `xli init`:

- `project.json` — collection ID (empty for `--local`), name, created timestamp, `local_only`, `extra_ignores`, `conversation_id` (xAI prompt-cache key)
- `manifest.json` — `relpath → {sha256, mtime, file_id, last_synced}` for diff-based sync
- `index.txt` — paths+sizes index when initialized with `--snapshot`
- `plugins.txt` — subscribed plugin IDs (one per line)
- `repl_history` — that project's REPL command history

### `~/.xli/chat/<persona>/.xli/`

Per-persona project state (same shape as project state — personas are projects under the hood):

- `project.json`, `manifest.json`, `repl_history`
- `plugins.txt` — persona-level plugin subscriptions

### `<project>/.xliignore` (optional)

`.gitignore`-syntax extra patterns to skip during sync.

---

## Architecture

### Sync engine

On startup the REPL walks the project (respecting `.gitignore` + `.xliignore`), diffs against the collection by sha256, and uploads/updates/removes deltas. Mutating operations fan out across `max_parallel_workers` threads with **429 backoff per op** (exponential, max 5 retries). Empty files and binaries are skipped automatically. After every turn that mutates files, dirty paths flush. **Local disk is the source of truth.**

The default ignore list is aggressive: `.git/`, `.xli/`, `venv/`, `.venv/`, `node_modules/`, `.next/`, `.nuxt/`, `.svelte-kit/`, `.turbo/`, `.vercel/`, `.astro/`, `out/`, `dist/`, `build/`, `target/`, `.cache/`, `coverage/`, `__pycache__/`, build outputs, dotfiles for many tools.

**Known limitation:** only the root `.gitignore` is read; nested `.gitignore` files in subdirectories are not honored. For a monorepo with package-level ignores, use `.xliignore` at the root.

### Streaming

Orchestrator chat completions stream via `stream=True`. Content deltas render through a `rich.Live` widget that re-renders Markdown progressively (headers, bold, code blocks, lists). Tool calls stream silently — they materialize as discrete `→ tool_name` events with green/red badges and dimmed result previews. The user sees both the answer flowing AND the work happening.

**Reasoning model handling:** xAI's reasoning models (e.g. `grok-4.20-reasoning`) emit `delta.reasoning_content` separately from `delta.content`. iXaac captures both — reasoning is treated as private thinking and not displayed unless the model produced reasoning but no final content (then a yellow panel surfaces the reasoning so the failure mode is diagnostic rather than silent).

### Multi-key swarm

Tool calls in a single batch are classified as parallel-safe (reads, greps, search_project, plugin_search, web_search, x_search, code_execute, dispatch_subagent) or sequential (writes, edits, bash). Parallel-safe calls fan out via a thread pool. Each `dispatch_subagent` worker pulls a chat key from a round-robin pool, runs its own contained tool loop with **read-only tools only**, returns a tight summary. Workers cannot write, edit, or dispatch further workers.

### xAI server-side tools

`web_search`, `x_search`, and `code_execute` are local function tools that internally fire one-shot Responses-API sub-calls (xAI moved these to Responses API only; Chat Completions Live Search is deprecated). Sub-call usage is tracked and absorbed into the turn's stats.

### Plugin invocation flow

L1 ("read-and-bash") tier — currently shipped:

1. User triggers via `/get <intent>` or natural-language query
2. Agent calls `plugin_search(intent)` → returns top-5 candidate plugins from subscribed set
3. Agent calls `plugin_get(name)` → reads full markdown of the chosen plugin
4. Agent composes a `curl` call from the plugin's usage examples, expanding `${ENV_VAR}` from environment
5. Agent calls `bash(command, intent="network")` → fires the request
6. Result returns; agent interprets and answers the user

L2 (templated curl in plugin's `## Usage`) and L3 (`plugin_call(name, params)` structured RPC) are deferred — see [Known limitations](#known-limitations--future-work).

### Plan mode

Tool list is restricted to read-only investigation tools, a preamble is injected. Agent investigates and outputs a concrete numbered plan. `/execute` toggles back and replays "Execute the plan above" — agent now has the full toolset.

### Hallucination guard

After every turn, if the orchestrator's text contains a past-tense action verb (`verified`, `created`, `wrote`, `tested`, `installed`, `fixed`, etc.) but `tool_calls == 0`, a yellow warning surfaces under the turn line: `⚠ model said "X" but called 0 tools — verify before trusting`. False-positives are tolerable — the warning is a nudge, not a wall.

### Cost tracking

Token usage from every completion is absorbed into per-turn stats. Server-tool sub-calls (Responses API) include their own usage which is added to the orchestrator's tally. If `pricing` is configured, USD cost is computed per call with the actual model used. Orchestrator and worker spend tracked separately.

### Personas

Each persona is a Collection-backed XLI project at `~/.xli/chat/<name>/`. Recent turns load inline as history; older turns sync as `turns/<ts>.md` files and become RAG-searchable. Switching personas mid-chat saves the current state and reopens fresh on the new persona.

### Knowledge layer

- **`/ref` (memory):** appends a persona's `collection_id` to the session's `search_project` collection list. Agent's existing RAG search now spans both project + attached personas.
- **`/doc` (knowledge):** stores `(name, content)` in `Agent.attached_docs`. The agent's `_effective_system_prompt()` rebuilds each turn from `base_system_prompt + attached_docs`. `/reset` keeps docs (they live in the system prompt, not tool history).
- **`/lib + /get` (capability):** subscribed plugins live in `<project>/.xli/plugins.txt`. Two new tools `plugin_search` and `plugin_get` are added to the agent's palette only when at least one plugin is subscribed (otherwise hidden to keep the palette clean).

### Registry + GC

`xli init` writes an entry to `~/.config/xli/projects.json` (path → collection_id). `xli gc` cross-references the registry against the cloud's collection list and your filesystem to identify orphans.

---

## Tool catalog (what the agent can call)

| Tool | Type | Purpose |
|---|---|---|
| `read_file` | local | Read a UTF-8 file from the project (line-numbered output) |
| `write_file` | local | Create or overwrite a file. Marks dirty for sync. |
| `edit_file` | local | Replace exact substring; errors if `old_string` isn't unique unless `replace_all=true` |
| `list_dir` | local | List immediate children of a directory |
| `glob` | local | Recursive glob via fnmatch over project-relative paths |
| `grep` | local | Recursive regex search; returns `path:line: match` |
| `bash` | local | Run shell command in project root. **Mandatory `intent` declaration** (read-only / modifies-project / modifies-system / network); the gate prompts y/N for risky intents unless YOLO. |
| `search_project` | RAG | Hybrid retrieval over the project's xAI Collection (and attached refs). Disabled in `--local` mode. |
| `web_search` | server | xAI Live Search via Responses API. Returns answer + citations. |
| `x_search` | server | Search posts on X (Twitter) via Live Search. |
| `code_execute` | server | Run Python in xAI's sandbox. NumPy/Pandas/Matplotlib/SciPy preinstalled. |
| `plugin_search` | local | Search subscribed plugins by intent. Returns top-K candidates. Hidden when no plugins subscribed. |
| `plugin_get` | local | Read full markdown of a subscribed plugin. Hidden when no plugins subscribed. |
| `dispatch_subagent` | swarm | Fire one or more read-only worker investigators in parallel, each on its own API key. |

**Workers** see all of the above except `write_file`, `edit_file`, and `dispatch_subagent`. They're read-only by design — investigators, not implementers.

---

## Security model

### One privileged credential, env-only

`XAI_MANAGEMENT_API_KEY` is read from the environment, **never stored on disk**. It can create new chat keys, rotate them, and manage your collections.

### Chat keys are scoped, expiring, rotatable

Each key created by `xli setup` / `xli bootstrap` has an `expireTime` (default 180 days) and named ACLs (`api-key:model:*`, `api-key:endpoint:*`). All chat keys are revocable via `xli keys revoke` (server-side + local).

### Bash gating

The agent's `bash` tool requires an honest `intent` declaration on every call (`read-only`, `modifies-project`, `modifies-system`, `network`). Riskier intents are gated on a y/N confirmation prompt unless `--yolo`/`/yolo` is set. Workers may only run `read-only` bash.

### Plugin risk levels

Each plugin declares a `risk:` field — `low`, `medium`, `high`. Plugin invocations go through the bash tool with `intent=network` (curl reaches the internet), so the bash gate prompts you before each call by default. A dedicated risk-aware gate (e.g. require explicit y/N for `risk: high` even in YOLO mode) is on the roadmap.

### File perms

`~/.config/xli/config.json` is `chmod 600`. Persona prompts, docs, and plugin files inherit normal user perms.

### Public-release safety

- Don't paste real API keys into chat logs (xAI auto-flags accounts when secrets appear in third-party model inputs)
- Rotate keys periodically (`xli keys rotate`)
- Default 180-day expiry caps the blast radius if config.json leaks

---

## Concrete workflows

### NAS-as-database (paths-only structural search)

```bash
cd /mnt/nas/music
xli init mymusic --local --snapshot
# ...indexing files... 47000  /Music/Pink Floyd/...
xli code mymusic
[mymusic] › how many flac files do I have, grouped by artist?
# Agent greps .xli/index.txt — instant answer, no full-tree walk
```

### Persona for ongoing work

```bash
xli chat --new project-bob          # write a persona that knows about your project
# ... in editor: "You are Bob, who specializes in our auth module..."
xli chat project-bob
[project-bob] › I'm thinking about migrating from JWT to PASETO. Pros and cons?
# ... long discussion, all turns saved ...
/exit

# Two weeks later, in your code project:
xli code my-app
[my-app] › /ref project-bob
✓ attached project-bob's memory

[my-app] +1r › last week we discussed PASETO migration. Now let's actually start it.
# Agent's search_project sees both the code AND the conversation. Continuity.
```

### Plugin for a third-party API

```bash
xli plugin --new openweather
# in editor: fill in template with OpenWeatherMap details
xli plugin --list
#   id (use this with /lib subscribe) · risk · categories · description
#     openweather  low  weather, geo  ·  Current weather + forecasts

cd ~/Projects/my-trip-planner
xli code
[my-trip-planner] › /lib subscribe openweather
✓ subscribed to openweather (risk=low)
# (Set the env var first if needed)
[my-trip-planner] +0d › /get the weather forecast for tokyo next week
# Agent: plugin_search → openweather → plugin_get → bash curl → result
```

### Project-specific rules via `/doc`

```bash
xli doc --new this-project-conventions
# in editor:
#   - Always run pytest before claiming success.
#   - Use httpx, not requests.
#   - Test files live in tests/, mirror the source structure.

xli code my-app
[my-app] › /doc this-project-conventions
✓ attached doc this-project-conventions (412 bytes inlined into system prompt)

[my-app] +1d › add a function that fetches user data from /api/users/{id}
# Agent uses httpx (not requests), creates a test, runs pytest before declaring done.
```

### Multi-machine: phone-as-thin-client to home iXaac (shipped)

Phase 1 (`xmpp_send` plugin) and Phase 2 (`xli daemon --xmpp` listener) both ship. Substrate: local Prosody on Tailscale + Conversations app on phone + OMEMO end-to-end encryption. Detailed walkthrough in [Multi-machine fabric](#multi-machine-fabric).

```bash
# Send a notification (Phase 1; agent does this via the xmpp_send plugin)
~/.config/xli/bin/xmpp_send.py "$XMPP_DEFAULT_RECIPIENT" "long-running task done"

# Run the inbound daemon (Phase 2)
xli daemon --xmpp
# Then from your phone (Conversations) to daemon@<your-tailnet>:
#   "disk"                          → verb, replies with disk usage
#   "[ixaac] what changed today?"   → agent run in the iXaac workspace
#   "kill"                          → daemon shuts down cleanly
```

Phase 3 (agent-loop bot triggered by chat) is research-level and deferred.

---

## File layout

### iXaac source

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
  ignore.py         .gitignore + .xliignore + binary-skip walker (walk_project) + paths-only walker (walk_paths_only)
  registry.py       Cloud-Collection-tracked xli-initialized projects (used by `xli gc` and `xli projects`)
  workspaces.py     Broader workspace registry — every dir xli has run in + explicit references (used by daemon for routing)
  persona.py        Persona file management for `xli chat`
  transcript.py     Per-turn conversation persistence for personas
  doc.py            Reference doc management for `/doc`
  plugin.py         Plugin file management + subscription model + intent search for `/lib` + `/get`
  daemon.py         XMPP inbound listener — Phase 2 of the multi-machine fabric (verb dispatch + agent fallback)
proposals/
  ref-system.md     Detailed design proposal for the four-slash knowledge layer
```

### Per-user state under `~/.config/xli/`

```
config.json              Single global config (mode 0600). All credentials, models, pricing.
projects.json            Registry of xli-initialized projects (collection_id, name, path).
workspaces.json          Broader workspace registry (project + snapshot + last_active).
plugins/                 Plugin catalog. One markdown file per plugin (id.md).
docs/                    Reference docs attachable via /doc.
personas/                Persona prompts + state for `xli chat`.
verbs/                   Daemon verb catalog. One executable script per named verb.
                         Base catalog: disk, load, temp, branch, recent, wol, restart.
bin/                     Dedicated venv + helper scripts for the OMEMO sender.
  xmpp_send.py             One-shot OMEMO-encrypted XMPP sender (Phase 1).
  xmpp_check.py            Diagnostic for inspecting OMEMO PubSub state for any JID.
  omemo-state.json         Sender's OMEMO identity + ratchet state (mode 0600).
  venv/                    Python venv with slixmpp + slixmpp-omemo + crypto deps.
daemon.toml              XMPP daemon config (JID, whitelist, rate limit, fallback settings).
daemon.toml.example      Annotated template. Copy to daemon.toml and edit.
daemon-omemo-state.json  Daemon's OMEMO identity + ratchet state (mode 0600). DIFFERENT from sender's.
wol.txt                  Optional. Hostname → MAC whitelist for the `wol` verb.
restart-allowed.txt      Optional. systemd unit names allowed for the `restart` verb.
```

### Append-only audit log

```
~/.local/share/xli/daemon-audit.log   JSONL — every inbound XMPP message, accepted or rejected.
```

---

## Troubleshooting

### `xli setup` couldn't auto-detect models

Some xAI accounts don't expose `/v1/models`. Workaround: `xli models set --orchestrator <name> --worker <name>`. Defaults work for most accounts.

### `web_search`, `x_search`, or `code_execute` errors with `'OpenAI' object has no attribute 'responses'`

Your `openai` package is too old. Upgrade: `./venv/bin/pip install -U 'openai>=1.50'`.

### Sync fails with "Empty stream received"

Caused by 0-byte files. Already auto-skipped at the walk step. If you still see this, check whether the file size changed mid-sync.

### Sync uploaded files I didn't expect (e.g., `.next/` build output)

The default ignore list covers most build outputs. If a specific dir isn't ignored and you don't want it uploaded, add it to `.xliignore` at the project root.

### Yellow `⚠ model said "created" but called 0 tools` warning

The model claimed work without using any tools — likely a hallucination. Verify before trusting; ask follow-up questions to confirm the work actually happened.

### Reasoning model produces no answer

xAI's reasoning models can sometimes "think" through a problem and fail to emit a final answer. iXaac now surfaces the reasoning in a yellow panel when this happens (so you see what it was thinking). Workarounds:
- Rephrase the question
- Switch to a non-reasoning model (`xli models set --orchestrator grok-4`)
- The reasoning panel often shows what got the model stuck

### `/doc <name>` doesn't appear to influence the agent

For reasoning models specifically, system-prompt rules can be reasoned past. Try:
- A clearer/firmer doc rewrite (imperative English, not cryptic shorthand)
- Use a non-reasoning model for persona/style work

### "/doc CALLMESIR" — "no such doc"

That's the doc *content*, not the doc *name*. Doc names are slug-cased (letters/digits/_/-/). Use the cyan-highlighted **first column** of `xli doc --list` output — that's the name. Same applies to plugins and personas.

### Markdown rendering looks weird mid-stream

Code blocks render as plain text until the closing ``` arrives, then snap to highlighted. That's a quirk of streaming Markdown — no clean fix.

### Worker output looks weird

Workers return summaries inside a `--- worker[label] · model · iters · tokens ---` header; the headers can wrap on narrow terminals. Cosmetic only.

### Bootstrap throttled (429)

Retries with exponential backoff up to 5 times. If you're rapidly creating many keys, slow down or wait a few minutes.

---

## Known limitations / future work

### Encrypted credential vault

Currently plugins authenticate via env vars set before invocation. A proper encrypted vault at `~/.config/xli/vault.enc` (Fernet-encrypted, OS-keyring-backed by default with passphrase fallback) is designed and ready to build — see `proposals/ref-system.md` § "Auth model — encrypted vault, keyring-backed by default". Single biggest near-term security improvement.

### Plugin authoring wizard

`xli plugin --new <id>` currently opens `$EDITOR` on a starter template. Designed: `xli plugin add` interactive wizard that prompts for id/name/description/category/risk/URL/auth-type/env-vars/parameters and generates the markdown + empty `.env` file in ~45 seconds. See proposal § "Authoring path — making this real for non-developers".

### Argus-byo bulk import

A `xli plugin import <directory>` command that parses `~/argus-byo/providers/plugins/*.js` (94 ready-to-port plugins covering government, finance, news, social, cloud storage, OCR, satellite, blockchain) and writes one markdown file per connector. Each plugin uses the same `ProviderRegistry.register({...})` shape so a single regex parser handles all of them. Bulk import would seed the catalog with ~94 immediately-usable plugins.

### Plugin invocation tiers L2 + L3

L1 (read-and-bash) is shipped. L2 (templated curl in plugin's `## Usage`) and L3 (`plugin_call(name, params)` structured RPC) are designed but deferred — only worth building if real usage shows L1 is a friction point.

### Persistent doc/ref subscriptions

Currently `/ref bob` and `/doc react-rules` work session-only. Per-project persistent subscription files (`<project>/.xli/refs.txt`, `<project>/.xli/docs.txt`) so you don't have to re-attach every session — easy add when you find yourself doing it manually too often.

### XMPP fabric — what's still deferred

Phases 1 and 2 ship (see [Multi-machine fabric](#multi-machine-fabric)). What's not yet built within Phase 2's scope:

- **Approval flow for high-impact verbs.** A second-factor JID has to confirm within N seconds before the daemon executes destructive verbs (e.g. `restart prod`). Defer until you've used the daemon enough to know which verbs warrant the friction.
- **systemd user-service unit.** Currently the daemon runs in a foreground terminal (or via `nohup ... &`). Wrapping it in a systemd user unit gives auto-start, restart-on-crash, structured journald logging.
- **Per-JID privilege tiers.** Right now any whitelisted JID can run any verb. Tiered access (read-only vs full) is straightforward to add when more JIDs join the whitelist.
- **Job IDs for long-running verbs.** Agent fallback can take 30s-5min. Currently the phone just waits; there's no `running... will reply when done` ack.
- **File/blob transfer for photo intake.** XMPP supports inline file transfers (XEP-0066, HTTP upload). Phone takes a photo, daemon receives, OCRs, files. Same plumbing as the planned `/img` REPL command.
- **Phase 3 agent-loop bot.** Bot fires off full multi-turn agent runs in response to messages, with chat-driven session state. Research-level; defer until Phase 2 has real-world use.

### Hierarchical .gitignore

Only the root `.gitignore` is read; nested ones in subdirectories are ignored. For monorepos, drop ignores in `.xliignore` at the root or use `git check-ignore` invocation (designed, not built).

### Reasoning-model rule adherence

Reasoning models like `grok-4.20-reasoning` can rationalize past system-prompt rules during their thinking phase. Workaround: use non-reasoning models for persona/style work. We considered framing reinforcement (stronger "non-negotiable" language in addendum) but didn't ship it — too easy to make models rigid on benign queries.

### Auto-snapshot refresh

`xli init --local --snapshot` builds the index once. To refresh after files change: `/sync` rebuilds it (already implemented). Auto-rebuild on chat-start when index is older than N minutes is a small future win for big NAS use cases.

---

## Design philosophy (the load-bearing thesis)

**The model and primitives are commodities; the user's curation is the most valuable resource.**

- Personas are **user-curated memories**.
- Docs are **user-curated rules**.
- Plugins are **user-curated capabilities**.
- Refs cross-pollinate them.
- xAI provides Grok + Collections + server tools; **the user composes the system from there**.

This is the inverse of Claude Code / Codex / Cursor, which assume "vendor provides capability, user provides project files." iXaac assumes "vendor provides primitives, user composes the system."

### What this means in practice

- **Plugins are markdown files the user writes** (or imports), not vendor-curated tools. Authoring friction matters more than feature breadth — the wizard (when shipped) is core, not a nice-to-have.
- **Personas are user-defined personalities with their own indexed memory.** Each is a real Collection.
- **Multi-machine is native.** Phone-at-work talks to desktop-at-home via the XMPP daemon — shipped. Conversations app on the phone is the UI; OMEMO end-to-end encryption over Tailscale; verb dispatch + agent fallback into your local iXaac. Not a hack.
- **Self-managing credentials.** Auto-provision, auto-expire, auto-rotate. User isn't asked to "bring an API key"; iXaac mints them.
- **The agent fights its own model's hallucinations.** Yellow warning when claimed work doesn't match tool calls. Structurally suspicious.
- **Reasoning models are tools for thinking, not following directions.** Document the trade-off; let users pick the right model for the job.

### Closest spiritual ancestor

**Emacs.** Not a finished product, a substrate that rewards investment. Power users build their own personal computing environment over years. The platform's job is to provide composable primitives; the user's job is to compose. **iXaac is heading toward Emacs-shape but for AI tooling.**

### Tradeoffs (be honest about these)

- **Power users will love it.** People who already build their own bash aliases, dotfiles, NixOS configs, self-hosted services — they'll get it immediately. The investment compounds.
- **Casual users will bounce.** "Why isn't there a button for X?" → "Because it's yours, not ours" doesn't satisfy them.
- **Niche-but-deeply-loved is a valid position** (Emacs, NixOS, Mastodon, Org-mode). It isn't mass-market and shouldn't try to be.

---

## Uninstall / reset

```bash
# Local state
rm -rf ~/.config/xli/             # config + plugins + docs + personas + verbs + bin/ + daemon state + workspaces
rm -rf ~/.xli/                    # personas, scratch projects, chat transcripts
rm -rf ~/.local/share/xli/        # XMPP daemon audit log

# Per-project state (run in each project dir)
rm -rf .xli/

# Server-side: revoke every key iXaac created
xli bootstrap --revoke --prefix worker --yes
xli bootstrap --revoke --prefix primary --yes
# (Delete xli-prefixed collections via the xAI dashboard, or with `xli gc`)
# Local Prosody users (if you set up the multi-machine fabric):
#   sudo prosodyctl deluser sender@<your-tailnet>
#   sudo prosodyctl deluser daemon@<your-tailnet>
#   sudo prosodyctl deluser phone@<your-tailnet>
```

---

## License

(To be filled in based on your release intent — MIT and Apache-2.0 are common defaults; AGPL-3.0 if you want strong copyleft on hosted use.)

---

## For agents reading this cold

If you're a fresh agent session picking up this project:

1. **Read `proposals/ref-system.md`** if it exists — it's the comprehensive design document for the knowledge layer. The four open questions at the bottom are still open; the architecture above them is settled. (`proposals/` is gitignored, so it may not be present in a fresh clone — see `~/Projects/XLI/proposals/` for the original copy.)
2. **Read the memory notes** at `~/.claude/projects/-home-birdman-Projects-iXaac/memory/` (current project) and `~/.claude/projects/-home-birdman-Projects-XLI/memory/` (predecessor — same codebase, pre-rebrand). The current memory has Phase 1 + Phase 2 status; the predecessor has the philosophical thesis, the iXaac rebrand context, the original XMPP design plan, and a survey of `~/isaac2/Isaac` (a cautionary tale + design library).
3. **The multi-machine fabric (Phases 1 + 2) ships.** Phase 1 = `xmpp_send` plugin (outbound notifications). Phase 2 = `xli daemon --xmpp` (inbound listener with verb dispatch + agent fallback). Substrate: local Prosody on Tailscale + Conversations on phone + OMEMO E2E. See `~/.config/xli/plugins/xmpp_send.md` for the reproducible setup.
4. **Don't propose features that violate the thesis.** Vendor-curated tool palettes, kitchen-sink subsystem sprawl, finished products instead of composable primitives — these are anti-patterns for this project.
5. **Reasoning models are weaker at instruction-following.** When debugging "the doc/persona isn't sticking," check the model first.
6. **Workers are read-only investigators.** Never propose giving them write access; that's a load-bearing safety property.
7. **The user works incrementally and trusts the substrate to compose.** Ship small slices that work end-to-end; defer L2/L3 elaborations until L1 friction is real.

