"""Program and Rung context managers for the immutable PLC engine.

Provides DSL syntax for building PLC programs:

    with Program() as logic:
        with Rung(Button):
            out(Light)

    runner = PLCRunner(logic)
"""

from __future__ import annotations

import ast
import inspect
import textwrap
import warnings
from collections.abc import Callable
from types import FrameType
from typing import TYPE_CHECKING, Any, ClassVar, overload

from pyrung.core.condition import (
    AllCondition,
    AnyCondition,
    Condition,
    FallingEdgeCondition,
    NormallyClosedCondition,
    RisingEdgeCondition,
)
from pyrung.core.instruction import (
    AsyncFunctionCallInstruction,
    BlockCopyInstruction,
    CallInstruction,
    CopyInstruction,
    CountDownInstruction,
    CountUpInstruction,
    FillInstruction,
    ForLoopInstruction,
    FunctionCallInstruction,
    LatchInstruction,
    MathInstruction,
    OffDelayInstruction,
    OnDelayInstruction,
    OutInstruction,
    PackBitsInstruction,
    PackWordsInstruction,
    ResetInstruction,
    ReturnInstruction,
    SearchInstruction,
    ShiftInstruction,
    SubroutineReturnSignal,
    UnpackToBitsInstruction,
    UnpackToWordsInstruction,
)
from pyrung.core.memory_block import BlockRange
from pyrung.core.rung import Rung as RungLogic
from pyrung.core.tag import Tag, TagType
from pyrung.core.time_mode import TimeUnit

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext
    from pyrung.core.memory_block import IndirectBlockRange, IndirectExprRef, IndirectRef
    from pyrung.core.state import SystemState


# Context stack for tracking current rung
_rung_stack: list[Rung] = []
_forloop_active = False


def _current_rung() -> Rung | None:
    """Get the current rung context (if any)."""
    return _rung_stack[-1] if _rung_stack else None


def _require_rung_context(func_name: str) -> Rung:
    """Get current rung or raise error."""
    rung = _current_rung()
    if rung is None:
        raise RuntimeError(f"{func_name}() must be called inside a Rung context")
    return rung


def _iter_coil_tags(target: Tag | BlockRange) -> list[Tag]:
    """Normalize a coil target to concrete tags."""
    if isinstance(target, Tag):
        return [target]
    if isinstance(target, BlockRange):
        return target.tags()
    raise TypeError(f"Expected Tag or BlockRange from .select(), got {type(target).__name__}")


def _validate_function_call(
    fn: Any,
    ins: dict[str, Any] | None,
    outs: dict[str, Any] | None,
    *,
    func_name: str,
    has_enabled: bool = False,
) -> None:
    """Validate callback contract for run_function/run_enabled_function DSL entry points."""
    if not callable(fn):
        raise TypeError(f"{func_name}() fn must be callable, got {type(fn).__name__}")

    if inspect.iscoroutinefunction(fn) or inspect.iscoroutinefunction(type(fn).__call__):
        raise TypeError(f"{func_name}() fn must be synchronous (async def is not supported)")

    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{func_name}() could not inspect function signature") from exc

    if ins is not None:
        if not isinstance(ins, dict):
            raise TypeError(f"{func_name}() ins must be a dict, got {type(ins).__name__}")
    elif has_enabled:
        ins = {}

    if ins is not None:
        bind_kwargs = {k: object() for k in ins}
        bind_args = (object(),) if has_enabled else ()
        try:
            signature.bind(*bind_args, **bind_kwargs)
        except TypeError as exc:
            raise TypeError(
                f"{func_name}() ins keys {sorted(ins.keys())} incompatible with "
                f"{getattr(fn, '__name__', repr(fn))!r} signature"
            ) from exc

    if outs is not None and not isinstance(outs, dict):
        raise TypeError(f"{func_name}() outs must be a dict, got {type(outs).__name__}")


class ForbiddenControlFlowError(RuntimeError):
    """Raised when Python control flow is used inside strict DSL scope."""


_IF_HINT = "Use `Rung(condition)` to express conditional logic"
_BOOL_HINT = "Use `all_of()` / `any_of()` for compound conditions"
_NOT_HINT = "Use `nc()` for normally-closed contacts"
_LOOP_HINT = "Each rung is independent; express repeated patterns as separate rungs"
_ASSIGN_HINT = "DSL instructions write to tags directly; no intermediate Python variables needed"
_TRY_HINT = "Errors in DSL scope are programming mistakes; no recovery logic in ladder logic"
_COMPREHENSION_HINT = (
    "Build tag collections outside the Program scope, then reference them in rungs"
)
_SCOPE_HINT = "DSL scope should not mutate external Python state"
_RETURN_HINT = "Use `return_()` for early subroutine exit; no Python control flow in DSL scope"
_IMPORT_HINT = "Move imports outside the Program/subroutine scope"
_ASSERT_HINT = "Not valid in ladder logic; handle validation outside DSL scope"
_DEF_HINT = "Define functions and classes outside the Program/subroutine scope"
_GENERIC_STMT_HINT = "Only `with ...:`, bare function calls, and `pass` are allowed in DSL scope"

DialectValidator = Callable[..., Any]


def _warn_check_skipped(target: str, reason: Exception) -> None:
    """Warn and skip strict checking when source inspection/parsing is unavailable."""
    warnings.warn(
        f"Unable to perform strict DSL control-flow check for {target}: {reason}",
        RuntimeWarning,
        stacklevel=3,
    )


def _absolute_line(node: ast.AST, line_offset: int) -> int:
    lineno = getattr(node, "lineno", 1)
    return line_offset + lineno - 1


