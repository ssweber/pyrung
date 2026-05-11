"""Automatically generated module split."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, ClassVar

from pyrung.core.instruction import SubroutineReturnSignal
from pyrung.core.rung import Rung as RungLogic

from ..validation import _check_with_body_from_frame

if TYPE_CHECKING:
    from pyrung.core.analysis.dataview import DataView
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.simplified import TerminalForm
    from pyrung.core.context import ScanContext

    from ..validation import DialectValidator


def _validate_subroutine_name(name: str) -> str:
    if '"' in name:
        raise ValueError('Subroutine name must not contain ".')
    return name


class Program:
    """Container for PLC logic (rungs and subroutines).

    Used as a context manager to capture rungs:
        with Program() as logic:
            with Rung(Button):
                out(Light)

    Also works with PLC:
        runner = PLC(logic)
    """

    _active: Program | None = None
    _dialect_validators: ClassVar[dict[str, DialectValidator]] = {}

    def __init__(self, *, strict: bool = True) -> None:
        self._strict = strict
        self.rungs: list[RungLogic] = []
        self.subroutines: dict[str, list[RungLogic]] = {}
        self._current_subroutine: str | None = None  # Track if we're in a subroutine
        self._pending_comment: str | None = None
        self._cached_graph: ProgramGraph | None = None

    def __enter__(self) -> Program:
        if self._strict:
            frame = inspect.currentframe()
            try:
                caller = frame.f_back if frame is not None else None
                if caller is not None:
                    _check_with_body_from_frame(caller, opt_out_hint="Program(strict=False)")
            finally:
                del frame
        Program._active = self
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        Program._active = None
        if exc_type is None:
            self._assign_state_keys()
            self._validate_call_targets()

    def _invalidate_graph_cache(self) -> None:
        self._cached_graph = None

    def _assign_state_keys(self) -> None:
        from pyrung.core.instruction.base import Instruction
        from pyrung.core.instruction.control import ForLoopInstruction

        counter = 0

        def _walk_instructions(instructions: list[Instruction]) -> None:
            nonlocal counter
            for instr in instructions:
                counter += 1
                instr._state_key = f"i{counter}"
                if isinstance(instr, ForLoopInstruction):
                    _walk_instructions(instr.instructions)

        def _walk_rung(rung: RungLogic) -> None:
            _walk_instructions(rung._instructions)
            for branch_rung in rung._branches:
                _walk_rung(branch_rung)

        for rung in self.rungs:
            _walk_rung(rung)
        for subroutine_rungs in self.subroutines.values():
            for rung in subroutine_rungs:
                _walk_rung(rung)

    def _add_rung(self, rung: RungLogic) -> None:
        """Add a rung to the program or current subroutine."""
        target = (
            self.subroutines[self._current_subroutine]
            if self._current_subroutine is not None
            else self.rungs
        )
        if rung._use_prior_snapshot and not target:
            scope = (
                f"subroutine '{self._current_subroutine}'"
                if self._current_subroutine is not None
                else "program"
            )
            raise RuntimeError(
                f"Rung.continued() cannot be the first rung in a {scope}. "
                "It can only reuse the prior rung snapshot in the same execution scope."
            )
        target.append(rung)
        self._invalidate_graph_cache()

    def _start_subroutine(self, name: str) -> None:
        """Start defining a subroutine."""
        _validate_subroutine_name(name)
        self._current_subroutine = name
        self.subroutines[name] = []
        self._invalidate_graph_cache()

    def _end_subroutine(self) -> None:
        """End subroutine definition."""
        self._current_subroutine = None

    def _iter_call_instructions(self):
        """Yield all CallInstruction instances in main rungs and subroutines."""
        from pyrung.core.instruction.control import CallInstruction, ForLoopInstruction

        def _from_instructions(instructions: list[Any]):
            for instruction in instructions:
                if isinstance(instruction, CallInstruction):
                    yield instruction
                if isinstance(instruction, ForLoopInstruction) and hasattr(
                    instruction, "instructions"
                ):
                    yield from _from_instructions(instruction.instructions)

        def _from_rung(rung: RungLogic):
            yield from _from_instructions(rung._instructions)
            for branch in rung._branches:
                yield from _from_rung(branch)

        for rung in self.rungs:
            yield from _from_rung(rung)
        for subroutine_rungs in self.subroutines.values():
            for rung in subroutine_rungs:
                yield from _from_rung(rung)

    def _validate_call_targets(self) -> None:
        """Raise if any call() targets refer to undefined subroutines."""
        for instruction in self._iter_call_instructions():
            if instruction.subroutine_name in self.subroutines:
                continue

            location = ""
            source_file = getattr(instruction, "source_file", None)
            source_line = getattr(instruction, "source_line", None)
            if source_file and source_line:
                location = f" at {source_file}:{source_line}"
            elif source_line:
                location = f" at line {source_line}"

            raise KeyError(
                f"Subroutine '{instruction.subroutine_name}' not defined"
                f" (referenced by call(){location})"
            )

    def _call_subroutine_ctx(self, name: str, ctx: ScanContext) -> None:
        """Execute a subroutine by name within a ScanContext."""
        if name not in self.subroutines:
            raise KeyError(f"Subroutine '{name}' not defined")
        saved_snapshot = ctx._condition_snapshot
        saved_scope_token = ctx._condition_scope_token
        ctx._condition_snapshot = None
        ctx._condition_scope_token = object()
        try:
            for rung in self.subroutines[name]:
                rung.evaluate(ctx)
        except SubroutineReturnSignal:
            pass
        finally:
            ctx._condition_snapshot = saved_snapshot
            ctx._condition_scope_token = saved_scope_token

    @classmethod
    def _current(cls) -> Program | None:
        """Get the current program context (if any)."""
        return cls._active

    @classmethod
    def register_dialect(cls, name: str, validator: DialectValidator) -> None:
        """Register a portability validator callback for a dialect name."""
        existing = cls._dialect_validators.get(name)
        if existing is None:
            cls._dialect_validators[name] = validator
            return
        if existing is validator:
            return
        raise ValueError(f"Dialect {name!r} already registered to a different validator")

    @classmethod
    def registered_dialects(cls) -> tuple[str, ...]:
        """Return registered dialect names in deterministic order."""
        return tuple(sorted(cls._dialect_validators))

    def validate(
        self,
        dialect: str | None = None,
        *,
        mode: str = "warn",
        select: set[str] | None = None,
        ignore: set[str] | None = None,
        dt: float = 0.010,
        **kwargs: Any,
    ) -> Any:
        """Run validation on this Program.

        Without arguments, runs all core validators and returns a
        ``ValidationReport``.  Use ``select`` / ``ignore`` to filter by
        rule code, and ``dt`` to configure the physical-realism validator.

        With a ``dialect`` string, runs dialect-specific portability
        validation (e.g. ``logic.validate("click", mode="strict")``).
        """
        if dialect is not None:
            validator = self._dialect_validators.get(dialect)
            if validator is None:
                available = ", ".join(self.registered_dialects()) or "<none>"
                raise KeyError(
                    f"Unknown validation dialect {dialect!r}. "
                    f"Available dialects: {available}. "
                    f"Import the dialect package first (example: import pyrung.{dialect})."
                )
            return validator(self, mode=mode, **kwargs)
        from pyrung.core.validation.report import validate as _validate_core

        return _validate_core(self, select=select, ignore=ignore, dt=dt)

    def dataview(self) -> DataView:
        """Return a chainable query over this program's tag dependency graph.

        The graph is built lazily on first call and cached.
        """
        if self._cached_graph is None:
            from pyrung.core.analysis import build_program_graph

            self._cached_graph = build_program_graph(self)
        from pyrung.core.analysis.dataview import DataView

        return DataView.from_graph(self._cached_graph)

    def simplified(self) -> dict[str, TerminalForm]:
        """Compute the simplified Boolean form for every terminal tag."""
        from pyrung.core.analysis.simplified import simplified_forms

        return simplified_forms(self)

    def _evaluate(self, ctx: ScanContext) -> None:
        """Evaluate all main rungs in order (not subroutines) within a ScanContext."""
        for rung in self.rungs:
            rung.evaluate(ctx)
