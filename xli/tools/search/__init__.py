"""
xli.tools.search — web, X, and project search tools.
"""
from .search_project import t_search_project as search_project
from .web_search import t_web_search as web_search
from .x_search import t_x_search as x_search

__all__ = ["search_project", "web_search", "x_search"]