def _describe_forbidden_node(node: ast.AST) -> tuple[str, str]:
    """Return user-facing construct label and DSL hint for a forbidden node."""
    if isinstance(node, (ast.If, ast.IfExp)):
        return "if/elif/else", _IF_HINT
    if isinstance(node, ast.BoolOp):
        return "and/or", _BOOL_HINT
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return "not", _NOT_HINT
    if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
        return "for/while", _LOOP_HINT
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
        return "assignment", _ASSIGN_HINT
    if isinstance(node, ast.Try):
        return "try/except", _TRY_HINT
    if isinstance(
        node,
        (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.comprehension),
    ):
        return "comprehension/generator", _COMPREHENSION_HINT
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return "global/nonlocal", _SCOPE_HINT
    if isinstance(node, (ast.Yield, ast.YieldFrom, ast.Await, ast.Return)):
        return "yield/await/return", _RETURN_HINT
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return "import", _IMPORT_HINT
    if isinstance(node, (ast.Assert, ast.Raise, ast.Delete)):
        return "assert/raise/del", _ASSERT_HINT
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return type(node).__name__, _DEF_HINT
    if isinstance(node, ast.Expr):
        return "expression statement", "Only bare call expressions are allowed as statements"
    if isinstance(node, ast.stmt):
        return type(node).__name__, _GENERIC_STMT_HINT
    return type(node).__name__, "This construct is not allowed in strict DSL scope"


def _raise_forbidden_node(
    node: ast.AST,
    *,
    filename: str,
    line_offset: int,
    opt_out_hint: str,
) -> None:
    construct, hint = _describe_forbidden_node(node)
    line = _absolute_line(node, line_offset)
    raise ForbiddenControlFlowError(
        f"{filename}:{line}: forbidden Python construct '{construct}' in strict DSL scope. "
        f"{hint}. Opt out with {opt_out_hint}."
    )


def _iter_expression_nodes(node: ast.AST) -> list[ast.AST]:
    """Iterate expression graph for a statement, excluding nested statements."""
    nodes: list[ast.AST] = []
    stack: list[ast.AST] = [node]
    while stack:
        current = stack.pop()
        nodes.append(current)
        children = list(ast.iter_child_nodes(current))
        for child in reversed(children):
            if isinstance(child, ast.stmt):
                continue
            stack.append(child)
    return nodes


def _check_expression_tree(
    node: ast.AST,
    *,
    filename: str,
    line_offset: int,
    opt_out_hint: str,
) -> None:
    for child in _iter_expression_nodes(node):
        if isinstance(child, ast.BoolOp):
            _raise_forbidden_node(
                child,
                filename=filename,
                line_offset=line_offset,
                opt_out_hint=opt_out_hint,
            )
        if isinstance(child, ast.UnaryOp) and isinstance(child.op, ast.Not):
            _raise_forbidden_node(
                child,
                filename=filename,
                line_offset=line_offset,
                opt_out_hint=opt_out_hint,
            )
        if isinstance(
            child,
            (
                ast.IfExp,
                ast.NamedExpr,
                ast.Await,
                ast.Yield,
                ast.YieldFrom,
                ast.ListComp,
                ast.SetComp,
                ast.DictComp,
                ast.GeneratorExp,
                ast.comprehension,
            ),
        ):
            _raise_forbidden_node(
                child,
                filename=filename,
                line_offset=line_offset,
                opt_out_hint=opt_out_hint,
            )


def _check_statement_list(
    statements: list[ast.stmt],
    *,
    filename: str,
    line_offset: int,
    opt_out_hint: str,
) -> None:
    for statement in statements:
        if isinstance(statement, ast.Pass):
            continue

        if isinstance(statement, ast.Expr):
            if not isinstance(statement.value, ast.Call):
                _raise_forbidden_node(
                    statement,
                    filename=filename,
                    line_offset=line_offset,
                    opt_out_hint=opt_out_hint,
                )
            _check_expression_tree(
                statement.value,
                filename=filename,
                line_offset=line_offset,
                opt_out_hint=opt_out_hint,
            )
            continue

        if isinstance(statement, ast.With):
            for item in statement.items:
                _check_expression_tree(
                    item.context_expr,
                    filename=filename,
                    line_offset=line_offset,
                    opt_out_hint=opt_out_hint,
                )
                if item.optional_vars is not None:
                    _check_expression_tree(
                        item.optional_vars,
                        filename=filename,
                        line_offset=line_offset,
                        opt_out_hint=opt_out_hint,
                    )
            _check_statement_list(
                statement.body,
                filename=filename,
                line_offset=line_offset,
                opt_out_hint=opt_out_hint,
            )
            continue

        _raise_forbidden_node(
            statement,
            filename=filename,
            line_offset=line_offset,
            opt_out_hint=opt_out_hint,
        )


def _check_function_body_strict(
    fn: Callable[[], None],
    *,
    opt_out_hint: str,
    source_label: str,
) -> None:
    try:
        source_lines, start_line = inspect.getsourcelines(fn)
        source = textwrap.dedent("".join(source_lines))
        module = ast.parse(source)
    except (OSError, TypeError, SyntaxError) as exc:
        _warn_check_skipped(source_label, exc)
        return

    function_nodes = [
        node for node in module.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if not function_nodes:
        _warn_check_skipped(source_label, RuntimeError("function body AST not found"))
        return

    filename = inspect.getsourcefile(fn) or inspect.getfile(fn)
    _check_statement_list(
        function_nodes[0].body,
        filename=filename,
        line_offset=start_line - 1,
        opt_out_hint=opt_out_hint,
    )


def _find_enclosing_with(module: ast.Module, line_number: int) -> ast.With | None:
    matches: list[ast.With] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.With):
            continue
        end_line = getattr(node, "end_lineno", node.lineno)
        if node.lineno <= line_number <= end_line:
            matches.append(node)

    if not matches:
        return None
    return max(matches, key=lambda node: node.lineno)


