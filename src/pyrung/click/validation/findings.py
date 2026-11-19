"""Click validation findings and suggestion helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pyclickplc.banks import BANKS

from pyrung.core.validation.display import FindingDisplay, Frame, _FindingTextMixin

if TYPE_CHECKING:
    from collections.abc import Collection

    from pyrung.click.tag_map import TagMap
    from pyrung.core.validation.walker import OperandFact

ValidationMode = Literal["warn", "strict"]
FindingSeverity = Literal["error", "warning", "hint"]

# ---------------------------------------------------------------------------
# Finding codes
# ---------------------------------------------------------------------------

CLK_PTR_CONTEXT_ONLY_COPY = "CLK_PTR_CONTEXT_ONLY_COPY"
CLK_PTR_POINTER_MUST_BE_DS = "CLK_PTR_POINTER_MUST_BE_DS"
CLK_PTR_EXPR_NOT_ALLOWED = "CLK_PTR_EXPR_NOT_ALLOWED"
CLK_PTR_BLOCK_OFFSET = "CLK_PTR_BLOCK_OFFSET"
CLK_EXPR_ONLY_IN_MATH = "CLK_EXPR_ONLY_IN_MATH"
CLK_TILDE_BOOL_CONTACT_ONLY = "CLK_TILDE_BOOL_CONTACT_ONLY"
CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED = "CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED"
CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED = "CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED"
CLK_PTR_DS_UNVERIFIED = "CLK_PTR_DS_UNVERIFIED"
CLK_FUNCTION_CALL_NOT_PORTABLE = "CLK_FUNCTION_CALL_NOT_PORTABLE"
CLK_MATH_MODE_MIXED = "CLK_MATH_MODE_MIXED"
CLK_MATH_FLOOR_DIV = "CLK_MATH_FLOOR_DIV"
CLK_MATH_FUNC_MODE_MISMATCH = "CLK_MATH_FUNC_MODE_MISMATCH"
CLK_MATH_NESTING_DEPTH = "CLK_MATH_NESTING_DEPTH"
CLK_MATH_RANGE_MIXED_BANKS = "CLK_MATH_RANGE_MIXED_BANKS"
CLK_MATH_RANGE_NOT_CONTIGUOUS = "CLK_MATH_RANGE_NOT_CONTIGUOUS"
CLK_MATH_MEDIAN_RANGE_TOO_LARGE = "CLK_MATH_MEDIAN_RANGE_TOO_LARGE"
CLK_MATH_BIT_RANGE_AGGREGATE = "CLK_MATH_BIT_RANGE_AGGREGATE"
CLK_MATH_SUM_BANK_NOT_AVAILABLE = "CLK_MATH_SUM_BANK_NOT_AVAILABLE"

CLK_PROFILE_UNAVAILABLE = "CLK_PROFILE_UNAVAILABLE"
CLK_ADDRESS_IDENTITY_CONFLICT = "CLK_ADDRESS_IDENTITY_CONFLICT"
CLK_NICKNAME_TOO_LONG = "CLK_NICKNAME_TOO_LONG"
CLK_BANK_UNRESOLVED = "CLK_BANK_UNRESOLVED"
CLK_BANK_NOT_WRITABLE = "CLK_BANK_NOT_WRITABLE"
CLK_BANK_WRONG_ROLE = "CLK_BANK_WRONG_ROLE"
CLK_DRUM_JUMP_STEP_TAG_REQUIRED = "CLK_DRUM_JUMP_STEP_TAG_REQUIRED"
CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED = "CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED"
CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED = "CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED"
CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y = "CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y"
CLK_IMMEDIATE_CONTACT_MUST_BE_X = "CLK_IMMEDIATE_CONTACT_MUST_BE_X"
CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS = "CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS"
CLK_TIMER_PRESET_OVERFLOW = "CLK_TIMER_PRESET_OVERFLOW"
CLK_OUT_UNIT_UNSUPPORTED = "CLK_OUT_UNIT_UNSUPPORTED"
CLK_TIMER_PAIR_MISMATCH = "CLK_TIMER_PAIR_MISMATCH"
CLK_STATUS_BIT_NOT_PORTABLE = "CLK_STATUS_BIT_NOT_PORTABLE"
CLK_FORLOOP_NESTING_DEPTH = "CLK_FORLOOP_NESTING_DEPTH"
CLK_FORLOOP_INNER_ONESHOT = "CLK_FORLOOP_INNER_ONESHOT"
CLK_FORLOOP_RETURN_INSIDE = "CLK_FORLOOP_RETURN_INSIDE"
CLK_NESTED_CALL = "CLK_NESTED_CALL"
CLK_STEP_LIMIT = "CLK_STEP_LIMIT"
CLK_FORLOOP_COUNT_BANK = "CLK_FORLOOP_COUNT_BANK"
CLK_RAND_BOUND_TYPE = "CLK_RAND_BOUND_TYPE"
CLK_RAND_EQUAL_BOUNDS = "CLK_RAND_EQUAL_BOUNDS"
CLK_MATH_ARG_NOT_PLAIN = "CLK_MATH_ARG_NOT_PLAIN"
CLK_MATH_WIDE_CONSTANT = "CLK_MATH_WIDE_CONSTANT"
CLK_TEXT_ORDER_COMPARE = "CLK_TEXT_ORDER_COMPARE"
CLK_TEXT_PAST_BANK = "CLK_TEXT_PAST_BANK"
CLK_TEXT_PAST_BLOCK = "CLK_TEXT_PAST_BLOCK"
CLK_TEXT_WINDOW_NOT_CONTIGUOUS = "CLK_TEXT_WINDOW_NOT_CONTIGUOUS"
CLK_COPY_WIDE_CONSTANT = "CLK_COPY_WIDE_CONSTANT"
CLK_COPY_RANGE_NOT_CONSECUTIVE = "CLK_COPY_RANGE_NOT_CONSECUTIVE"
CLK_MATH_BIT_OPERAND = "CLK_MATH_BIT_OPERAND"
CLK_MATH_DEST_BANK = "CLK_MATH_DEST_BANK"
CLK_SEARCH_RANGE_BANK = "CLK_SEARCH_RANGE_BANK"
CLK_SEARCH_RESULT_NOT_DS = "CLK_SEARCH_RESULT_NOT_DS"
CLK_TIMER_PRESET_TOO_LOW = "CLK_TIMER_PRESET_TOO_LOW"
CLK_PTR_PAST_BANK = "CLK_PTR_PAST_BANK"
CLK_COMPARE_HEX_MIX = "CLK_COMPARE_HEX_MIX"
CLK_NOP_NO_COIL = "CLK_NOP_NO_COIL"


@dataclass(frozen=True, init=False)
class ClickFinding(_FindingTextMixin):
    """A CLICK portability finding. ``str()`` renders its ``display``.

    If the caller passes no ``display``, the constructor builds one with one frame at
    ``location``, ``message`` as the problem and ``suggestion`` as the hint. A display
    that the caller passes must have the finding's code and severity. The constructor
    does not compare its text with ``message`` or ``suggestion``.
    """

    code: str
    severity: FindingSeverity
    message: str
    location: str
    suggestion: str | None
    display: FindingDisplay

    def __init__(
        self,
        code: str,
        severity: FindingSeverity,
        message: str,
        location: str,
        suggestion: str | None = None,
        display: FindingDisplay | None = None,
    ) -> None:
        if display is None:
            display = FindingDisplay(
                code=code,
                severity=severity,
                frames=(Frame(location=location),),
                problem=message,
                hint=suggestion or "",
            )
        elif display.code != code or display.severity != severity:
            # dataclasses.replace() passes the old display on, so a changed code or
            # severity would print the old one.
            raise ValueError(
                f"The display of finding {code} ({severity}) is for {display.code} "
                f"({display.severity}). Pass a display with the same code and severity."
            )
        # A frozen dataclass refuses plain attribute assignment.
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "severity", severity)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "location", location)
        object.__setattr__(self, "suggestion", suggestion)
        object.__setattr__(self, "display", display)


@dataclass(frozen=True)
class ClickValidationReport:
    errors: tuple[ClickFinding, ...] = field(default_factory=tuple)
    warnings: tuple[ClickFinding, ...] = field(default_factory=tuple)
    hints: tuple[ClickFinding, ...] = field(default_factory=tuple)

    def summary(self) -> str:
        parts: list[str] = []
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        if self.warnings:
            parts.append(f"{len(self.warnings)} warning(s)")
        if self.hints:
            parts.append(f"{len(self.hints)} hint(s)")
        if not parts:
            return "No findings."
        return ", ".join(parts) + "."


def _route_severity(code: str, mode: ValidationMode) -> FindingSeverity:
    if mode == "strict":
        return "error"
    # The export refuses a Copy whose text runs past TXT1000 in every mode, so warn mode
    # reports it as a warning, not a hint.
    if code in (CLK_PROFILE_UNAVAILABLE, CLK_NOP_NO_COIL, "text-past-bank"):
        return "warning"
    return "hint"


_DEST_PATHS = {"instruction.dest", "instruction.target"}


def _bank_pointer_advice(
    block_name: str, index_dsl: str, tag_map: TagMap, *, dest: bool = False
) -> str | None:
    """Return advice on reaching ``block_name[index_dsl]`` through a bare CLICK bank pointer.

    CLICK accepts only ``BANK[pointer]``, so the block's offset goes into the pointer.
    Return None when the block is not mapped onto one bank.
    """
    entry = tag_map._block_entry_by_name(block_name)
    if entry is None:
        # A pre-built CLICK block such as ds or dd is its own bank, with no offset.
        if block_name not in BANKS:
            return None
        offset, bank = 0, block_name
    else:
        try:
            offset = tag_map._offset_for(entry.logical)
            bank = tag_map.resolve(entry.logical, entry.logical_addresses[0]).rstrip("0123456789")
        except (KeyError, ValueError, IndexError):
            return None
    if not bank:
        return None
    total = index_dsl if offset == 0 else f"{index_dsl} {'+' if offset > 0 else '-'} {abs(offset)}"
    copy_text = f"copy(src, {bank.lower()}[Ptr])" if dest else f"copy({bank.lower()}[Ptr], dest)"
    return (
        f"Compute the {bank} address into a DS tag and index the bank directly: "
        f"math({total}, Ptr); {copy_text}"
    )


# The export refusals and the portability findings share these texts, so one fault reads
# the same in both. Each takes *at*, the location phrase that portability adds.


def immediate_edge_text(at: str = " here") -> str:
    """Return the message for an immediate() rise() or fall() contact."""
    return f"The contact{at} is an immediate rise() or fall(). CLICK has no immediate edge contact."


def immediate_misplaced_text(at: str = " here") -> str:
    """Return the message for immediate() on an operand that is no contact or coil."""
    return f"immediate() is not allowed{at}. CLICK accepts immediate() only on a contact or a coil."


def immediate_coil_operand_text(got: str, at: str = "") -> str:
    """Return the message for immediate() around a coil operand that is no tag or range."""
    return f"immediate() on a coil{at} needs a tag or a block range, got {got}."


def immediate_coil_not_y_text(at: str = "", found: str | None = None) -> str:
    """Return the message for an immediate coil that is not mapped to Y."""
    if found is None:
        return f"immediate() on a coil{at} needs a Y output."
    return f"immediate() on a coil{at} needs a Y output, but it is mapped to {found}."


RangeFault = Literal["empty", "descending", "scattered"]

# One Copy family range that CLICK cannot hold as a range: its name, its fault, the
# addresses that the export would write, and whether its tags are those CLICK addresses.
RangeProblem = tuple[str, RangeFault, list[str], bool]

_RANGE_RULES: dict[str, str] = {
    "descending": "CLICK takes a range only in ascending order.",
    "scattered": "CLICK takes a range only as consecutive addresses in one bank.",
}

_RANGE_ADVICE: dict[str, str] = {
    "empty": "Select a range that holds CLICK addresses.",
    "descending": "Remove .reverse(), or reverse the data in another way.",
    "scattered": (
        "Map the tags to consecutive addresses in one bank, or write one call for each run "
        "of consecutive addresses."
    ),
}


def _range_fact(name: str, fault: RangeFault, addresses: list[str], own: bool) -> str:
    if fault == "empty":
        return f"{name} holds no CLICK address."
    if fault == "descending":
        return f"{name} runs from {addresses[0]} down to {addresses[-1]}."
    verb = "holds" if own else "maps onto"
    return f"{name} {verb} [{','.join(addresses)}]."


def range_not_consecutive_text(problems: list[RangeProblem]) -> str:
    """Return what is wrong with each range, then each CLICK rule that it breaks, once."""
    facts = [_range_fact(*problem) for problem in problems]
    rules = dict.fromkeys(
        _RANGE_RULES[fault] for _name, fault, _addresses, _own in problems if fault in _RANGE_RULES
    )
    return " ".join([*facts, *rules])


def range_not_consecutive_advice(problems: list[RangeProblem]) -> str:
    """Return the advice for each fault of the ranges, once."""
    return " ".join(dict.fromkeys(_RANGE_ADVICE[fault] for _name, fault, _a, _o in problems))


def immediate_coil_range_gap_text(at: str = "") -> str:
    """Return the message for an immediate coil range whose Y addresses have a gap."""
    return f"immediate() on a coil range{at} needs consecutive Y addresses."


def pointer_expression_text(at: str = " here") -> str:
    """Return the message for a pointer that is an expression."""
    return f"The pointer{at} is an expression. CLICK cannot compute a pointer."


def pointer_offset_text(block: str, pointer: str, offset: int, at: str = "") -> str:
    """Return the message for a pointer into a block that starts away from its bank's start."""
    return (
        f"Pointer {block}[{pointer}]{at} needs the block's offset of {offset} added. "
        "A CLICK pointer takes no offset."
    )


