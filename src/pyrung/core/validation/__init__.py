"""Validation infrastructure for pyrung programs.

Stage 1: Generic, policy-free walker that extracts operand and condition facts
from a Program object graph. No dialect-specific logic, no severity levels.

Stage 2: Conflicting output target detection for INERT_WHEN_DISABLED=False
instructions with mutual-exclusivity analysis.
"""

from pyrung.core.validation.duplicate_out import (
    CORE_CONFLICTING_OUTPUT,
    ConflictingOutputFinding,
    ConflictingOutputReport,
    OutputSite,
    validate_conflicting_outputs,
)
from pyrung.core.validation.walker import (
    FactScope,
    OperandFact,
    ProgramFacts,
    ProgramLocation,
    ValueKind,
    walk_program,
)

__all__ = [
    "CORE_CONFLICTING_OUTPUT",
    "ConflictingOutputFinding",
    "ConflictingOutputReport",
    "FactScope",
    "OperandFact",
    "OutputSite",
    "ProgramFacts",
    "ProgramLocation",
    "ValueKind",
    "validate_conflicting_outputs",
    "walk_program",
]
