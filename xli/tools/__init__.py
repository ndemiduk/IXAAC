"""
xli.tools — tool registry, schemas, and implementations.

All tool code lives in the subpackages (io/, search/, execution/, plugins/, plan/, context/).
Schemas are in schemas.py. No legacy single-file shim remains.
"""

from __future__ import annotations

from .context import ToolContext, ToolResult
from .plan import t_plan_note, t_read_plan_notes

# IO / filesystem tools
from .io.list_dir import t_list_dir
from .io.glob import t_glob
from .io.grep import t_grep
from .io.read_file import t_read_file
from .io.locate_then_read import t_locate_then_read
from .io.summarize_file import t_summarize_file
from .io.write_file import t_write_file
from .io.edit_file import t_edit_file

# Search tools
from .search.search_project import t_search_project
from .search.web_search import t_web_search
from .search.x_search import t_x_search

# Execution / code tools
from .execution.code_execute import t_code_execute
from .execution.bash import t_bash

# Plugin tools
from .plugins.plugin_search import t_plugin_search
from .plugins.plugin_get import t_plugin_get
from .plugins.plugin_call import t_plugin_call

# Bare-name aliases (so `from xli.tools import list_dir` and `tools.list_dir(...)` work
# and the functions shadow any submodule names to avoid package shadowing issues).
list_dir = t_list_dir
glob = t_glob
grep = t_grep
read_file = t_read_file
locate_then_read = t_locate_then_read
summarize_file = t_summarize_file
search_project = t_search_project
web_search = t_web_search
x_search = t_x_search
code_execute = t_code_execute
plugin_search = t_plugin_search
plugin_get = t_plugin_get
plugin_call = t_plugin_call
bash = t_bash
write_file = t_write_file
edit_file = t_edit_file
plan_note = t_plan_note
read_plan_notes = t_read_plan_notes

# Schemas and sets (owned here, extracted from the old monolithic tools.py)
from .schemas import (
    tool_schemas,
    worker_tool_schemas,
    plan_mode_schemas,
    dispatch_subagent_schema,
    PARALLEL_SAFE,
    PLAN_MODE_TOOLS,
)

# REGISTRY built from the t_ implementations we own
REGISTRY = {
    "read_file": t_read_file,
    "list_dir": t_list_dir,
    "glob": t_glob,
    "grep": t_grep,
    "locate_then_read": t_locate_then_read,
    "summarize_file": t_summarize_file,
    "write_file": t_write_file,
    "edit_file": t_edit_file,
    "bash": t_bash,
    "search_project": t_search_project,
    "web_search": t_web_search,
    "x_search": t_x_search,
    "code_execute": t_code_execute,
    "plugin_search": t_plugin_search,
    "plugin_get": t_plugin_get,
    "plugin_call": t_plugin_call,
    "plan_note": t_plan_note,
    "read_plan_notes": t_read_plan_notes,
}

WORKER_REGISTRY = {
    name: fn for name, fn in REGISTRY.items() if name not in {"write_file", "edit_file"}
}

__all__ = [
    "ToolContext",
    "ToolResult",
    "REGISTRY",
    "WORKER_REGISTRY",
    "PARALLEL_SAFE",
    "PLAN_MODE_TOOLS",
    "tool_schemas",
    "worker_tool_schemas",
    "plan_mode_schemas",
    "dispatch_subagent_schema",
]
