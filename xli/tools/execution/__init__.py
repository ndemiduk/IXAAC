"""
xli.tools.execution — code execution and shell tools.
"""
from .code_execute import t_code_execute as code_execute
from .bash import t_bash as bash

__all__ = ["code_execute", "bash"]
