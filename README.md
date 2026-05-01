# iXaac

*The cloud that knows your code — a personal AI substrate built on xAI's
Grok and Collections.*

> **Status: alpha.** Works end-to-end against a real xAI account.
> Architecture is settled; specific features are still in flux. Expect rough
> edges. **Linux / macOS only.** Python 3.11+.

---

## What this is

A terminal program where your projects live in xAI Collections (durable,
RAG-searchable cloud memory) and Grok talks to them. Not a coding agent in an
IDE wrapper. Not a chatbot you re-paste your code into. Not an MCP platform.
**A substrate that grows with what you curate** — personas, reference docs,
plugins for APIs you care about, and a multi-machine fabric so you can keep
working from your phone.

The internal name on disk is `xli` — that's the binary you'll type. The brand
is **iXaac**.

## What's different

There's a category gap in the AI tooling landscape that iXaac sits in:

- **Cursor / Claude Code / Codex / Copilot** — vendor-curated coding agents.
  Their tool palette is what they ship, extended via MCP servers (heavyweight,
  professionally maintained, not yours). You live in their app.
- **ChatGPT / Claude.ai / Grok app** — vendor-hosted SaaS chatbots. Memory
  features exist but are vendor-controlled. No file ops, no multi-machine,
  you re-paste your project every conversation.
- **Self-hosted RAG apps (Khoj, MemGPT)** — usually one machine, one purpose,
  no agent loop.
- **LangChain / agent frameworks** — toolkits to build *one* product. Not a
  shell you live in.

iXaac sits in the unoccupied gap: **a substrate that rewards investment.**
Write a few personas, a few reference docs, a few plugins for APIs you
actually use, and the system becomes uniquely yours. Power users will love
it; casual users will bounce — and that's fine.

The thesis in one sentence:

> **Vendor provides primitives; user composes the system.**

The reason most AI tools don't offer what iXaac does isn't technical — it's
that walls are their business model. iXaac doesn't have walls because it's
built on commodity primitives (xAI Collections, slixmpp, Tailscale, OMEMO).
That trade is the whole point.

---

## Quick start

You'll need:

- Python 3.11+ on Linux or macOS
- An xAI account with a **management API key** (created in the xAI console,
  Team Settings → API Keys)
- About 5 minutes

```bash
git clone https://github.com/iXaac-xli/iXaac ixaac
cd ixaac
python3 -m venv venv
./venv/bin/pip install -e .
sudo ln -s "$(pwd)/venv/bin/xli" /usr/local/bin/xli   # optional but useful

export XAI_MANAGEMENT_API_KEY=xai-...your-management-key...    # add to your shell rc
xli setup
```

`xli setup` is a single command that:

- Writes a config template at `~/.config/xli/config.json` (chmod 600)
- Auto-discovers your team_id
- Creates 1 primary chat key + 8 worker chat keys via the xAI Management API
- Sets a 180-day expiration on each key (rotatable)
- Auto-detects the best orchestrator and worker models

You don't manually create chat API keys — iXaac provisions them, manages
them, expires them, rotates them. The management key is the *only* credential
you handle directly, and it's read from env, never stored on disk.

Verify with:

```bash
xli status
```

Should show: management key found, team_id cached, 9 chat keys in pool,
models configured.

---

## What it feels like to use

Two complementary REPLs, both invoked with one word.

### `xli code` — the project mode

```bash
cd my-project
xli init                    # uploads the project to a fresh xAI Collection
xli code                    # drops you into the REPL
```

```
╭──────────────────────────────────────────────────────────╮
│ XLI v0.1.0  ·  my-project                                │
│ orchestrator: grok-4-1-fast-non-reasoning  ·  worker: …  │
│ collection: collection_abc123  ·  pool: 9 key(s)         │
│ type /help for slash commands                            │
╰──────────────────────────────────────────────────────────╯

› refactor the auth module to use the new token format
› investigate every .py file in src/ in parallel and summarize each
› /plan migrate from JWT to PASETO
```

The agent reads, writes, runs tests, fans out parallel workers, hits xAI's
server-side `web_search` / `x_search` / `code_execute` (Python sandbox) tools,
and syncs your dirty files back to the Collection at the end of every turn.
Files on disk are the source of truth.

