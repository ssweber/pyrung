"""Fuzz test configuration — constants and markers."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.hypothesis, pytest.mark.fuzz]

MAX_STATES = 10_000
DEPTH_BUDGET = 20
DT = 0.010
PARITY_SCANS = 50
