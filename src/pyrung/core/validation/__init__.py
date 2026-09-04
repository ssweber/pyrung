"""Validation infrastructure for pyrung programs.

Stage 1: Generic, policy-free walker that extracts operand and condition facts
from a Program object graph. No dialect-specific logic, no severity levels.

Stage 2: Conflicting output target detection for INERT_WHEN_DISABLED=False
instructions with mutual-exclusivity analysis.

Stage 3: Stuck-bit detection for latch/reset imbalances.

Stage 4: Tag-flag validators (readonly writes, choices violations, final
multiple-writers).

Stage 5: Domain, call-graph, math, redundancy, and ordered-write checks.
"""

from pyrung.core.validation.call_graph import (
    CALL_NEVER_CALLED,
    CALL_RECURSION,
    CallGraphFinding,
    CallGraphReport,
    validate_call_graph,
)
from pyrung.core.validation.choices_violation import (
    TAG_CHOICES_VIOLATION,
    ChoicesViolationFinding,
    ChoicesViolationReport,
    validate_choices,
)
from pyrung.core.validation.cmp_conditions import (
    CMP_ALWAYS_FALSE,
    CMP_ALWAYS_TRUE,
    CMP_EQ_ON_MONOTONE,
    CMP_OPERAND_NO_WRITER,
    CMP_PRESET_STAYS_ZERO,
    CMP_REPEATED_STATE_VALUE,
    CMP_STATIC_ON_LEFT,
    CMP_STEPPER_VALUE_NOT_SET,
    CMP_TRUE_AT_RESET,
    CmpConditionFinding,
    CmpConditionReport,
    validate_cmp_conditions,
)
from pyrung.core.validation.dead_write import (
    TAG_DEAD_WRITE,
    DeadWriteFinding,
    DeadWriteReport,
    validate_dead_writes,
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
from pyrung.core.validation.math_conditions import (
    MATH_DIV_ZERO,
    MathConditionFinding,
    MathConditionReport,
    validate_math_conditions,
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
    PTR_MAY_ESCAPE_BLOCK,
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
    check,
    validate,
)
from pyrung.core.validation.rung_conditions import (
    RUNG_CONTRADICTION,
    RUNG_REDUNDANT_TERM,
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
from pyrung.core.validation.wait_escape import (
    STEP_NO_ESCAPE,
    StepEscapeFinding,
    StepEscapeReport,
    validate_wait_escapes,
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
    "CALL_NEVER_CALLED",
    "CALL_RECURSION",
    "CMP_ALWAYS_FALSE",
    "CMP_ALWAYS_TRUE",
    "MATH_DIV_ZERO",
    "TAG_CHOICES_VIOLATION",
    "COIL_CONFLICTING_OUTPUT",
    "TAG_FINAL_MULTIPLE_WRITERS",
    "PHYS_MISSING_PROFILE",
    "PTR_DEFAULT_BEFORE_BLOCK_START",
    "PTR_MAY_ESCAPE_BLOCK",
    "TAG_RANGE_VIOLATION",
    "TAG_READONLY_WRITE",
    "COIL_STUCK_HIGH",
    "COIL_STUCK_LOW",
    "RUNG_CONTRADICTION",
    "RUNG_REDUNDANT_TERM",
    "RUNG_TAUTOLOGY",
    "CMP_EQ_ON_MONOTONE",
    "CMP_OPERAND_NO_WRITER",
    "CMP_PRESET_STAYS_ZERO",
    "CMP_REPEATED_STATE_VALUE",
    "CMP_STATIC_ON_LEFT",
    "CMP_STEPPER_VALUE_NOT_SET",
    "CMP_TRUE_AT_RESET",
    "STEP_NO_ESCAPE",
    "TAG_DEAD_WRITE",
    "CallGraphFinding",
    "CallGraphReport",
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
    "DeadWriteFinding",
    "DeadWriteReport",
    "MathConditionFinding",
    "MathConditionReport",
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
    "StepEscapeFinding",
    "StepEscapeReport",
    "StuckBitFinding",
    "StuckBitReport",
    "ValidationReport",
    "ValueKind",
    "check",
    "validate",
    "validate_cmp_conditions",
    "validate_call_graph",
    "validate_dead_writes",
    "validate_math_conditions",
    "validate_rung_conditions",
    "validate_choices",
    "validate_conflicting_outputs",
    "validate_final_writers",
    "validate_physical_realism",
    "validate_pointer_defaults",
    "validate_readonly_writes",
    "validate_stuck_bits",
    "validate_wait_escapes",
    "walk_program",
]
