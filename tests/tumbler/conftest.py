"""Tumbler test fixtures."""

from __future__ import annotations

import importlib

import pytest

_PKG = "tests.fixtures.tumbler"


@pytest.fixture
def tumbler_logic():
    """Tumbler program, imported once and reused across tests.

    The repo-wide autouse ``_clean_block_state`` fixture snapshots each
    block's import-time slot config on first sight and restores that baseline
    between tests, so the cached module's banks (Ref_* command constants, dh
    mode-config tables, ...) stay configured for every test.
    """
    return importlib.import_module(_PKG).logic