def pointer_block_range_text(at: str = " here") -> str:
    """Return the message for a block range whose ends use a pointer."""
    return f"The block range{at} uses a pointer. CLICK accepts only a block range with fixed ends."


def _build_suggestion(code: str, fact: OperandFact | None, tag_map: TagMap) -> str:
    """Build a context-aware suggestion string for a finding code."""
    meta = {} if fact is None else fact.metadata
    is_dest = fact is not None and fact.location.arg_path in _DEST_PATHS

    if code == CLK_PTR_CONTEXT_ONLY_COPY:
        block_name = str(meta.get("block_name", ""))
        pointer_name = str(meta.get("pointer_name", ""))
        if block_name and pointer_name:
            return (
                f"Pointer {block_name}[{pointer_name}] can only be used inside copy(). "
                "Use a direct tag reference here instead."
            )
        return "Use direct tag addressing in this context; keep pointer usage in copy() only."

    if code == CLK_PTR_POINTER_MUST_BE_DS:
        from .resolve import _resolve_pointer_memory_type

        pointer_name = str(meta.get("pointer_name", ""))
        resolved_type = (
            _resolve_pointer_memory_type(pointer_name, tag_map) if pointer_name else None
        )
        if pointer_name and resolved_type:
            return (
                f"Pointer '{pointer_name}' is mapped to {resolved_type} memory. "
                "Remap it to a DS address so Click hardware can use it as a pointer."
            )
        return "Use a DS tag as the pointer source for copy() addressing."

    if code == CLK_PTR_DS_UNVERIFIED:
        pointer_name = str(meta.get("pointer_name", ""))
        if pointer_name:
            return (
                f"Pointer '{pointer_name}' is not in the tag map - cannot verify it is DS. "
                f"Map it to a DS address: {pointer_name}.map_to(ds[N])"
            )
        return "Use a DS tag as the pointer source for copy() addressing."

    if code == CLK_PTR_EXPR_NOT_ALLOWED:
        block_name = str(meta.get("block_name", ""))
        expr_dsl = str(meta.get("expr_dsl", ""))
        if block_name and expr_dsl:
            advice = _bank_pointer_advice(block_name, expr_dsl, tag_map, dest=is_dest)
            if advice is not None:
                return f"Click cannot compute {block_name}[{expr_dsl}] at runtime. {advice}"
            return (
                f"Click cannot compute {block_name}[{expr_dsl}] at runtime. "
                "Pre-compute the index with math() into a DS pointer tag, "
                "then use block[Ptr]."
            )
        return "Compute the index with math() into a DS pointer tag, then use block[Ptr]."

    if code == CLK_PTR_BLOCK_OFFSET:
        block_name = str(meta.get("block_name", ""))
        pointer_name = str(meta.get("pointer_name", ""))
        advice = (
            _bank_pointer_advice(block_name, pointer_name, tag_map, dest=is_dest)
            if pointer_name
            else None
        )
        return advice or "Index the CLICK bank directly with a DS pointer tag."

    if code == CLK_EXPR_ONLY_IN_MATH:
        expr_dsl = str(meta.get("expr_dsl", ""))
        if expr_dsl:
            return (
                f"Expression '{expr_dsl}' cannot be used directly here. "
                f"Move it into math({expr_dsl}, temp) and use temp in this context."
            )
        return "Move expression into math(expr, temp) and use temp in this context."

    if code == CLK_TILDE_BOOL_CONTACT_ONLY:
        expr_dsl = str(meta.get("expr_dsl", ""))
        if expr_dsl:
            return (
                f"Expression '{expr_dsl}' uses `~`. Click portability reserves `~` for BOOL "
                "contact inversion in rung conditions. Rewrite with explicit math or masking."
            )
        return (
            "Click portability reserves `~` for BOOL contact inversion in rung conditions. "
            "Rewrite bitwise inversion with explicit math or masking."
        )

    if code == CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED:
        return (
            "Click contacts are BOOL-only; numeric truthiness is not portable. "
            "Rewrite as rung(tag != 0) (or rung(tag == 0) for inverted intent)."
        )

    if code == CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED:
        block_name = str(meta.get("block_name", ""))
        if block_name:
            return (
                f"Block '{block_name}' has a pointer at one end of its range. Use a range "
                "with fixed ends, such as ds.select(1, 10)."
            )
        return "Use a block range with fixed ends, such as ds.select(1, 10)."

    if code == CLK_FUNCTION_CALL_NOT_PORTABLE:
        return (
            "Replace run_function/run_enabled_function with Click-portable instructions "
            "(copy/math/timer/counter/send/receive)."
        )

    if code == CLK_MATH_MODE_MIXED:
        return (
            "Split mixed math into separate decimal and WORD-only math() steps, "
            "or convert through an intermediate tag so each math() stays one family."
        )

    if code == CLK_MATH_FLOOR_DIV:
        return (
            "Click has no floor-division operator. Use math(a / b, int_dest) instead; "
            "copying the result to an Int or Dint tag truncates toward zero automatically."
        )

    if code == CLK_MATH_FUNC_MODE_MISMATCH:
        return ""  # suggestion is built inline with context

    if code == CLK_MATH_NESTING_DEPTH:
        return (
            "Click limits parenthetical nesting to 8 levels. "
            "Break the expression into intermediate math() steps with temporary tags."
        )

    if code == CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED:
        return (
            "Use immediate() only on a contact, or on the target of out(), latch(), reset() "
            "or toggle()."
        )

    if code == CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED:
        return "Use rise(tag)/fall(tag) without immediate(...), or use a plain immediate contact."

    if code == CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y:
        return "Map the coil to a Y address."

    if code == CLK_IMMEDIATE_CONTACT_MUST_BE_X:
        return "Map it to an X input, or use a plain contact."

    if code == CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS:
        return "Map the range to consecutive Y addresses, such as Y001..Y004."

    if code == CLK_TIMER_PRESET_OVERFLOW:
        return "Use a larger time unit to keep the preset within INT range (max 32,767)."

    if code == CLK_STATUS_BIT_NOT_PORTABLE:
        return (
            "Use the corresponding Done/Acc fields instead, or remove this reference "
            "from Click-targeted logic. Status bits (EN, TT, CU, CD) are available in "
            "the pyrung runner API (.when(), .monitor()) but cannot appear in Click ladder."
        )

    return ""


