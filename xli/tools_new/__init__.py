"""
xli.tools_new — parallel rewrite of the tool system.

This is the safe parallel copy.
Original xli/tools.py remains the live backup until we do the final swap:
  mv xli/tools.py xli/tools_legacy.py
  mv xli/tools_new xli/tools

All new tool code will be developed here.
"""

from __future__ import annotations

from .context import ToolContext, ToolResult
from .plan import t_plan_note, t_read_plan_notes
from .list_dir import t_list_dir
from .glob import t_glob
from .grep import t_grep
from .read_file import t_read_file
from .locate_then_read import t_locate_then_read
from .summarize_file import t_summarize_file
from .search_project import t_search_project
from .web_search import t_web_search
from .x_search import t_x_search
from .code_execute import t_code_execute
from .plugin_search import t_plugin_search
from .plugin_get import t_plugin_get
from .bash import t_bash
from .write_file import t_write_file
from .edit_file import t_edit_file

from xli.tools import (
    REGISTRY as _old_registry,
    WORKER_REGISTRY as _old_worker_registry,
    PARALLEL_SAFE as _old_parallel_safe,
    PLAN_MODE_TOOLS as _old_plan_tools,
    tool_schemas,
    dispatch_subagent_schema,
)

		# Rebuild REGISTRY with owned implementations (plan tools now live here)
REGISTRY = dict(_old_registry)
REGISTRY["plan_note"] = t_plan_note
REGISTRY["read_plan_notes"] = t_read_plan_notes
REGISTRY["list_dir"] = t_list_dir
REGISTRY["glob"] = t_glob
REGISTRY["grep"] = t_grep
REGISTRY["read_file"] = t_read_file
REGISTRY["locate_then_read"] = t_locate_then_read
REGISTRY["summarize_file"] = t_summarize_file
REGISTRY["search_project"] = t_search_project
REGISTRY["web_search"] = t_web_search
REGISTRY["x_search"] = t_x_search
REGISTRY["code_execute"] = t_code_execute
REGISTRY["plugin_search"] = t_plugin_search
REGISTRY["plugin_get"] = t_plugin_get
REGISTRY["bash"] = t_bash
REGISTRY["write_file"] = t_write_file
REGISTRY["edit_file"] = t_edit_file

WORKER_REGISTRY = {
    name: fn for name, fn in REGISTRY.items() if name not in {"write_file", "edit_file"}
}
PARALLEL_SAFE = _old_parallel_safe
PLAN_MODE_TOOLS = _old_plan_tools

__all__ = [
    "ToolContext",
    "ToolResult",
    "REGISTRY",
    "WORKER_REGISTRY",
    "PARALLEL_SAFE",
    "PLAN_MODE_TOOLS",
    "tool_schemas",
    "dispatch_subagent_schema",
]

# TODO: split tool implementations into logical submodules and own the registry here.
