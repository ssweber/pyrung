"""Automatically generated module split."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pyrung.core.instruction.operands import FitsRegister, NumberInto, Operand, check_fields

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext
    from pyrung.core.instruction.advance import AdvanceProfile


# Immutable execution-plan opcodes.  Rungs classify their contents once while
# the program is built so the scan walker does not repeat concrete-type checks
# for every instruction on every scan.
_EXECUTOR_INSTRUCTION = 0
_EXECUTOR_CALL = 1
_EXECUTOR_FOR_LOOP = 2
_EXECUTOR_RETURN = 3
_EXECUTOR_BRANCH = 4
_EXECUTOR_LOOP_EXIT = 5


@dataclass(frozen=True)
class DebugInstructionSubStep:
    """Per-instruction debug step metadata for chained builder methods."""

    instruction_kind: str
    source_file: str | None
    source_line: int | None
    eval_mode: Literal["enabled", "condition"]
    condition: Any | None = None
    expression: str | None = None


class Instruction(ABC):
    """Base class for all instructions.

    Instructions execute within a ScanContext, writing to batched evolvers.
    All state modifications are collected and committed at scan end.
    """

    source_file: str | None = None
    source_line: int | None = None
    end_line: int | None = None
    debug_substeps: tuple[DebugInstructionSubStep, ...] | None = None
    _state_key: str | None = None
    _site_program_owned: bool = False
    # The binding that stamped ``_state_key`` (see ``Condition._site_owner``).
    _site_owner: object | None = None
    # The edge sites of this instruction's helper legs, as ``(memory key, tag)`` pairs.
    # ``site_memory.build_edge_frames`` stamps them. The box scans them as it reads its
    # pins (``utils.instruction_condition_view``).
    _edge_frame: tuple[tuple[str, str], ...] | None = None
    ALWAYS_EXECUTES: bool = False
    INERT_WHEN_DISABLED: bool = True
    _reads: tuple[str, ...] = ()
    _writes: tuple[str, ...] = ()
    _conditions: tuple[str, ...] = ()
    _structural_fields: tuple[str, ...] = ()
    _exclusive_fields: tuple[str, ...] = ()
    # What each operand field accepts, and the rules that tie two fields together
    # (``pyrung.core.instruction.operands``).
    _operands: ClassVar[dict[str, Operand]] = {}
    _operand_rules: ClassVar[tuple[FitsRegister | NumberInto, ...]] = ()
    # The call that a finding shows for the instruction (``validation.render``). Without
    # _call_operands, the instruction shows as ``verb(target)``.
    # - _call_operands pairs the DSL's name for each positional argument with the
    #   attribute that holds it, in the order that the call takes them. An argument that
    #   holds several attributes names them in a tuple. It shows as the structure that
    #   they are the fields of, such as a timer's UDT, or as the text of the class's
    #   ``_call_argument(name, operand_name)``.
    # - _call_keyword_operands pairs the name of each keyword argument that holds a tag, a
    #   range or a number with its attribute.
    # - _call_keywords names the flags and options.
    # - _call_chained pairs the name of each chained call, such as ``.reset()``, with the
    #   attribute that holds its condition.
    # The call shows a keyword or a chained call only when the instruction sets it.
    _call_operands: tuple[tuple[str, str | tuple[str, ...]], ...] | None = None
    _call_keyword_operands: tuple[tuple[str, str], ...] = ()
    _call_keywords: tuple[str, ...] = ("oneshot",)
    _call_chained: tuple[tuple[str, str], ...] = ()
    # The system fault flags (``fault.*`` tag names) that the instruction can set or
    # clear while executing, beyond its declared destinations.
    _fault_writes: tuple[str, ...] = ()
    # The flags that follow this instruction. On each scan that it runs, it clears them
    # as it starts, before it reads, and then sets each flag that it raises
    # (``guard_oneshot_execution``). A flag stays set from scan to scan until an
    # instruction that follows it clears it. Every name here is in ``_fault_writes`` too.
    _fault_follows: tuple[str, ...] = ()
    # Whether it clears those flags on a scan where it is reached but does not run, as a
    # CLICK Math does. That happens when its rung is false or a one-shot does not fire.
    _fault_settles_idle: bool = False
    _executor_kind: int = _EXECUTOR_INSTRUCTION

    @abstractmethod
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        """Execute this instruction within the given context (internal)."""
        pass

    def exclusive_resources(self) -> list[tuple[str, str]]:
        """Return (resource_type, resource_name) for each exclusively-owned resource.

        A resource is exclusive when only one instruction in a program may
        target it (e.g. a timer accumulator can have only one owner).
        Subclasses declare exclusive fields via ``_exclusive_fields``.
        """
        result: list[tuple[str, str]] = []
        for field_name in self._exclusive_fields:
            tag = getattr(self, field_name)
            result.append((type(self).__name__, tag.name))
        return result

    def check_operands(self, call: str | None = None) -> None:
        """Raise an error for an operand that ``_operands`` or ``_operand_rules`` refuses.

        ``Rung.add_instruction`` calls this method, so the error comes when the program
        is built.  The error names *call*, the call that the author wrote, such as
        ``"calc()"``.  *call* defaults to the call that usually builds the instruction.
        """
        if not self._operands and not self._operand_rules:
            return
        if call is None:
            from pyrung.core.rung import dsl_call

            call = dsl_call(self)
        names = dict.fromkeys(self._operands)
        for rule in self._operand_rules:
            names.update(dict.fromkeys(rule.fields))
        self.check_values(call, {name: getattr(self, name) for name in names})

    @classmethod
    def check_values(cls, call: str, values: Mapping[str, Any]) -> None:
        """Raise an error for a value in *values*, by field name, that the class refuses.

        A builder such as ``shift()`` calls this method before it builds the instruction.
        The error then comes from the line that holds the wrong operand.  The method
        checks only the fields in *values*.
        """
        check_fields(cls._operands, cls._operand_rules, call, values)

    @classmethod
    def walker_fields(cls) -> tuple[str, ...]:
        """Return deterministic field order for generic walkers.

        Field roles are declared on each concrete instruction class.
        Walkers see reads, writes, hidden conditions, and any structural
        fields that should still be surfaced for validation/codegen.
        """
        fields = (
            cls._reads
            + cls._writes
            + cls._conditions
            + cls._structural_fields
            + getattr(cls, "_status_fields", ())
        )
        return tuple(dict.fromkeys(fields))

    def always_execute(self) -> bool:
        """Whether this instruction should execute even when rung is false.

        Override to return True for terminal instructions like counters
        that need to check their conditions independently.
        """
        return self.ALWAYS_EXECUTES

    def is_inert_when_disabled(self) -> bool:
        """Whether this instruction is a no-op when `enabled` is False."""
        return self.INERT_WHEN_DISABLED

    @property
    def state_key(self) -> str:
        """Return the site key that ``bind_sites`` assigned (``i{N}``).

        See :mod:`pyrung.core.site_memory`.
        """
        key = self._state_key
        if key is None:
            from pyrung.core.rung import dsl_call

            raise RuntimeError(
                f"{dsl_call(self)} is not part of a program, so it has no memory of its own. "
                "Build it inside `with Program():`, or bind overlay rungs with "
                "pyrung.core.site_memory.bind_sites()."
            )
        return key

    def memory_key(self, prefix: str) -> str:
        return f"{prefix}:{self.state_key}"

    def has_pins(self) -> bool:
        """Return True if this is a box with pins (``.reset()``, ``.down()``, ``.clock()``, ...).

        A box with pins ends its rung. Nothing else shares its wire tree.
        """
        return False

    def is_terminal(self) -> bool:
        """Whether this instruction must be the last execution item in its flow."""
        return self.has_pins()

    def advance_profile(self) -> AdvanceProfile | None:
        """Describe the next cross-scan operation for owned result channels."""
        return None


class SubroutineReturnSignal(Exception):
    """Internal control-flow signal used by return_early() inside subroutines."""


class LoopBreakSignal(Exception):
    """Internal control-flow signal used by break_loop() inside a forloop body."""


class LoopContinueSignal(Exception):
    """Internal control-flow signal used by continue_loop() inside a forloop body."""


class OneShotMixin:
    """Mixin for instructions that support one-shot mode.

    One-shot instructions execute only once per rung activation.  Each keeps that
    memory in the scan state (``memory_key("_oneshot")``), so each PLC has its own.
    """

    def __init__(self, oneshot: bool = False):
        self._oneshot = oneshot

    @property
    def oneshot(self) -> bool:
        return self._oneshot