# The CLK_ codes that laddercodec's codes replaced, and the codes that took their place.
_REPLACED_CODES: dict[str, str] = {
    "CLK_COPY_BANK_INCOMPATIBLE": "copy-bank-incompatible or bank-not-allowed",
    "CLK_COPY_CONVERTER_INCOMPATIBLE": (
        "copy-converter-bank, copy-txt-needs-convert or option-not-offered"
    ),
    "CLK_COPY_CONSTANT_INTO_BIT": "constant-into-bit or bank-not-allowed",
    "CLK_PACK_TEXT_BANK_INCOMPATIBLE": "pack-kind-mismatch or copy-bank-incompatible",
}


def check_allowed_codes(allow: Collection[str]) -> None:
    """Raise ``ValueError`` for a name in *allow* that no finding can have.

    Otherwise a misspelt or removed code lets nothing through, and the caller is not told.
    """
    from laddercodec import Code

    if isinstance(allow, str):
        # A string is a collection of its letters, so no letter would match a code.
        raise ValueError(f"allow= is the string {allow!r}. Pass allow= a list of codes.")
    known = {value for name, value in globals().items() if name.startswith("CLK_")}
    known |= {code.value for code in Code}
    for name in allow:
        if name in known:
            continue
        if name in _REPLACED_CODES:
            raise ValueError(
                f"pyrung no longer reports {name}. Pass {_REPLACED_CODES[name]} to allow= instead."
            )
        raise ValueError(
            f"allow= names {name!r}. It is neither a pyrung code nor a laddercodec code. "
            "Pass the code that the finding names."
        )


