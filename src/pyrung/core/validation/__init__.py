"""Validation infrastructure for pyrung programs.

Stage 1: Generic, policy-free walker that extracts operand and condition facts
from a Program object graph. No dialect-specific logic, no severity levels.

Stage 2: Conflicting output target detection for INERT_WHEN_DISABLED=False
instructions with mutual-exclusivity analysis.

Stage 3: Stuck-bit detection for latch/reset imbalances.

Stage 4: Tag-flag validators (readonly writes, choices violations, final
multiple-writers).
"""

from pyrung.core.validation.choices_violation import (
    CORE_CHOICES_VIOLATION,
    ChoicesViolationFinding,
    ChoicesViolationReport,
    validate_choices,
)
from pyrung.core.validation.duplicate_out import (
    CORE_CONFLICTING_OUTPUT,
    ConflictingOutputFinding,
    ConflictingOutputReport,
    OutputSite,
    validate_conflicting_outputs,
)
from pyrung.core.validation.final_writers import (
    CORE_FINAL_MULTIPLE_WRITERS,
    FinalWritersFinding,
    FinalWritersReport,
    validate_final_writers,
)
from pyrung.core.validation.physical_realism import (
    CORE_ANTITOGGLE,
    CORE_MISSING_PROFILE,
    CORE_RANGE_VIOLATION,
    PhysicalRealismFinding,
    PhysicalRealismReport,
    validate_physical_realism,
)
from pyrung.core.validation.readonly_write import (
    CORE_READONLY_WRITE,
    ReadonlyWriteFinding,
    ReadonlyWriteReport,
    validate_readonly_writes,
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
    "CORE_CHOICES_VIOLATION",
    "CORE_ANTITOGGLE",
    "CORE_CONFLICTING_OUTPUT",
    "CORE_FINAL_MULTIPLE_WRITERS",
    "CORE_MISSING_PROFILE",
    "CORE_RANGE_VIOLATION",
    "CORE_READONLY_WRITE",
    "CORE_STUCK_HIGH",
    "CORE_STUCK_LOW",
    "ChoicesViolationFinding",
    "ChoicesViolationReport",
    "ConflictingOutputFinding",
    "ConflictingOutputReport",
    "FactScope",
    "FinalWritersFinding",
    "FinalWritersReport",
    "OperandFact",
    "OutputSite",
    "PhysicalRealismFinding",
    "PhysicalRealismReport",
    "ProgramFacts",
    "ProgramLocation",
    "ReadonlyWriteFinding",
    "ReadonlyWriteReport",
    "StuckBitFinding",
    "StuckBitReport",
    "ValueKind",
    "validate_choices",
    "validate_conflicting_outputs",
    "validate_final_writers",
    "validate_physical_realism",
    "validate_readonly_writes",
    "validate_stuck_bits",
    "walk_program",
]