def _check_with_body_from_frame(frame: FrameType, *, opt_out_hint: str) -> None:
    code = frame.f_code
    source_label = f"{code.co_filename}:{frame.f_lineno}"
    try:
        source_lines, start_line = inspect.getsourcelines(code)
        source = textwrap.dedent("".join(source_lines))
        module = ast.parse(source)
    except (OSError, TypeError, SyntaxError) as exc:
        _warn_check_skipped(source_label, exc)
        return

    relative_line = frame.f_lineno - start_line + 1
    with_node = _find_enclosing_with(module, relative_line)
    if with_node is None:
        _warn_check_skipped(source_label, RuntimeError("enclosing with-statement AST not found"))
        return

    filename = inspect.getsourcefile(code) or code.co_filename
    _check_statement_list(
        with_node.body,
        filename=filename,
        line_offset=start_line - 1,
        opt_out_hint=opt_out_hint,
    )


class Program:
    """Container for PLC logic (rungs and subroutines).

    Used as a context manager to capture rungs:
        with Program() as logic:
            with Rung(Button):
                out(Light)

    Also works with PLCRunner:
        runner = PLCRunner(logic)
    """

    _current: Program | None = None
    _dialect_validators: ClassVar[dict[str, DialectValidator]] = {}

    def __init__(self, *, strict: bool = True) -> None:
        self._strict = strict
        self.rungs: list[RungLogic] = []
        self.subroutines: dict[str, list[RungLogic]] = {}
        self._current_subroutine: str | None = None  # Track if we're in a subroutine

    def __enter__(self) -> Program:
        if self._strict:
            frame = inspect.currentframe()
            try:
                caller = frame.f_back if frame is not None else None
                if caller is not None:
                    _check_with_body_from_frame(caller, opt_out_hint="Program(strict=False)")
            finally:
                del frame
        Program._current = self
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        Program._current = None

    def add_rung(self, rung: RungLogic) -> None:
        """Add a rung to the program or current subroutine."""
        if self._current_subroutine is not None:
            self.subroutines[self._current_subroutine].append(rung)
        else:
            self.rungs.append(rung)

    def start_subroutine(self, name: str) -> None:
        """Start defining a subroutine."""
        self._current_subroutine = name
        self.subroutines[name] = []

    def end_subroutine(self) -> None:
        """End subroutine definition."""
        self._current_subroutine = None

    def call_subroutine(self, name: str, state: SystemState) -> SystemState:
        """Execute a subroutine by name (legacy state-based API)."""
        # This method is kept for backwards compatibility but should not be used
        # with ScanContext-based execution. Use call_subroutine_ctx instead.
        from pyrung.core.context import ScanContext

        if name not in self.subroutines:
            raise KeyError(f"Subroutine '{name}' not defined")
        ctx = ScanContext(state)
        for rung in self.subroutines[name]:
            rung.evaluate(ctx)
        return ctx.commit(dt=0.0)

    def call_subroutine_ctx(self, name: str, ctx: ScanContext) -> None:
        """Execute a subroutine by name within a ScanContext."""
        if name not in self.subroutines:
            raise KeyError(f"Subroutine '{name}' not defined")
        try:
            for rung in self.subroutines[name]:
                rung.evaluate(ctx)
        except SubroutineReturnSignal:
            return

    @classmethod
    def current(cls) -> Program | None:
        """Get the current program context (if any)."""
        return cls._current

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

    def validate(self, dialect: str, *, mode: str = "warn", **kwargs: Any) -> Any:
        """Run dialect-specific portability validation for this Program."""
        validator = self._dialect_validators.get(dialect)
        if validator is None:
            available = ", ".join(self.registered_dialects()) or "<none>"
            raise KeyError(
                f"Unknown validation dialect {dialect!r}. "
                f"Available dialects: {available}. "
                f"Import the dialect package first (example: import pyrung.{dialect})."
            )
        return validator(self, mode=mode, **kwargs)

    def evaluate(self, ctx: ScanContext) -> None:
        """Evaluate all main rungs in order (not subroutines) within a ScanContext."""
        for rung in self.rungs:
            rung.evaluate(ctx)


class Rung:
    """Context manager for defining a rung.

    Example:
        with Rung(Button):
            out(Light)

        with Rung(Step == 0):
            out(Light1)
            copy(1, Step, oneshot=True)
    """

    def __init__(self, *conditions: Condition | Tag) -> None:
        self._rung = RungLogic(*conditions)

    def __enter__(self) -> Rung:
        _rung_stack.append(self)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        _rung_stack.pop()
        # Add rung to current program
        prog = Program.current()
        if prog is not None:
            prog.add_rung(self._rung)


# ============================================================================
# DSL Functions - called inside Rung context
# ============================================================================


def out(target: Tag | BlockRange, oneshot: bool = False) -> Tag | BlockRange:
    """Output coil instruction (OUT).

    Sets target to True when rung is true.
    Resets to False when rung goes false.

    Example:
        with Rung(Button):
            out(Light)
            out(Y.select(1, 4))
    """
    ctx = _require_rung_context("out")
    for coil_tag in _iter_coil_tags(target):
        ctx._rung.register_coil(coil_tag)
    ctx._rung.add_instruction(OutInstruction(target, oneshot))
    return target


def latch(target: Tag | BlockRange) -> Tag | BlockRange:
    """Latch/Set instruction (SET).

    Sets target to True. Unlike OUT, does NOT reset when rung goes false.
    Use reset() to turn off.

    Example:
        with Rung(StartButton):
            latch(MotorRunning)
            latch(C.select(1, 8))
    """
    ctx = _require_rung_context("latch")
    _iter_coil_tags(target)
    ctx._rung.add_instruction(LatchInstruction(target))
    return target


def reset(target: Tag | BlockRange) -> Tag | BlockRange:
    """Reset/Unlatch instruction (RST).

    Sets target to its default value (False for bits, 0 for ints).

    Example:
        with Rung(StopButton):
            reset(MotorRunning)
            reset(C.select(1, 8))
    """
    ctx = _require_rung_context("reset")
    _iter_coil_tags(target)
    ctx._rung.add_instruction(ResetInstruction(target))
    return target


