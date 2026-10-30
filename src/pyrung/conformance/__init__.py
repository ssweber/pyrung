"""Conformance suite -- plain-English cases observed only through slot registers.

Cases live in groups; each group compiles to one CLICK subroutine on a fixed DS
block from a checked-in manifest.  ``run(group)`` runs a group in-process with
exact scan counts.  ``run_modbus(build_program(groups, manifest))`` runs the
same cases over Modbus TCP, against pyrung's soft PLC by default, the
program's generated P1AM-200 code run on this machine (``sim=CircuitPySim``),
or a real CLICK given its ``host[:port]``, all groups or a chosen subset.
"""

from pyrung.conformance._build import ConformanceProgram, GroupBuild, build_program
from pyrung.conformance._case import Case, CaseResult, Group, case
from pyrung.conformance._circuitpy import CircuitPySim
from pyrung.conformance._confirm import (
    Confirmation,
    is_confirmed,
    load_confirmations,
    record_confirmations,
)
from pyrung.conformance._manifest import GroupLayout, Manifest, ManifestError, load_manifest
from pyrung.conformance._modbus import (
    BuildMismatchError,
    ConformanceError,
    Endpoint,
    SimServer,
    parse_endpoint,
    read_program_hash,
    run_modbus,
    wait_for_scan_counter_wrap,
)
from pyrung.conformance._runner import assert_all_passed, format_report, run

__all__ = [
    "BuildMismatchError",
    "Case",
    "CaseResult",
    "CircuitPySim",
    "ConformanceError",
    "Confirmation",
    "ConformanceProgram",
    "Endpoint",
    "Group",
    "GroupBuild",
    "GroupLayout",
    "Manifest",
    "ManifestError",
    "SimServer",
    "assert_all_passed",
    "build_program",
    "case",
    "format_report",
    "is_confirmed",
    "load_confirmations",
    "load_manifest",
    "parse_endpoint",
    "read_program_hash",
    "record_confirmations",
    "run",
    "run_modbus",
    "wait_for_scan_counter_wrap",
]
