"""Conformance suite -- plain-English cases observed only through slot registers.

Cases live in groups; each group compiles to one CLICK subroutine on a fixed DS
block from a checked-in manifest.  ``run(group)`` runs a group in-process with
exact scan counts.  ``run_modbus(build_program(groups, manifest))`` runs the
same cases over Modbus TCP, against pyrung's soft PLC by default or against a
real CLICK given its ``host[:port]``, all groups or a chosen subset.
"""

from pyrung.conformance._build import ConformanceProgram, GroupBuild, build_program
from pyrung.conformance._case import Case, CaseResult, Group, case
from pyrung.conformance._manifest import GroupLayout, Manifest, ManifestError, load_manifest
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
    "Group",
    "GroupBuild",
    "GroupLayout",
    "Manifest",
    "ManifestError",
    "SimServer",
    "assert_all_passed",
    "build_program",
    "case",
    "load_manifest",
    "parse_endpoint",
    "run",
    "run_modbus",
]