def copy(
    source: Any,
    target: Tag | IndirectRef | IndirectExprRef,
    oneshot: bool = False,
) -> Tag | IndirectRef | IndirectExprRef:
    """Copy instruction (CPY/MOV).

    Copies source value to target.

    Example:
        with Rung(Button):
            copy(5, StepNumber)
    """
    ctx = _require_rung_context("copy")
    ctx._rung.add_instruction(CopyInstruction(source, target, oneshot))
    return target


def run_function(
    fn: Callable[..., dict[str, Any]],
    ins: dict[str, Tag | IndirectRef | IndirectExprRef | Any] | None = None,
    outs: dict[str, Tag | IndirectRef | IndirectExprRef] | None = None,
    *,
    oneshot: bool = False,
) -> None:
    """Execute a synchronous function when rung power is true."""
    ctx = _require_rung_context("run_function")
    _validate_function_call(fn, ins, outs, func_name="run_function")
    ctx._rung.add_instruction(FunctionCallInstruction(fn, ins, outs, oneshot))


def run_enabled_function(
    fn: Callable[..., dict[str, Any]],
    ins: dict[str, Tag | IndirectRef | IndirectExprRef | Any] | None = None,
    outs: dict[str, Tag | IndirectRef | IndirectExprRef] | None = None,
) -> None:
    """Execute a synchronous function every scan with rung enabled state."""
    ctx = _require_rung_context("run_enabled_function")
    _validate_function_call(fn, ins, outs, func_name="run_enabled_function", has_enabled=True)
    enable_condition = ctx._rung._get_combined_condition()
    ctx._rung.add_instruction(AsyncFunctionCallInstruction(fn, ins, outs, enable_condition))


def blockcopy(source: Any, dest: Any, oneshot: bool = False) -> None:
    """Block copy instruction.

    Copies values from source BlockRange to dest BlockRange.
    Both ranges must have the same length.

    Example:
        with Rung(CopyEnable):
            blockcopy(DS.select(1, 10), DD.select(1, 10))

    Args:
        source: Source BlockRange or IndirectBlockRange from .select().
        dest: Dest BlockRange or IndirectBlockRange from .select().
        oneshot: If True, execute only once per rung activation.
    """
    ctx = _require_rung_context("blockcopy")
    ctx._rung.add_instruction(BlockCopyInstruction(source, dest, oneshot))


def fill(value: Any, dest: Any, oneshot: bool = False) -> None:
    """Fill instruction.

    Writes a constant value to every element in a BlockRange.

    Example:
        with Rung(ClearEnable):
            fill(0, DS.select(1, 100))

    Args:
        value: Value to write (literal, Tag, or Expression).
        dest: Dest BlockRange or IndirectBlockRange from .select().
        oneshot: If True, execute only once per rung activation.
    """
    ctx = _require_rung_context("fill")
    ctx._rung.add_instruction(FillInstruction(value, dest, oneshot))


def pack_bits(bit_block: Any, dest: Any, oneshot: bool = False) -> None:
    """Pack BOOL tags from a BlockRange into a register destination."""
    ctx = _require_rung_context("pack_bits")
    ctx._rung.add_instruction(PackBitsInstruction(bit_block, dest, oneshot))


def pack_words(word_block: Any, dest: Any, oneshot: bool = False) -> None:
    """Pack two 16-bit tags from a BlockRange into a 32-bit destination."""
    ctx = _require_rung_context("pack_words")
    ctx._rung.add_instruction(PackWordsInstruction(word_block, dest, oneshot))


def unpack_to_bits(source: Any, bit_block: Any, oneshot: bool = False) -> None:
    """Unpack a register source into BOOL tags in a BlockRange."""
    ctx = _require_rung_context("unpack_to_bits")
    ctx._rung.add_instruction(UnpackToBitsInstruction(source, bit_block, oneshot))


def unpack_to_words(source: Any, word_block: Any, oneshot: bool = False) -> None:
    """Unpack a 32-bit register source into two 16-bit tags in a BlockRange."""
    ctx = _require_rung_context("unpack_to_words")
    ctx._rung.add_instruction(UnpackToWordsInstruction(source, word_block, oneshot))


def math(expression: Any, dest: Tag, oneshot: bool = False, mode: str = "decimal") -> Tag:
    """Math instruction.

    Evaluates an expression and stores the result in dest, with
    truncation to the destination tag's bit width (modular wrapping).

    Key differences from copy():
    - Truncates result to destination tag's type width
    - Division by zero produces 0 (not infinity)
    - Supports "decimal" (signed) and "hex" (unsigned 16-bit) modes

    Example:
        with Rung(Enable):
            math(DS1 * DS2 + DS3, Result)
            math(MaskA & MaskB, MaskResult, mode="hex")

    Args:
        expression: Expression, Tag, or literal to evaluate.
        dest: Destination tag (type determines truncation width).
        oneshot: If True, execute only once per rung activation.
        mode: "decimal" (signed arithmetic) or "hex" (unsigned 16-bit wrap).

    Returns:
        The dest tag.
    """
    ctx = _require_rung_context("math")
    ctx._rung.add_instruction(MathInstruction(expression, dest, oneshot, mode))
    return dest