__all__ = [
    "ValidationMode",
    "FindingSeverity",
    "CLK_PTR_CONTEXT_ONLY_COPY",
    "CLK_PTR_POINTER_MUST_BE_DS",
    "CLK_PTR_EXPR_NOT_ALLOWED",
    "CLK_PTR_BLOCK_OFFSET",
    "CLK_EXPR_ONLY_IN_MATH",
    "CLK_TILDE_BOOL_CONTACT_ONLY",
    "CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED",
    "CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED",
    "CLK_PTR_DS_UNVERIFIED",
    "CLK_FUNCTION_CALL_NOT_PORTABLE",
    "CLK_MATH_MODE_MIXED",
    "CLK_MATH_FLOOR_DIV",
    "CLK_MATH_FUNC_MODE_MISMATCH",
    "CLK_MATH_NESTING_DEPTH",
    "CLK_MATH_RANGE_MIXED_BANKS",
    "CLK_MATH_RANGE_NOT_CONTIGUOUS",
    "CLK_MATH_MEDIAN_RANGE_TOO_LARGE",
    "CLK_MATH_BIT_RANGE_AGGREGATE",
    "CLK_MATH_SUM_BANK_NOT_AVAILABLE",
    "CLK_PROFILE_UNAVAILABLE",
    "CLK_NICKNAME_TOO_LONG",
    "CLK_BANK_UNRESOLVED",
    "CLK_BANK_NOT_WRITABLE",
    "CLK_BANK_WRONG_ROLE",
    "CLK_DRUM_JUMP_STEP_TAG_REQUIRED",
    "CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED",
    "CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED",
    "CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y",
    "CLK_IMMEDIATE_CONTACT_MUST_BE_X",
    "CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS",
    "CLK_COPY_RANGE_NOT_CONSECUTIVE",
    "CLK_TIMER_PRESET_OVERFLOW",
    "CLK_OUT_UNIT_UNSUPPORTED",
    "CLK_TIMER_PAIR_MISMATCH",
    "CLK_STATUS_BIT_NOT_PORTABLE",
    "CLK_FORLOOP_NESTING_DEPTH",
    "CLK_STEP_LIMIT",
    "CLK_FORLOOP_INNER_ONESHOT",
    "CLK_FORLOOP_RETURN_INSIDE",
    "CLK_NESTED_CALL",
    "CLK_FORLOOP_COUNT_BANK",
    "CLK_RAND_BOUND_TYPE",
    "CLK_RAND_EQUAL_BOUNDS",
    "CLK_MATH_ARG_NOT_PLAIN",
    "CLK_MATH_WIDE_CONSTANT",
    "CLK_TEXT_ORDER_COMPARE",
    "CLK_TEXT_PAST_BANK",
    "CLK_TEXT_PAST_BLOCK",
    "CLK_TEXT_WINDOW_NOT_CONTIGUOUS",
    "CLK_COPY_WIDE_CONSTANT",
    "CLK_MATH_BIT_OPERAND",
    "CLK_MATH_DEST_BANK",
    "CLK_SEARCH_RANGE_BANK",
    "CLK_SEARCH_RESULT_NOT_DS",
    "CLK_TIMER_PRESET_TOO_LOW",
    "CLK_PTR_PAST_BANK",
    "CLK_COMPARE_HEX_MIX",
    "CLK_NOP_NO_COIL",
    "ClickFinding",
    "ClickValidationReport",
]
