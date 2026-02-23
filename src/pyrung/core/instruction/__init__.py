"""Instruction classes for the immutable PLC engine.

Instructions execute within a ScanContext, writing to batched evolvers.
All state modifications are collected and committed at scan end.
"""

from .advanced import SearchInstruction, ShiftInstruction
from .base import DebugInstructionSubStep, Instruction, OneShotMixin, SubroutineReturnSignal
from .calc import CalcInstruction
from .coils import LatchInstruction, OutInstruction, ResetInstruction
from .control import (
    CallInstruction,
    EnabledFunctionCallInstruction,
    ForLoopInstruction,
    FunctionCallInstruction,
    ReturnInstruction,
)
from .counters import CountDownInstruction, CountUpInstruction
from .data_transfer import BlockCopyInstruction, CopyInstruction, FillInstruction
from .packing import (
    PackBitsInstruction,
    PackTextInstruction,
    PackWordsInstruction,
    UnpackToBitsInstruction,
    UnpackToWordsInstruction,
)
from .resolvers import (
    resolve_block_range_ctx,
    resolve_block_range_tags_ctx,
    resolve_coil_targets_ctx,
    resolve_tag_ctx,
    resolve_tag_name_ctx,
    resolve_tag_or_value_ctx,
)
from .timers import OffDelayInstruction, OnDelayInstruction

__all__ = [
    # Base
    "DebugInstructionSubStep",
    "Instruction",
    "OneShotMixin",
    "SubroutineReturnSignal",
    # Resolvers
    "resolve_block_range_ctx",
    "resolve_block_range_tags_ctx",
    "resolve_coil_targets_ctx",
    "resolve_tag_ctx",
    "resolve_tag_name_ctx",
    "resolve_tag_or_value_ctx",
    # Instructions
    "BlockCopyInstruction",
    "CallInstruction",
    "CopyInstruction",
    "CountDownInstruction",
    "CountUpInstruction",
    "EnabledFunctionCallInstruction",
    "FillInstruction",
    "ForLoopInstruction",
    "FunctionCallInstruction",
    "LatchInstruction",
    "CalcInstruction",
    "OffDelayInstruction",
    "OnDelayInstruction",
    "OutInstruction",
    "PackBitsInstruction",
    "PackTextInstruction",
    "PackWordsInstruction",
    "ResetInstruction",
    "ReturnInstruction",
    "SearchInstruction",
    "ShiftInstruction",
    "UnpackToBitsInstruction",
    "UnpackToWordsInstruction",
]