def search(
    condition: str,
    value: Any,
    search_range: BlockRange | IndirectBlockRange,
    result: Tag,
    found: Tag,
    continuous: bool = False,
    oneshot: bool = False,
) -> Tag:
    """Search instruction.

    Scans a selected range and writes the first matching address into `result`.
    Writes `found` True on hit; on miss writes `result=-1` and `found=False`.
    """
    from pyrung.core.memory_block import BlockRange, IndirectBlockRange
    from pyrung.core.tag import TagType

    if condition not in {"==", "!=", "<", "<=", ">", ">="}:
        raise ValueError(
            f"Invalid search condition: {condition!r}. Expected one of: ==, !=, <, <=, >, >="
        )
    if not isinstance(search_range, (BlockRange, IndirectBlockRange)):
        raise TypeError(
            "search() expects search_range from .select() "
            f"(BlockRange or IndirectBlockRange), got {type(search_range).__name__}"
        )
    if found.type != TagType.BOOL:
        raise TypeError(f"search() found must be BOOL, got {found.type.name}")
    if result.type not in {TagType.INT, TagType.DINT}:
        raise TypeError(f"search() result must be INT or DINT, got {result.type.name}")

    ctx = _require_rung_context("search")
    ctx._rung.add_instruction(
        SearchInstruction(condition, value, search_range, result, found, continuous, oneshot)
    )
    return result


class ShiftBuilder:
    """Builder for shift instruction with required .clock().reset() chaining."""

    def __init__(
        self,
        bit_range: BlockRange | IndirectBlockRange,
        data_condition: Any,
    ):
        self._bit_range = bit_range
        self._data_condition = data_condition
        self._clock_condition: Condition | Tag | None = None
        self._rung = _require_rung_context("shift")

    def clock(self, condition: Condition | Tag) -> ShiftBuilder:
        """Set the shift clock trigger condition."""
        self._clock_condition = condition
        return self

    def reset(self, condition: Condition | Tag) -> BlockRange | IndirectBlockRange:
        """Finalize the shift instruction with required reset condition."""
        if self._clock_condition is None:
            raise RuntimeError("shift().clock(...) must be called before shift().reset(...)")

        instr = ShiftInstruction(
            bit_range=self._bit_range,
            data_condition=self._data_condition,
            clock_condition=self._clock_condition,
            reset_condition=condition,
        )
        self._rung._rung.add_instruction(instr)
        return self._bit_range


def shift(bit_range: BlockRange | IndirectBlockRange) -> ShiftBuilder:
    """Shift register instruction builder.

    Data input comes from current rung power. Use .clock(...) then .reset(...)
    to finalize and add the instruction.

    Example:
        with Rung(DataBit):
            shift(C.select(2, 7)).clock(ClockBit).reset(ResetBit)
    """
    from pyrung.core.memory_block import BlockRange, IndirectBlockRange

    if not isinstance(bit_range, (BlockRange, IndirectBlockRange)):
        raise TypeError(
            f"shift() expects a BlockRange or IndirectBlockRange from .select(), "
            f"got {type(bit_range).__name__}"
        )

    ctx = _require_rung_context("shift")
    data_condition = ctx._rung._get_combined_condition()
    return ShiftBuilder(bit_range, data_condition)


# ============================================================================
# Condition helpers - used in Rung conditions
# ============================================================================


def nc(tag: Tag) -> NormallyClosedCondition:
    """Normally closed contact (XIO).

    True when tag is False/0.

    Example:
        with Rung(StartButton, nc(StopButton)):
            latch(MotorRunning)
    """
    return NormallyClosedCondition(tag)


def rise(tag: Tag) -> RisingEdgeCondition:
    """Rising edge contact (RE).

    True only on 0->1 transition. Requires PLCRunner to track previous values.

    Example:
        with Rung(rise(Button)):
            latch(MotorRunning)  # Latches on button press, not while held
    """
    return RisingEdgeCondition(tag)


def fall(tag: Tag) -> FallingEdgeCondition:
    """Falling edge contact (FE).

    True only on 1->0 transition. Requires PLCRunner to track previous values.

    Example:
        with Rung(fall(Button)):
            reset(MotorRunning)  # Resets when button is released
    """
    return FallingEdgeCondition(tag)


def any_of(
    *conditions: Condition | Tag,
) -> AnyCondition:
    """OR condition - true when any sub-condition is true.

    Use this to combine multiple conditions with OR logic within a rung.
    Multiple conditions passed directly to Rung() are ANDed together.

    Example:
        with Rung(Step == 1, any_of(Start, CmdStart)):
            out(Light)  # True if Step==1 AND (Start OR CmdStart)

        # Also works with | operator:
        with Rung(Step == 1, Start | CmdStart):
            out(Light)

        # Grouped AND inside OR (explicit):
        with Rung(any_of(Start, all_of(AutoMode, Ready), RemoteStart)):
            out(Light)

    Args:
        conditions: Conditions to OR together.

    Returns:
        AnyCondition that evaluates True if any sub-condition is True.
    """
    return AnyCondition(*conditions)


def all_of(
    *conditions: Condition | Tag | tuple[Condition | Tag, ...] | list[Condition | Tag],
) -> AllCondition:
    """AND condition - true when all sub-conditions are true.

    This is equivalent to comma-separated rung conditions, but useful when building
    grouped condition trees with any_of() or `&`.

    Example:
        with Rung(all_of(Ready, AutoMode)):
            out(StartPermissive)

        # Equivalent operator form:
        with Rung((Ready & AutoMode) | RemoteStart):
            out(StartPermissive)
    """
    return AllCondition(*conditions)


def call(target: str | SubroutineFunc) -> None:
    """Call a subroutine instruction.

    Executes the named subroutine when the rung is true.
    Accepts either a string name or a @subroutine-decorated function.

    Example:
        with Rung(Button):
            call("init_sequence")

        with subroutine("init_sequence"):
            with Rung():
                out(Light)

        # Or with decorator:
        @subroutine("init")
        def init_sequence():
            with Rung():
                out(Light)

        with Program() as logic:
            with Rung(Button):
                call(init_sequence)
    """
    ctx = _require_rung_context("call")
    prog = Program.current()
    if prog is None:
        raise RuntimeError("call() must be used inside a Program context")

    if isinstance(target, SubroutineFunc):
        name = target.name
        if name not in prog.subroutines:
            target._register(prog)
    else:
        name = target

    ctx._rung.add_instruction(CallInstruction(name, prog))


