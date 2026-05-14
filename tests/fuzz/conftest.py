"""Fuzz test configuration — constants and markers."""

from __future__ import annotations

import os

import pytest

pytestmark = [pytest.mark.hypothesis, pytest.mark.fuzz]

MAX_EXAMPLES = int(os.environ.get("FUZZ_MAX_EXAMPLES", "200"))
MAX_STATES = 10_000
DEPTH_BUDGET = 20
DT = 0.010
REACHABILITY_SCANS = int(os.environ.get("FUZZ_SCANS", "100"))
PARITY_SCANS = int(os.environ.get("FUZZ_SCANS", "50"))