### `xli chat` — persona-based conversation

```bash
xli chat --new larry        # creates persona, opens $EDITOR on its prompt
xli chat larry              # start a session as larry
```

Each persona is a Collection-backed long-running conversation with its own
memory. Recent turns load inline as history; older turns stay searchable
forever via RAG. Switch personas mid-chat with `/persona <name>` — different
personalities, different specialties, all on demand.

### Slash commands

Both REPLs share most of these:

```
/help, /status, /sync, /reset, /cost, /yolo, /safe, /models, /temp,
/projects, /plan, /execute, /cancel, /ref, /unref, /doc, /undoc,
/lib, /get, /persona, /personas, /edit, /forget, /exit, !<shell>
```

`!` followed by a shell command runs it locally (no model call, no tokens).
For `clear`, `ls`, ad-hoc utilities. The agent gets out of your way when you
don't need it.

### Plan mode

When you're about to make a non-trivial change, type `/plan` first. The agent
investigates with read-only tools, captures intermediate findings to a
scratchpad at `.xli/plan-notes.md`, and emits a numbered plan as text. You
review. `/execute` to approve and run with the full toolset. `/cancel` to
drop. The scratchpad survives across `/exit` and across max-iteration aborts
— long investigations are resumable.

---

## The knowledge layer

Three slash commands that work in concert, all backed by markdown files you
write or import:

### `/ref <persona>` — cross-pollinate memory

Attach another persona's collection to your current session. Your
`search_project` tool now spans both the current project AND that persona's
transcript. Useful when you want yesterday's planning conversation to inform
today's code work.

### `/doc <name>` — attach static knowledge

Reference docs (markdown files at `~/.config/xli/docs/<name>.md`) get inlined
into the agent's system prompt. Project rules, framework conventions,
CLAUDE.md-style guidance, any always-on context. Soft cap of 20kB before a
warning — bigger reference material should become a persona instead.

### `/get <intent>` — invoke a plugin

Find and call a subscribed plugin matching a natural-language intent.
*"What's the weather in Aurora?"* → `plugin_search` finds Open-Meteo →
`plugin_get` reads its doc → agent composes the curl call → result comes
back.

### `/lib` — manage the plugin library

`/lib` lists subscribed plugins. `/lib all` shows everything installed.
`/lib subscribe <id>` opts into a plugin for the current project
(subscriptions are per-project). `/lib unsubscribe <id>` opts out.
`/lib remove <id>` deletes a plugin from your catalog entirely.

---

## Plugins

A **plugin** is a markdown file at `~/.config/xli/plugins/<id>.md` describing
an external API — its endpoints, auth shape, curl examples, gotchas. The
agent reads it and composes the actual call via `bash`. No MCP servers to
spawn, no SDKs to import. Write one in 60 seconds, share by sending a
markdown file.

iXaac ships a starter pack of 11 read-only plugins covering categories you'll
actually use. Install with one command:

```bash
xli plugin --install-stock
```

| Plugin | Auth | What it's for |
|---|---|---|
| `open-meteo` | none | Weather forecasts and current conditions |
| `gdelt` | none | Global news/event trends over time |
| `google-news` | none | Topic and region headlines via RSS |
| `hackernews` | none | Full-text search over Hacker News |
| `wikipedia` | none | Article summaries and lookups |
| `courtlistener` | optional token | US federal/state court opinions |
| `coingecko` | none | Cryptocurrency prices and history |
| `alpha-vantage` | free key | Stock quotes, forex, fundamentals |
| `bluesky` | none | Social search + popular feeds |
| `xtwitter` | paid key | X v2 search + trends + timelines |
| `aviationstack` | free key | Flight status by number/route |

Plugin credentials live in an encrypted vault (Fernet, OS-keyring-backed by
default with passphrase fallback). Set them with:

```bash
xli auth set alpha-vantage ALPHA_VANTAGE_KEY=<your-key>
```