def return_() -> None:
    """Return from the current subroutine.

    Example:
        with subroutine("my_sub"):
            with Rung(Abort):
                return_()
    """
    ctx = _require_rung_context("return_")
    prog = Program.current()
    if prog is None or prog._current_subroutine is None:
        raise RuntimeError("return_() must be used inside a subroutine")
    ctx._rung.add_instruction(ReturnInstruction())


# ============================================================================
# Decorator
# ============================================================================


@overload
def program(fn: Callable[[], None], /) -> Program: ...


@overload
def program(
    fn: None = None,
    /,
    *,
    strict: bool = True,
) -> Callable[[Callable[[], None]], Program]: ...


def program(
    fn: Callable[[], None] | None = None,
    /,
    *,
    strict: bool = True,
) -> Program | Callable[[Callable[[], None]], Program]:
    """Decorator to create a Program from a function.

    Example:
        @program
        def my_logic():
            with Rung(Button):
                out(Light)

        @program(strict=False)
        def permissive_logic():
            with Rung(Button):
                out(Light)

        runner = PLCRunner(my_logic)
    """

    def _decorate(inner_fn: Callable[[], None]) -> Program:
        if strict:
            _check_function_body_strict(
                inner_fn,
                opt_out_hint="@program(strict=False)",
                source_label=f"@program {getattr(inner_fn, '__qualname__', repr(inner_fn))}",
            )
        prog = Program(strict=strict)
        with prog:
            inner_fn()
        return prog

    if fn is None:
        return _decorate
    return _decorate(fn)


# ============================================================================
# Subroutine - named block of rungs
# ============================================================================


class Subroutine:
    """Context manager for defining a subroutine.

    Subroutines are named blocks of rungs that are only executed when called.

    Example:
        with subroutine("my_sub"):
            with Rung():
                out(Light)
    """

    def __init__(self, name: str, *, strict: bool = True) -> None:
        self._name = name
        self._strict = strict

    def __enter__(self) -> Subroutine:
        prog = Program.current()
        if prog is None:
            raise RuntimeError("subroutine() must be used inside a Program context")
        prog.start_subroutine(self._name)
        return self

    def __call__(self, fn: Callable[[], None]) -> SubroutineFunc:
        """Use subroutine() as a decorator.

        Example:
            @subroutine("init")
            def init_sequence():
                with Rung():
                    out(Light)
        """
        if self._strict:
            _check_function_body_strict(
                fn,
                opt_out_hint=f'@subroutine("{self._name}", strict=False)',
                source_label=f'@subroutine("{self._name}") {getattr(fn, "__qualname__", repr(fn))}',
            )
        return SubroutineFunc(self._name, fn)

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        prog = Program.current()
        if prog is not None:
            prog.end_subroutine()


class SubroutineFunc:
    """A decorated function that represents a subroutine.

    Created by using @subroutine("name") as a decorator. When passed to call(),
    auto-registers with the current Program on first use.

    Example:
        @subroutine("init")
        def init_sequence():
            with Rung():
                out(Light)

        with Program() as logic:
            with Rung(Button):
                call(init_sequence)
    """

    def __init__(self, name: str, fn: Callable[[], None]) -> None:
        self._name = name
        self._fn = fn

    @property
    def name(self) -> str:
        """The subroutine name."""
        return self._name

    def _register(self, prog: Program) -> None:
        """Register this subroutine's rungs with a Program."""
        prog.start_subroutine(self._name)
        self._fn()
        prog.end_subroutine()


def subroutine(name: str, *, strict: bool = True) -> Subroutine:
    """Define a named subroutine.

    Subroutines are only executed when called via call().
    They are NOT executed during normal program scan.

    Example:
        with Program() as logic:
            with Rung(Button):
                call("my_sub")

            with subroutine("my_sub"):
                with Rung():
                    out(Light)
    """
    return Subroutine(name, strict=strict)


# ============================================================================
# ForLoop - repeated instruction block within a rung
# ============================================================================


class ForLoop:
    """Context manager for a repeated instruction block within a rung."""

    def __init__(self, count: Tag | int, oneshot: bool = False) -> None:
        self.count = count
        self.oneshot = oneshot
        self.idx = Tag("_forloop_idx", TagType.DINT)
        self._parent_ctx: Rung | None = None
        self._capture_rung: RungLogic | None = None
        self._capture_ctx: Rung | None = None

    def __enter__(self) -> ForLoop:
        global _forloop_active

        if _forloop_active:
            raise RuntimeError("Nested forloop is not permitted")

        self._parent_ctx = _require_rung_context("forloop")
        _forloop_active = True

        # Capture body instructions to a temporary rung (like Branch capture).
        self._capture_rung = RungLogic()
        self._capture_ctx = Rung.__new__(Rung)
        self._capture_ctx._rung = self._capture_rung
        _rung_stack.append(self._capture_ctx)

        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        global _forloop_active

        _rung_stack.pop()
        _forloop_active = False

        if self._parent_ctx is None or self._capture_rung is None:
            return

        instruction = ForLoopInstruction(
            count=self.count,
            idx_tag=self.idx,
            instructions=self._capture_rung._instructions,
            coils=self._capture_rung._coils,
            oneshot=self.oneshot,
        )
        self._parent_ctx._rung.add_instruction(instruction)

        # Register child OUT targets on parent rung so rung-false resets still apply.
        for coil in self._capture_rung._coils:
            self._parent_ctx._rung.register_coil(coil)


def forloop(count: Tag | int, oneshot: bool = False) -> ForLoop:
    """Create a repeated instruction block context.

    Example:
        with Rung(Enable):
            with forloop(10) as loop:
                copy(Source[loop.idx + 1], Dest[loop.idx + 1])
    """
    return ForLoop(count, oneshot=oneshot)


