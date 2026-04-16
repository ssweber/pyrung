"""Validation infrastructure for pyrung programs.

Stage 1: Generic, policy-free walker that extracts operand and condition facts
from a Program object graph. No dialect-specific logic, no severity levels.

Stage 2: Conflicting output target detection for INERT_WHEN_DISABLED=False
instructions with mutual-exclusivity analysis.

Stage 3: Stuck-bit detection for latch/reset imbalances.
"""

from pyrung.core.validation.duplicate_out import (
    CORE_CONFLICTING_OUTPUT,
    ConflictingOutputFinding,
    ConflictingOutputReport,
    OutputSite,
    validate_conflicting_outputs,
)
from pyrung.core.validation.stuck_bits import (
    CORE_STUCK_HIGH,
    CORE_STUCK_LOW,
    StuckBitFinding,
    StuckBitReport,
    validate_stuck_bits,
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
    "CORE_STUCK_HIGH",
    "CORE_STUCK_LOW",
    "ConflictingOutputFinding",
    "ConflictingOutputReport",
    "FactScope",
    "OperandFact",
    "OutputSite",
    "ProgramFacts",
    "ProgramLocation",
    "StuckBitFinding",
    "StuckBitReport",
    "ValueKind",
    "validate_conflicting_outputs",
    "validate_stuck_bits",
    "walk_program",
]
