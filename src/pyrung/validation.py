"""Read the results of ``Program.check()`` and the project's check settings.

``Program.check()`` runs pyrung's ladder checks and returns a
:class:`ValidationReport`::

    from pathlib import Path

    from pyrung.validation import find_check_config

    config, _path = find_check_config(Path.cwd())
    report = logic.check(select=set(config.resolve()))
    for code, displays in report.grouped().items():
        print(code, len(displays))

:func:`load_check_config` raises ``ValueError`` if ``[tool.pyrung.check]`` names a
rule code or category that pyrung does not know. The message names the file and
the unknown name.

This module does not import pyclickplc. The CLICK checks are in ``pyrung.click``
(``TagMap.validate``), and the CircuitPython checks are in ``pyrung.circuitpy``
(``P1AM.validate``).
"""

from pyrung.core.validation.config import (
    CheckConfig,
    find_check_config,
    load_check_config,
    save_check_config,
)
from pyrung.core.validation.display import FindingDisplay
from pyrung.core.validation.registry import RuleSpec, default_on_rules, ordered_rules
from pyrung.core.validation.report import Finding, ValidationReport

__all__ = [
    "ValidationReport",
    "Finding",
    "FindingDisplay",
    "RuleSpec",
    "ordered_rules",
    "default_on_rules",
    "CheckConfig",
    "load_check_config",
    "save_check_config",
    "find_check_config",
]