The bash tool injects vault values only for plugins subscribed in the current
session, only when the command actually references the variable, and only
into that one subprocess's environment. Plaintext never lands on disk;
secrets enter process memory only at call time.

Write your own plugin with `xli plugin --new <id>` — opens a starter template
in `$EDITOR`.

---

## Multi-machine fabric (XMPP + OMEMO + Tailscale)

This is the most distinctive thing iXaac does. Your phone becomes a thin
client to your home iXaac, *not* to OpenAI/Anthropic/xAI cloud chat apps.

The shape:

- A local Prosody XMPP server bound to your Tailscale IP — private, no public
  reachability.
- OMEMO (Signal-style ratchet, BTBV trust) provides end-to-end encryption.
- The Conversations app on your phone is the UI.
- Two halves with different identities and different blast radii:

| Half | JID | Purpose | Risk |
|---|---|---|---|
| **Send** | `sender@<your-tailnet>` | Outbound notifications. Agent calls this via the `xmpp_send` plugin. | Low — send-only. |
| **Daemon** | `daemon@<your-tailnet>` | Inbound listener. Decrypts messages from a JID whitelist; dispatches to verbs or to a one-shot agent run. | High — RCE-capable on a whitelisted JID. |

Splitting identities means a leaked sender password lets an attacker spoof
notifications (annoying), but doesn't let them impersonate the daemon to your
phone (RCE). Different OMEMO state files, different rotation schedules,
different blast radius.

### Outbound — Phase 1

```bash
~/.config/xli/bin/xmpp_send.py "$XMPP_DEFAULT_RECIPIENT" "tests passed"
```

Or the agent calls it via the `xmpp_send` plugin when you ask it to "let me
know when this is done."

### Inbound — Phase 2

```bash
xli daemon --xmpp
```

A long-running listener that decrypts incoming OMEMO DMs and dispatches:

1. **Built-in `kill`** — daemon shuts down cleanly.
2. **Verb scripts** at `~/.config/xli/verbs/<name>.sh` — first word matches a
   verb, runs that script with the rest as args, replies with stdout. Base
   catalog: `disk`, `load`, `temp`, `branch`, `recent`, `wol`, `restart`.
   Drop new verbs in the directory; the daemon picks them up at message time.
3. **Workspace prefix** — `[alias] message...` overrides the agent fallback
   target so `[isaac2] grep me the auth module` runs the agent against that
   specific workspace.
4. **Agent fallback** — anything that didn't match a verb spawns `xli ask`
   against the most-recently-active project. Reply is the agent's output,
   OMEMO-encrypted back.

Audit log at `~/.local/share/xli/daemon-audit.log` (JSONL, mode 0600) records
every received message — accepted or rejected.

### Why this matters

Cursor doesn't have this. Claude.ai doesn't have this. Copilot doesn't have
this. Their architectures don't bend that way — the walls *are* the product.

What this gets you concretely: you can leave your desk, walk the dog, and
keep thinking about the problem you were debugging. *"Hey, what was that
thing about the auth ratio?"* over OMEMO → daemon receives → agent has full
project context still in the Collection → answer comes back to your phone.
The work follows you.

Setup is non-trivial — local Prosody, Tailscale install, OMEMO trust on first
message — but the full reproducible walkthrough lives in the plugin doc at
`~/.config/xli/plugins/xmpp_send.md`.

---

## A few more things worth knowing

### Local-only mode + path snapshots

For directories you don't want uploaded — NAS, photo libraries, PDFs,
archives, anything binary or private:

```bash
xli init --local --snapshot
```

No Collection, no sync. `--snapshot` caches a paths-and-sizes index for fast
structural search ("how many .flac files do I have, grouped by artist?").

### Multi-key swarm

Tool calls in a single batch get classified as parallel-safe (reads, greps,
search) or sequential (writes, edits, bash). Parallel-safe calls fan out
across a thread pool; each worker pulls a chat key from the pool round-robin
and runs its own contained tool loop with read-only tools only. Workers are
investigators, not implementers — that's a load-bearing safety property.

### Hallucination guard

