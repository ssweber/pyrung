"""Tumbler test fixtures."""

from __future__ import annotations

import importlib
import sys

import pytest

_PKG = "tests.fixtures.tumbler"


@pytest.fixture
def tumbler_logic():
    """Freshly imported tumbler program.

    The repo-wide autouse ``_clean_block_state`` fixture resets every Block
    before each test, wiping the slot config and init constants (Ref_* command
    values, dh mode-config tables, ...) that the fixture's tags.py stamps at
    import time.  Without those the PackML command handshake silently swallows
    every command.  Re-import the package so each test gets fully initialized
    banks.
    """
    for name in [m for m in sys.modules if m == _PKG or m.startswith(_PKG + ".")]:
        del sys.modules[name]
    module = importlib.import_module(_PKG)
    return module.logic