# ============================================================================
# Branch - parallel path within a rung
# ============================================================================


class Branch:
    """Context manager for a parallel branch within a rung.

    A branch executes when both the parent rung conditions AND
    the branch's own conditions are true.

    Example:
        with Rung(Step == 0):
            out(Light1)
            with branch(AutoMode):  # Only executes if Step==0 AND AutoMode
                out(Light2)
                copy(1, Step, oneshot=True)
    """

    def __init__(self, *conditions: Condition | Tag) -> None:
        """Create a branch with additional conditions.

        Args:
            conditions: Conditions that must be true (in addition to parent rung)
                        for this branch's instructions to execute.
        """
        self._conditions = list(conditions)
        self._branch_rung: RungLogic | None = None
        self._parent_ctx: Rung | None = None
        self._branch_ctx: Rung | None = None

    def __enter__(self) -> Branch:
        if _forloop_active:
            raise RuntimeError("branch() is not permitted inside forloop()")

        # Get parent rung context
        self._parent_ctx = _current_rung()
        if self._parent_ctx is None:
            raise RuntimeError("branch() must be called inside a Rung context")

        # Create a nested rung for the branch that includes BOTH parent and branch conditions
        # This ensures terminal instructions (counters, timers) see the full condition chain
        parent_conditions = self._parent_ctx._rung._conditions
        combined_conditions = parent_conditions + self._conditions
        self._branch_rung = RungLogic(*combined_conditions)

        # Push a new "fake" rung context so instructions go to the branch
        self._branch_ctx = Rung.__new__(Rung)
        self._branch_ctx._rung = self._branch_rung
        _rung_stack.append(self._branch_ctx)

        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        # Pop our branch context
        _rung_stack.pop()

        # Add the branch as a nested rung to the parent
        if self._parent_ctx is not None and self._branch_rung is not None:
            self._parent_ctx._rung.add_branch(self._branch_rung)


def branch(*conditions: Condition | Tag) -> Branch:
    """Create a parallel branch within a rung.

    A branch executes when both the parent rung conditions AND
    the branch's own conditions are true.

    Example:
        with Rung(Step == 0):
            out(Light1)
            with branch(AutoMode):  # Only executes if Step==0 AND AutoMode
                out(Light2)
                copy(1, Step, oneshot=True)

    Args:
        conditions: Conditions that must be true (in addition to parent rung)
                    for this branch's instructions to execute.

    Returns:
        Branch context manager.
    """
    return Branch(*conditions)


# Backwards compatibility alias
RungContext = Rung


# ============================================================================
# Counter Instructions - chaining API
# ============================================================================


class CountUpBuilder:
    """Builder for count_up instruction with chaining API (Click-style).

    Supports optional .down() and required .reset() chaining:
        count_up(done, acc, setpoint=100).reset(reset_tag)
        count_up(done, acc, setpoint=50).down(down_cond).reset(reset_tag)
    """

    def __init__(self, done_bit: Tag, accumulator: Tag, setpoint: Tag | int, up_condition: Any):
        self._done_bit = done_bit
        self._accumulator = accumulator
        self._setpoint = setpoint
        self._up_condition = up_condition  # From rung conditions
        self._down_condition: Condition | Tag | None = None
        self._reset_condition: Condition | Tag | None = None
        self._rung = _require_rung_context("count_up")

    def down(self, condition: Condition | Tag) -> CountUpBuilder:
        """Add down trigger (optional).

        Creates a bidirectional counter that increments on rung true
        and decrements on down condition true.

        Args:
            condition: Condition for decrementing the counter.

        Returns:
            Self for chaining.
        """
        self._down_condition = condition
        return self

    def reset(self, condition: Condition | Tag) -> Tag:
        """Add reset condition (required).

        When reset condition is true, clears both done bit and accumulator.

        Args:
            condition: Condition for resetting the counter.

        Returns:
            The done bit tag.
        """
        self._reset_condition = condition
        # Now build and add the instruction
        instr = CountUpInstruction(
            self._done_bit,
            self._accumulator,
            self._setpoint,
            self._up_condition,
            self._reset_condition,
            self._down_condition,
        )
        self._rung._rung.add_instruction(instr)
        return self._done_bit


class CountDownBuilder:
    """Builder for count_down instruction with chaining API (Click-style).

    Supports required .reset() chaining:
        count_down(done, acc, setpoint=25).reset(reset_tag)
    """

    def __init__(self, done_bit: Tag, accumulator: Tag, setpoint: Tag | int, down_condition: Any):
        self._done_bit = done_bit
        self._accumulator = accumulator
        self._setpoint = setpoint
        self._down_condition = down_condition  # From rung conditions
        self._reset_condition: Condition | Tag | None = None
        self._rung = _require_rung_context("count_down")

    def reset(self, condition: Condition | Tag) -> Tag:
        """Add reset condition (required).

        When reset condition is true, loads setpoint into accumulator
        and clears done bit.

        Args:
            condition: Condition for resetting the counter.

        Returns:
            The done bit tag.
        """
        self._reset_condition = condition
        # Now build and add the instruction
        instr = CountDownInstruction(
            self._done_bit,
            self._accumulator,
            self._setpoint,
            self._down_condition,
            self._reset_condition,
        )
        self._rung._rung.add_instruction(instr)
        return self._done_bit


def count_up(
    done_bit: Tag,
    accumulator: Tag,
    setpoint: Tag | int,
) -> CountUpBuilder:
    """Count Up instruction (CTU) - Click-style.

    Creates a counter that increments on each rising edge of the rung condition.

    Example:
        with Rung(rise(PartSensor)):
            count_up(done_bit, acc, setpoint=100).reset(ResetBtn)

    This is a terminal instruction. Requires .reset() chaining.

    Args:
        done_bit: Tag to set when accumulator >= setpoint.
        accumulator: Tag to increment on each rising edge.
        setpoint: Target value (Tag or int).

    Returns:
        Builder for chaining .down() and .reset().
    """
    ctx = _require_rung_context("count_up")
    up_condition = ctx._rung._get_combined_condition()
    return CountUpBuilder(done_bit, accumulator, setpoint, up_condition)


