"""
xli.tools.io — filesystem and file manipulation tools.
"""
# Re-exports for convenience (optional; main registry lives in parent)
from .read_file import t_read_file as read_file
from .write_file import t_write_file as write_file
from .edit_file import t_edit_file as edit_file
from .list_dir import t_list_dir as list_dir
from .glob import t_glob as glob
from .grep import t_grep as grep
from .locate_then_read import t_locate_then_read as locate_then_read
from .summarize_file import t_summarize_file as summarize_file

__all__ = [
    "read_file", "write_file", "edit_file", "list_dir", "glob",
    "grep", "locate_then_read", "summarize_file"
]
