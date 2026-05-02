from __future__ import annotations

PARALLEL_SAFE: set[str] = {
    "read_file",
    "list_dir",
    "glob",
    "grep",
    "locate_then_read",
    "search_project",
    "web_search",
    "x_search",
    "code_execute",
    "plugin_search",
    "plugin_get",
    "plugin_call",
    "dispatch_subagent",
    "summarize_file",
}

# Tools available in PLAN MODE.
#
# Refined invariant: plan mode cannot mutate project content but may write to
# iXaac's own working files. Concretely: every tool here is either read-only
# OR is the scoped scratchpad (plan_note), which can only append to a fixed
# path under .xli/. Bash, dispatch_subagent, write_file, edit_file all stay
# excluded — they can touch project content.
PLAN_MODE_TOOLS: set[str] = {
    "read_file",
    "list_dir",
    "glob",
    "grep",
    "locate_then_read",
    "search_project",
    "web_search",
    "x_search",
    "plugin_get",
    "plan_note",
    "read_plan_notes",
    "summarize_file",
}


def tool_schemas() -> list[dict]:
    """Return OpenAI-compatible tool schemas for chat.completions."""
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a UTF-8 text file from the project (line-numbered output).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Project-relative path"},
                        "offset": {"type": "integer", "description": "0-based start line"},
                        "limit": {"type": "integer", "description": "Max lines to return"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "summarize_file",
                "description": (
                    "Structural map of a Python file: every class, method, and "
                    "top-level function annotated with starting line number and "
                    "length in lines. Cheaper than read_file. Use this output as "
                    "a navigation index — once you know the line/length of the "
                    "block you care about, follow up with read_file(offset, limit) "
                    "to get exactly that range, no exploration needed."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Project-relative path"},
                        "focus": {
                            "type": "string",
                            "description": "What to extract: 'all' (default), 'signatures', 'imports', 'classes', 'functions'",
                        },
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Create or overwrite a file with the given content. Marks file dirty for sync.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": "Replace exact substring in a file. Errors if old_string is not unique unless replace_all=true.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                    },
                    "required": ["path", "old_string", "new_string"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": "List immediate children of a directory.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "default '.'"}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "glob",
                "description": "Recursive glob. Pattern uses fnmatch syntax against project-relative posix paths.",
                "parameters": {
                    "type": "object",
                    "properties": {"pattern": {"type": "string"}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "grep",
                "description": "Recursive regex search across project files. Returns 'path:line: match'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Python regex"},
                        "glob": {"type": "string", "description": "Optional fnmatch filter"},
                        "case_insensitive": {"type": "boolean"},
                    },
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "locate_then_read",
                "description": (
                    "Pattern search + targeted contextual read in ONE call. "
                    "Returns up to 6 matches across project files, each with "
                    "surrounding context (default 5 lines before, 25 after). "
                    "Strongly preferred over the grep + multiple read_file pattern: "
                    "use this when you know what you're looking for and want the "
                    "relevant code blocks immediately, with no follow-up reads needed."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Python regex"},
                        "glob": {"type": "string", "description": "Optional fnmatch path filter"},
                        "context_before": {"type": "integer", "description": "Lines before each match (default 5)"},
                        "context_after": {"type": "integer", "description": "Lines after each match (default 25)"},
                        "case_insensitive": {"type": "boolean"},
                        "max_matches": {"type": "integer", "description": "Max matches to return (default 6)"},
                    },
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": "Run a shell command in the project root. Must honestly declare `intent` (read-only / modifies-project / modifies-system / network). Lying about intent is a serious bug.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "intent": {
                            "type": "string",
                            "enum": ["read-only", "modifies-project", "modifies-system", "network"],
                            "description": "Honest category of what the command does.",
                        },
                        "timeout": {"type": "integer", "description": "Seconds, default 60"},
                    },
                    "required": ["command", "intent"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_project",
                "description": "Hybrid RAG search over the project's codebase. Returns top matching chunks + filenames.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "description": "default 10"},
                        "retrieval_mode": {
                            "type": "string",
                            "enum": ["hybrid", "semantic", "keyword"],
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the live web (current docs, versions, errors outside project). Prefer search_project for in-project info.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Natural language query"},
                        "allowed_domains": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional whitelist of domains to search.",
                        },
                        "excluded_domains": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional blacklist of domains to skip.",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "x_search",
                "description": "Search X (Twitter) posts for real-time chatter or announcements. Prefer web_search for stable docs.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Natural language query about X posts"},
                        "allowed_x_handles": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Restrict to these handles (max 10).",
                        },
                        "excluded_x_handles": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Exclude these handles (max 10).",
                        },
                        "from_date": {"type": "string", "description": "ISO8601 start date (e.g. 2025-01-01)"},
                        "to_date": {"type": "string", "description": "ISO8601 end date"},
                        "enable_image_understanding": {"type": "boolean"},
                        "enable_video_understanding": {"type": "boolean"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "plugin_search",
                "description": "Search subscribed plugins by intent. Returns matches with id/score/risk. Use plugin_get to read full docs.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "intent": {
                            "type": "string",
                            "description": "What data or action you need (e.g. current weather, recent filings).",
                        },
                    },
                    "required": ["intent"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "plugin_get",
                "description": "Read full markdown of a subscribed plugin (endpoints, auth, examples).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Plugin id (from plugin_search results)"},
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "plugin_call",
                "description": (
                    "Invoke a structured plugin action directly — no curl needed. "
                    "Only works for plugins with an actions manifest. Falls back to "
                    "plugin_get + bash for legacy plugins without actions."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "plugin": {
                            "type": "string",
                            "description": "Plugin id (e.g. 'open-meteo').",
                        },
                        "action": {
                            "type": "string",
                            "description": "Action id (e.g. 'current_weather').",
                        },
                        "params": {
                            "type": "object",
                            "description": "Action parameters as key-value pairs.",
                        },
                    },
                    "required": ["plugin", "action", "params"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "plan_note",
                "description": "PLAN-MODE ONLY. Append a note to the persistent scratchpad (.xli/plan-notes.md).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "Note text to append (timestamp added automatically).",
                        },
                        "return_notes_after": {
                            "type": "boolean",
                            "description": "If true, return full updated scratchpad instead of just line count.",
                        },
                    },
                    "required": ["text"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_plan_notes",
                "description": "PLAN-MODE ONLY. Read the current content of .xli/plan-notes.md scratchpad.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "code_execute",
                "description": "Execute Python in xAI sandbox (NumPy/Pandas/etc preinstalled). For project code use bash instead.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "Description of the computation, or Python code to run.",
                        },
                    },
                    "required": ["task"],
                },
            },
        },
    ]


def worker_tool_schemas() -> list[dict]:
    """Schemas for workers — same as main minus write_file/edit_file."""
    blocked = {"write_file", "edit_file"}
    return [s for s in tool_schemas() if s["function"]["name"] not in blocked]


def plan_mode_schemas() -> list[dict]:
    """Schemas available in plan mode — read-only investigation only."""
    return [s for s in tool_schemas() if s["function"]["name"] in PLAN_MODE_TOOLS]


def dispatch_subagent_schema() -> dict:
    """Schema for the dispatch_subagent tool — only available to the main agent."""
    return {
        "type": "function",
        "function": {
            "name": "dispatch_subagent",
            "description": (
                "Dispatch a read-only worker agent. Workers run in parallel, see only the brief, "
                "can use search/read tools but cannot modify files. Returns worker summary."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The investigation task. Be specific.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional snippets, file excerpts, or background the worker needs.",
                    },
                },
                "required": ["task"],
            },
        },
    }
