"""Validation infrastructure for pyrung programs.

Stage 1: Generic, policy-free walker that extracts operand and condition facts
from a Program object graph. No dialect-specific logic, no severity levels.

Stage 2: Conflicting output target detection for INERT_WHEN_DISABLED=False
instructions with mutual-exclusivity analysis.

Stage 3: Stuck-bit detection for latch/reset imbalances.

Stage 4: Tag-flag validators (readonly writes, choices violations, final
multiple-writers).

Stage 5: Pointer default validation for indirect block dereferences.
"""

from pyrung.core.validation.choices_violation import (
    TAG_CHOICES_VIOLATION,
    ChoicesViolationFinding,
    ChoicesViolationReport,
    validate_choices,
)
from pyrung.core.validation.cmp_conditions import (
    CMP_EQ_ON_MONOTONE,
    CMP_STATIC_ON_LEFT,
    CMP_TRUE_AT_RESET,
    CmpConditionFinding,
    CmpConditionReport,
    validate_cmp_conditions,
)
from pyrung.core.validation.display import (
    FindingDisplay,
    Frame,
)
from pyrung.core.validation.duplicate_out import (
    COIL_CONFLICTING_OUTPUT,
    ConflictingOutputFinding,
    ConflictingOutputReport,
    OutputSite,
    validate_conflicting_outputs,
)
from pyrung.core.validation.final_writers import (
    TAG_FINAL_MULTIPLE_WRITERS,
    FinalWritersFinding,
    FinalWritersReport,
    validate_final_writers,
)
from pyrung.core.validation.physical_realism import (
    PHYS_ANTITOGGLE,
    PHYS_MISSING_PROFILE,
    TAG_RANGE_VIOLATION,
    PhysicalRealismFinding,
    PhysicalRealismReport,
    validate_physical_realism,
)
from pyrung.core.validation.pointer_default import (
    PTR_DEFAULT_BEFORE_BLOCK_START,
    PointerDefaultFinding,
    PointerDefaultReport,
    validate_pointer_defaults,
)
from pyrung.core.validation.readonly_write import (
    TAG_READONLY_WRITE,
    ReadonlyWriteFinding,
    ReadonlyWriteReport,
    validate_readonly_writes,
)
from pyrung.core.validation.registry import (
    CATEGORIES,
    RULES,
    RuleSpec,
    ordered_rules,
)
from pyrung.core.validation.report import (
    ALL_RULES,
    Finding,
    ValidationReport,
    validate,
)
from pyrung.core.validation.rung_conditions import (
    RUNG_CONTRADICTION,
    RUNG_TAUTOLOGY,
    RungConditionFinding,
    RungConditionReport,
    validate_rung_conditions,
)
from pyrung.core.validation.stuck_bits import (
    COIL_STUCK_HIGH,
    COIL_STUCK_LOW,
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
    "ALL_RULES",
    "CATEGORIES",
    "RULES",
    "RuleSpec",
    "ordered_rules",
    "FindingDisplay",
    "Frame",
    "PHYS_ANTITOGGLE",
    "TAG_CHOICES_VIOLATION",
    "COIL_CONFLICTING_OUTPUT",
    "TAG_FINAL_MULTIPLE_WRITERS",
    "PHYS_MISSING_PROFILE",
    "PTR_DEFAULT_BEFORE_BLOCK_START",
    "TAG_RANGE_VIOLATION",
    "TAG_READONLY_WRITE",
    "COIL_STUCK_HIGH",
    "COIL_STUCK_LOW",
    "RUNG_CONTRADICTION",
    "RUNG_TAUTOLOGY",
    "CMP_EQ_ON_MONOTONE",
    "CMP_STATIC_ON_LEFT",
    "CMP_TRUE_AT_RESET",
    "ChoicesViolationFinding",
    "ChoicesViolationReport",
    "CmpConditionFinding",
    "CmpConditionReport",
    "ConflictingOutputFinding",
    "ConflictingOutputReport",
    "FactScope",
    "Finding",
    "FinalWritersFinding",
    "FinalWritersReport",
    "OperandFact",
    "OutputSite",
    "PhysicalRealismFinding",
    "PhysicalRealismReport",
    "PointerDefaultFinding",
    "PointerDefaultReport",
    "ProgramFacts",
    "ProgramLocation",
    "ReadonlyWriteFinding",
    "ReadonlyWriteReport",
    "RungConditionFinding",
    "RungConditionReport",
    "StuckBitFinding",
    "StuckBitReport",
    "ValidationReport",
    "ValueKind",
    "validate",
    "validate_cmp_conditions",
    "validate_rung_conditions",
    "validate_choices",
    "validate_conflicting_outputs",
    "validate_final_writers",
    "validate_physical_realism",
    "validate_pointer_defaults",
    "validate_readonly_writes",
    "validate_stuck_bits",
    "walk_program",
]