After every turn, if the model claims past-tense work was done (`verified`,
`created`, `wrote`, `tested`, `installed`, etc.) but called zero tools, a
yellow warning surfaces: *⚠ model said "X" but called 0 tools — verify
before trusting.* False positives are tolerable; the warning is a nudge, not
a wall.

### Plan-history archive

When you `/execute` or `/cancel` a plan, the scratchpad gets archived to
`.xli/plans/<label>-<timestamp>.md` and synced into the Collection —
RAG-searchable. The cloud knows not just what your code is, but how you got
there.

### Streaming with live Markdown rendering

Headers, bold, code blocks, lists render progressively as text accumulates.
Tool calls stream silently and materialize as discrete `→ tool_name` lines
with green/red badges and dimmed result previews under each one — you see
both the answer flowing and the work happening.

---

## Command reference

Two surfaces: the **CLI** (`xli <command>`, run from your shell) and the
**REPL slash commands** (typed inside `xli code` or `xli chat`). Most
day-to-day work lives in the REPL; the CLI is for setup, lifecycle, and
out-of-session management.

### CLI — project lifecycle

| Command | What it does |
|---|---|
| `xli init [NAME] [--path PATH] [--collection-id ID] [--no-sync] [--force] [--local] [--snapshot]` | Initialize a project. `--local` skips the Collection (no upload, no sync); `--snapshot` caches a paths+sizes index for fast structural search; `--collection-id` reuses an existing Collection. |
| `xli new <NAME> [--path PATH]` | Create a directory and initialize it in one step. |
| `xli scratch [NAME] [--no-chat] [--yolo] [--force]` | Spin up an ephemeral local-only project under `~/.xli/scratch/` and drop into chat. |
| `xli sync [PATH] [--dry-run]` | Push local changes to the project's Collection. Auto-runs after every mutating turn; manual is rarely needed. |
| `xli code [TARGET] [--yolo]` | Project-scoped code REPL. `TARGET` can be a path or a registered project name (works from anywhere). |
| `xli chat [NAME] [--new N \| --list \| --edit N \| --delete N] [--yolo]` | Persona-based conversation REPL. `--new` creates a persona, `--edit` re-opens its prompt in `$EDITOR`, `--delete` wipes prompt + state dir. |
| `xli status [PATH]` | Show config, key pool, models, temperatures, project state, cost-tracking state. |
| `xli projects [FILTER]` | List every registered xli-initialized project; filter by substring. |
| `xli ask --workspace W "PROMPT"` | One-shot agent run for non-interactive callers. Used by the XMPP daemon for agent fallback. Prints reply to stdout. |

### CLI — workspaces

Broader than `xli projects`: also tracks directories you reference but never
`xli init`'d (e.g. archived snapshots, reference repos). Auto-touched by
every `xli` invocation. Used by the XMPP daemon for agent-fallback routing.

| Command | What it does |
|---|---|
| `xli workspaces list [--projects \| --snapshots]` | List workspaces, sorted by last-active. |
| `xli workspaces add PATH [--snapshot] [--alias NAME] [--notes ...]` | Register a directory. Without `--snapshot` it's `kind=project`. |
| `xli workspaces project KEY` / `xli workspaces snapshot KEY` | Flip a workspace's kind. KEY can be alias or path. |
| `xli workspaces alias KEY [NEW_ALIAS]` | Set or clear an alias. Aliases are unique; setting a colliding alias clears the previous holder. |
| `xli workspaces remove KEY` | Forget a workspace. |

### CLI — knowledge curation

| Command | What it does |
|---|---|
| `xli plugin --new ID` | Create a new plugin from a starter template; opens `$EDITOR`. |
| `xli plugin --list` | List every installed plugin (the global catalog). |
| `xli plugin --show ID` | Print a plugin's full markdown to stdout. |
| `xli plugin --edit ID` | Edit a plugin in `$EDITOR`. |
| `xli plugin --delete ID [--yes]` | Delete a plugin. |
| `xli plugin --install-stock [--force]` | Install the bundled 11-plugin starter pack. Skips plugins you already have unless `--force`. |
| `xli auth set ID KEY=value [...]` | Store one or more secrets for a plugin in the encrypted vault. Auto-creates the vault on first call. |
| `xli auth list [ID]` | List plugins with stored secrets, or the keys for a single plugin. |
| `xli auth show ID [--reveal]` | Show stored keys (values redacted by default; `--reveal` prints plaintext). |
| `xli auth clear ID [KEY]` | Remove a single key, or every key for a plugin. |
| `xli doc --new NAME` | Create a new reference doc; opens `$EDITOR`. |
| `xli doc --list` | List all reference docs. |
| `xli doc --edit NAME` | Edit a doc in `$EDITOR`. |
| `xli doc --delete NAME [--yes]` | Delete a doc. |

