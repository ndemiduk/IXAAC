"""
xli.tools.plugins — plugin discovery and invocation tools.
"""
from .plugin_search import t_plugin_search as plugin_search
from .plugin_get import t_plugin_get as plugin_get
from .plugin_call import t_plugin_call as plugin_call

__all__ = ["plugin_search", "plugin_get", "plugin_call"]
