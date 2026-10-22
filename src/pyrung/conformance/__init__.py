"""Conformance suite -- plain-English cases observed only through slot registers.

``run(cases)`` runs them in-process with exact scan counts.  ``run_modbus(cases)``
runs the same cases over Modbus TCP, against pyrung's soft PLC by default or
against a real CLICK given its ``host[:port]``.
"""

from pyrung.conformance._build import ConformanceProgram, build_program
from pyrung.conformance._case import Case, CaseResult, case
from pyrung.conformance._modbus import (
    BuildMismatchError,
    ConformanceError,
    Endpoint,
    SimServer,
    parse_endpoint,
    run_modbus,
)
from pyrung.conformance._runner import assert_all_passed, run

__all__ = [
    "BuildMismatchError",
    "Case",
    "CaseResult",
    "ConformanceError",
    "ConformanceProgram",
    "Endpoint",
    "SimServer",
    "assert_all_passed",
    "build_program",
    "case",
    "parse_endpoint",
    "run",
    "run_modbus",
]