def count_down(
    done_bit: Tag,
    accumulator: Tag,
    setpoint: Tag | int,
) -> CountDownBuilder:
    """Count Down instruction (CTD) - Click-style.

    Creates a counter that decrements on each rising edge of the rung condition.

    Example:
        with Rung(rise(Dispense)):
            count_down(done_bit, acc, setpoint=25).reset(Reload)

    This is a terminal instruction. Requires .reset() chaining.

    Args:
        done_bit: Tag to set when accumulator <= -setpoint.
        accumulator: Tag to decrement on each rising edge.
        setpoint: Target value (Tag or int).

    Returns:
        Builder for chaining .reset().
    """
    ctx = _require_rung_context("count_down")
    down_condition = ctx._rung._get_combined_condition()
    return CountDownBuilder(done_bit, accumulator, setpoint, down_condition)


# ============================================================================
# Timer Instructions - chaining API
# ============================================================================


class OnDelayBuilder:
    """Builder for on_delay instruction with optional .reset() chaining (Click-style).

    Without .reset(): TON behavior (auto-reset on rung false)
    With .reset(): RTON behavior (manual reset required)
    """

    def __init__(
        self,
        done_bit: Tag,
        accumulator: Tag,
        setpoint: Tag | int,
        enable_condition: Any,
        time_unit: TimeUnit,
    ):
        self._done_bit = done_bit
        self._accumulator = accumulator
        self._setpoint = setpoint
        self._enable_condition = enable_condition
        self._time_unit = time_unit
        self._reset_condition: Condition | Tag | None = None
        self._rung = _require_rung_context("on_delay")
        self._added = False

    def reset(self, condition: Condition | Tag) -> Tag:
        """Add reset condition (makes timer retentive - RTON).

        When reset condition is true, clears both done bit and accumulator.

        Args:
            condition: Condition for resetting the timer.

        Returns:
            The done bit tag.
        """
        self._reset_condition = condition
        self._finalize()
        return self._done_bit

    def _finalize(self) -> None:
        """Build and add the instruction to the rung."""
        if self._added:
            return
        self._added = True
        instr = OnDelayInstruction(
            self._done_bit,
            self._accumulator,
            self._setpoint,
            self._enable_condition,
            self._reset_condition,
            self._time_unit,
        )
        self._rung._rung.add_instruction(instr)

    def __del__(self) -> None:
        """Finalize on garbage collection if not explicitly called."""
        # This handles the case where .reset() is not called (TON behavior)
        self._finalize()


class OffDelayBuilder:
    """Builder for off_delay instruction (TOF behavior, Click-style).

    Auto-resets when re-enabled.
    """

    def __init__(
        self,
        done_bit: Tag,
        accumulator: Tag,
        setpoint: Tag | int,
        enable_condition: Any,
        time_unit: TimeUnit,
    ):
        self._done_bit = done_bit
        self._accumulator = accumulator
        self._setpoint = setpoint
        self._enable_condition = enable_condition
        self._time_unit = time_unit
        self._rung = _require_rung_context("off_delay")
        self._added = False

    def _finalize(self) -> None:
        """Build and add the instruction to the rung."""
        if self._added:
            return
        self._added = True
        instr = OffDelayInstruction(
            self._done_bit,
            self._accumulator,
            self._setpoint,
            self._enable_condition,
            self._time_unit,
        )
        self._rung._rung.add_instruction(instr)

    def __del__(self) -> None:
        """Finalize on garbage collection if not explicitly called."""
        self._finalize()


def on_delay(
    done_bit: Tag,
    accumulator: Tag,
    setpoint: Tag | int,
    time_unit: TimeUnit = TimeUnit.Tms,
) -> OnDelayBuilder:
    """On-Delay Timer instruction (TON/RTON) - Click-style.

    Accumulates time while rung is true.

    Example:
        with Rung(MotorRunning):
            on_delay(done_bit, acc, setpoint=5000)                 # TON
            on_delay(done_bit, acc, setpoint=5000).reset(ResetBtn) # RTON

    This is a terminal instruction (must be last in rung).
    Optional .reset() chaining for retentive behavior.

    Args:
        done_bit: Tag to set when accumulator >= setpoint.
        accumulator: Tag to increment while enabled.
        setpoint: Target value in time units (Tag or int).
        time_unit: Time unit for accumulator (default: Tms).

    Returns:
        Builder for optional .reset() chaining.
    """
    ctx = _require_rung_context("on_delay")
    enable_condition = ctx._rung._get_combined_condition()
    return OnDelayBuilder(done_bit, accumulator, setpoint, enable_condition, time_unit)


def off_delay(
    done_bit: Tag,
    accumulator: Tag,
    setpoint: Tag | int,
    time_unit: TimeUnit = TimeUnit.Tms,
) -> OffDelayBuilder:
    """Off-Delay Timer instruction (TOF) - Click-style.

    Done bit is True while enabled. After disable, counts until setpoint,
    then done bit goes False. Auto-resets when re-enabled.

    Example:
        with Rung(MotorCommand):
            off_delay(done_bit, acc, setpoint=10000)

    This is a terminal instruction (must be last in rung).

    Args:
        done_bit: Tag that stays True for setpoint time after rung goes false.
        accumulator: Tag to increment while disabled.
        setpoint: Delay time in time units (Tag or int).
        time_unit: Time unit for accumulator (default: Tms).

    Returns:
        Builder for the off_delay instruction.
    """
    ctx = _require_rung_context("off_delay")
    enable_condition = ctx._rung._get_combined_condition()
    return OffDelayBuilder(done_bit, accumulator, setpoint, enable_condition, time_unit)
