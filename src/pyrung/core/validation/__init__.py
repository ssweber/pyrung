"""Validation infrastructure for pyrung programs.

Stage 1: Generic, policy-free walker that extracts operand and condition facts
from a Program object graph. No dialect-specific logic, no severity levels.
"""

from pyrung.core.validation.walker import (
    FactScope,
    OperandFact,
    ProgramFacts,
    ProgramLocation,
    ValueKind,
    walk_program,
)

__all__ = [
    "FactScope",
    "OperandFact",
    "ProgramFacts",
    "ProgramLocation",
    "ValueKind",
    "walk_program",
]
