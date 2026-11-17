"""Fuzz test configuration: constants and Hypothesis profiles.

A generating test carries `@pytest.mark.fuzz` itself. A `pytestmark` in a conftest has no
effect, because pytest reads it only from a test module or class.

`FUZZ_PROFILE=gate` selects the capped profile that the merge queue runs. The default
profile is the longer one that `make test-fuzz` runs by hand.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

MAX_EXAMPLES = int(os.environ.get("FUZZ_MAX_EXAMPLES", "200"))
DT = 0.010
PARITY_SCANS = int(os.environ.get("FUZZ_SCANS", "50"))

# Each profile sets max_examples per test and a deadline per example (ms). The gate bound is
# 400 examples x 60 s per test, with 200 programs for parity. A run on 6 workers measured 104 s.
GATE_MAX_EXAMPLES = 400
# Parity steps two engines for PARITY_SCANS scans per program, so it gets fewer programs.
GATE_PARITY_EXAMPLES = 200
GATE_DEADLINE_MS = 60_000

settings.register_profile(
    "fuzz-long",
    max_examples=MAX_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "fuzz-gate",
    max_examples=GATE_MAX_EXAMPLES,
    deadline=GATE_DEADLINE_MS,
    suppress_health_check=[HealthCheck.too_slow],
)
_GATE = os.environ.get("FUZZ_PROFILE") == "gate"
PROFILE = "fuzz-gate" if _GATE else "fuzz-long"
PARITY_EXAMPLES = GATE_PARITY_EXAMPLES if _GATE else MAX_EXAMPLES