### CLI — setup, keys, models

| Command | What it does |
|---|---|
| `xli config` | Write a config template to `~/.config/xli/config.json` if missing (chmod 600). |
| `xli setup [--workers N] [--expire-days N] [--force]` | One-shot first-time setup: config + team_id discovery + key provisioning + model auto-detect. Idempotent. |
| `xli bootstrap [--count N] [--prefix LABEL] [--expire-days N] [--force] [--revoke] [--yes]` | Lower-level: provision N chat keys with a label prefix, or `--revoke` to delete keys by prefix. |
| `xli keys list` | Show every chat key with days-until-expiration and status. |
| `xli keys rotate [--label LABEL]` | Rotate the secret on one (or all) keys. Key ID stays the same. |
| `xli keys expire --days N [--label LABEL]` | Update the `expireTime` on one (or all) keys. |
| `xli keys revoke [--prefix LABEL] [--yes]` | Delete keys by label prefix (server-side + local config). |
| `xli models list` | List models the team has access to. |
| `xli models recommended` | Print the heuristic best-of-class picks (no commit). |
| `xli models set [--orchestrator NAME] [--worker NAME]` | Pin orchestrator and/or worker model. |

### CLI — multi-machine fabric

| Command | What it does |
|---|---|
| `xli daemon --xmpp [--config PATH]` | Run the inbound XMPP daemon. Reads `~/.config/xli/daemon.toml` by default; password from `$XMPP_DAEMON_PASSWORD`. Re-execs through the OMEMO venv. |

The outbound side isn't a CLI subcommand — it's the `xmpp_send` plugin the
agent calls when you ask it to send a message. The script lives at
`~/.config/xli/bin/xmpp_send.py` if you want to invoke it manually.

### CLI — housekeeping

| Command | What it does |
|---|---|
| `xli gc [--dry-run] [--yes]` | Find orphan xAI Collections (registry says project exists, disk says no) and offer to delete them. |
| `xli help` | Grouped command reference. |

### REPL — universal slash commands

These work the same in both `xli code` and `xli chat`:

| Slash | Effect |
|---|---|
| `/help` | Show all slash commands available in this REPL. |
| `/exit`, `/quit` | Leave the REPL. |
| `!<shell>` | Run a shell command **locally** (no agent turn, no tokens). Output streams to your terminal. Use for `clear`, `ls`, ad-hoc utilities. |
| `/sync` | Force a full sync now. |
| `/reset` | Clear conversation history (keeps system prompt + attached docs). |
| `/cost` | Print pricing table + which active models are covered. |
| `/yolo` / `/safe` | Toggle the bash confirmation gate. YOLO skips per-intent prompts. |
| `/models` | Show current orchestrator + worker models and temperatures. |
| `/temp <0.0..2.0>` | Override orchestrator temperature for the next turn only. |
| `/status` | Show project state (collection, pool, mode flags, attached refs/docs). |
| `/projects` | List registered projects (current marked ●). |

### REPL — investigation flow (code mode)

| Slash | Effect |
|---|---|
| `/plan` | Enter plan mode. Read-only investigation tools + scoped scratchpad at `.xli/plan-notes.md`. Resumable across `/exit` and across max-iteration aborts. |
| `/execute` | Approve and execute the plan. Archives the scratchpad to `.xli/plans/approved-<ts>.md`. |
| `/cancel` | Drop plan mode. Archives the scratchpad to `.xli/plans/cancelled-<ts>.md` for recovery. |

### REPL — knowledge layer

| Slash | Effect |
|---|---|
| `/ref [persona]` | Attach a persona's memory to `search_project` (no arg = list). Persists in `.xli/refs.txt`. |
| `/unref <persona>` | Detach a previously-attached persona. |
| `/doc [name]` | Attach a reference doc into the system prompt (no arg = list). Persists in `.xli/docs.txt`. |
| `/undoc <name>` | Detach a previously-attached doc. |
| `/lib` | List subscribed plugins for this project. |
| `/lib all` | Show the global catalog (● = subscribed here). |
| `/lib subscribe <id>` | Subscribe to a plugin for this project. |
| `/lib unsubscribe <id>` | Unsubscribe. |
| `/lib remove <id>` | Delete a plugin from the catalog entirely. |
| `/get <intent>` | Find and invoke a subscribed plugin matching the natural-language intent. |

### REPL — persona-only (chat mode)

| Slash | Effect |
|---|---|
| `/persona <name>` | Switch to another persona mid-session. Saves current state, loads new one's prompt + last-N turns. |
| `/personas` | List personas (current/last-used marked ●). |
| `/edit` | Open the current persona's prompt in `$EDITOR` (takes effect next session). |
| `/forget` | Wipe the current persona's transcript (with y/N confirm). |

### Prompt-prefix indicators

Both REPLs show ambient state in the prompt prefix:

| Prefix | Meaning |
|---|---|
| `›` | Nothing attached, normal mode. |
| `+1d ›` | One doc attached. |
| `+1r ›` | One ref attached. |
| `+1r/2d ›` | One ref + two docs. |
| `+2d! ›` | Trailing `!` means at least one attached doc exceeds the 20kB soft cap. |
| `[plan] +1r ›` | Mode tag combines with attachment counts. |
| `[yolo] ›` | Yolo mode active (bash gate skipped). |

---

## What it isn't

- **It's not Cursor.** No editor wrapper. iXaac runs in a terminal; you bring
  your own editor.
- **It's not MCP.** Plugins are markdown files describing APIs. The agent
  reads them and uses bash. No subprocess servers, no installation friction,
  no ongoing maintenance per-plugin.
- **It's not vendor-curated.** No app store, no marketplace. You write the
  plugins, the personas, the docs, the verbs. The system rewards investment.
- **It's not for casual users.** *"Why isn't there a button for X?"* →
  *"Because it's yours, not ours."* That answer doesn't satisfy everyone, and
  that's fine.

## Who it's for

People who already build their own bash aliases, dotfiles, NixOS configs,
self-hosted services. People who type `:set` reflexively. People who want to
invest in a tool and have the tool reward that investment.

If you bounce off the setup, you're probably in the wrong audience — and
that's a feature, not a bug.

## What's still alpha

- **Plugin authoring wizard** — for now `xli plugin --new` opens `$EDITOR` on
  a template. An interactive `xli plugin add` wizard is designed but not
  built.
- **High-risk plugin gating** — plugins declare a `risk:` field; risk-aware
  confirmation gates beyond the existing bash intent gate are on the roadmap.
- **Some xAI Collections operations are flaky** — `INTERNAL` errors happen
  occasionally on document updates; sync retries on rate-limit but not on
  `INTERNAL` yet.
- **OMEMO trust** is BTBV (blind-trust-before-verification) — pragmatic for a
  Tailscale-only personal substrate, not appropriate for a publicly reachable
  daemon.
- **Reasoning models can rationalize past system-prompt rules.** For
  persona/style work, prefer non-reasoning models like
  `grok-4-1-fast-non-reasoning`. Set with
  `xli models set --orchestrator <name>`.

## License

MIT. See [LICENSE](LICENSE).

## Acknowledgments

iXaac is built on:

- **xAI** — Grok models + Collections (the cloud-knows-your-code primitive)
- **slixmpp** + **slixmpp-omemo** — encrypted XMPP that doesn't suck
- **Prosody** — the XMPP server you'd actually want on your tailnet
- **Tailscale** — private substrate so the XMPP daemon doesn't need to face
  the internet
- **prompt_toolkit** + **rich** — the REPL frontend
- **cryptography** + **keyring** — the credential vault
