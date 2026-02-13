"""Click portability validation for pyrung programs.

Consumes Stage 1 walker facts for R1-R5, and instruction context plus hardware
profile data for Stage 3 (R6-R8).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pyclickplc.addresses import parse_address

from pyrung.core.memory_block import BlockRange, IndirectBlockRange, IndirectExprRef, IndirectRef
from pyrung.core.tag import Tag
from pyrung.core.validation.walker import ProgramLocation, walk_program

if TYPE_CHECKING:
    from pyrung.click.profile import HardwareProfile
    from pyrung.click.tag_map import TagMap
    from pyrung.core.program import Program
    from pyrung.core.validation.walker import OperandFact

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

ValidationMode = Literal["warn", "strict"]
FindingSeverity = Literal["error", "warning", "hint"]

# ---------------------------------------------------------------------------
# Finding codes
# ---------------------------------------------------------------------------

CLK_PTR_CONTEXT_ONLY_COPY = "CLK_PTR_CONTEXT_ONLY_COPY"
CLK_PTR_POINTER_MUST_BE_DS = "CLK_PTR_POINTER_MUST_BE_DS"
CLK_PTR_EXPR_NOT_ALLOWED = "CLK_PTR_EXPR_NOT_ALLOWED"
CLK_EXPR_ONLY_IN_MATH = "CLK_EXPR_ONLY_IN_MATH"
CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED = "CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED"
CLK_PTR_DS_UNVERIFIED = "CLK_PTR_DS_UNVERIFIED"

CLK_PROFILE_UNAVAILABLE = "CLK_PROFILE_UNAVAILABLE"
CLK_BANK_UNRESOLVED = "CLK_BANK_UNRESOLVED"
CLK_BANK_NOT_WRITABLE = "CLK_BANK_NOT_WRITABLE"
CLK_BANK_WRONG_ROLE = "CLK_BANK_WRONG_ROLE"
CLK_COPY_BANK_INCOMPATIBLE = "CLK_COPY_BANK_INCOMPATIBLE"


@dataclass(frozen=True)
class ClickFinding:
    code: str
    severity: FindingSeverity
    message: str
    location: str
    suggestion: str | None = None


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


# ---------------------------------------------------------------------------
# Stage 3 rule maps (table-driven)
# ---------------------------------------------------------------------------

_R6_WRITE_TARGETS: frozenset[tuple[str, str]] = frozenset(
    {
        ("OutInstruction", "target"),
        ("LatchInstruction", "target"),
        ("ResetInstruction", "target"),
        ("CopyInstruction", "target"),
        ("BlockCopyInstruction", "dest"),
        ("FillInstruction", "dest"),
        ("MathInstruction", "dest"),
        ("SearchInstruction", "result"),
        ("SearchInstruction", "found"),
        ("ShiftInstruction", "bit_range"),
        ("PackBitsInstruction", "dest"),
        ("PackWordsInstruction", "dest"),
        ("UnpackToBitsInstruction", "bit_block"),
        ("UnpackToWordsInstruction", "word_block"),
    }
)

_R7_ROLE_FIELDS: dict[tuple[str, str], str] = {
    ("OnDelayInstruction", "done_bit"): "timer_done_bit",
    ("OnDelayInstruction", "accumulator"): "timer_accumulator",
    ("OnDelayInstruction", "setpoint"): "timer_setpoint",
    ("OffDelayInstruction", "done_bit"): "timer_done_bit",
    ("OffDelayInstruction", "accumulator"): "timer_accumulator",
    ("OffDelayInstruction", "setpoint"): "timer_setpoint",
    ("CountUpInstruction", "done_bit"): "counter_done_bit",
    ("CountUpInstruction", "accumulator"): "counter_accumulator",
    ("CountUpInstruction", "setpoint"): "counter_setpoint",
    ("CountDownInstruction", "done_bit"): "counter_done_bit",
    ("CountDownInstruction", "accumulator"): "counter_accumulator",
    ("CountDownInstruction", "setpoint"): "counter_setpoint",
}

_R8_COPY_FIELDS: dict[str, tuple[str, str, str]] = {
    "CopyInstruction": ("single", "source", "target"),
    "BlockCopyInstruction": ("block", "source", "dest"),
    "FillInstruction": ("fill", "value", "dest"),
    "PackBitsInstruction": ("pack_bits", "bit_block", "dest"),
    "PackWordsInstruction": ("pack_words", "word_block", "dest"),
    "UnpackToBitsInstruction": ("unpack_bits", "source", "bit_block"),
    "UnpackToWordsInstruction": ("unpack_words", "source", "word_block"),
}

_KNOWN_BANKS: frozenset[str] = frozenset(
    {
        "X",
        "Y",
        "C",
        "T",
        "CT",
        "SC",
        "DS",
        "DD",
        "DH",
        "DF",
        "XD",
        "YD",
        "TD",
        "CTD",
        "SD",
        "TXT",
    }
)


@dataclass(frozen=True)
class _ResolvedSlot:
    memory_type: str
    address: int | None


@dataclass(frozen=True)
class _OperandResolution:
    slots: tuple[_ResolvedSlot, ...] = ()
    unresolved: bool = False


# ---------------------------------------------------------------------------
# Location formatting
# ---------------------------------------------------------------------------


def _format_location(loc: ProgramLocation) -> str:
    """Convert a ProgramLocation into a deterministic human-readable string."""
    if loc.scope == "subroutine":
        prefix = f"subroutine[{loc.subroutine}].rung[{loc.rung_index}]"
    else:
        prefix = f"main.rung[{loc.rung_index}]"

    for branch_idx in loc.branch_path:
        prefix += f".branch[{branch_idx}]"

    if loc.instruction_index is not None:
        prefix += f".instruction[{loc.instruction_index}]({loc.instruction_type})"

    return f"{prefix}.{loc.arg_path}"


# ---------------------------------------------------------------------------
# Severity routing
# ---------------------------------------------------------------------------


def _route_severity(code: str, mode: ValidationMode) -> FindingSeverity:
    if mode == "strict":
        return "error"
    if code == CLK_PROFILE_UNAVAILABLE:
        return "warning"
    return "hint"


# ---------------------------------------------------------------------------
# Pointer memory-type resolution (Stage 2)
# ---------------------------------------------------------------------------


def _resolve_pointer_memory_type(pointer_name: str, tag_map: TagMap) -> str | None:
    """Resolve a pointer tag name to its memory_type via mapped_slots()."""
    found_types: set[str] = set()
    for slot in tag_map.mapped_slots():
        if slot.logical_name == pointer_name:
            found_types.add(slot.memory_type)

    if len(found_types) == 1:
        return next(iter(found_types))
    return None


# ---------------------------------------------------------------------------
# Suggestion text
# ---------------------------------------------------------------------------


def _build_suggestion(code: str, fact: OperandFact, tag_map: TagMap) -> str:
    """Build a context-aware suggestion string for a finding code."""
    meta = fact.metadata

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
            block_entry = tag_map.block_entry_by_name(block_name)
            if block_entry is not None:
                try:
                    offset = tag_map.offset_for(block_entry.logical)
                    return (
                        f"Click cannot compute {block_name}[{expr_dsl}] at runtime. "
                        f"Store the index in a DS tag and use copy(): "
                        f"math({expr_dsl} + {offset}, Ptr); copy({block_name}[Ptr], dest)"
                    )
                except (KeyError, ValueError):
                    pass
            return (
                f"Click cannot compute {block_name}[{expr_dsl}] at runtime. "
                "Pre-compute the index with math() into a DS pointer tag, "
                "then use block[Ptr]."
            )
        return "Replace computed pointer arithmetic with DS pointer tag updated separately."

    if code == CLK_EXPR_ONLY_IN_MATH:
        expr_dsl = str(meta.get("expr_dsl", ""))
        if expr_dsl:
            return (
                f"Expression '{expr_dsl}' cannot be used directly here. "
                f"Move it into math({expr_dsl}, temp) and use temp in this context."
            )
        return "Move expression into math(expr, temp) and use temp in this context."

    if code == CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED:
        block_name = str(meta.get("block_name", ""))
        if block_name:
            return (
                f"Block '{block_name}' uses computed range bounds. "
                "Use a fixed BlockRange with literal start/end addresses instead."
            )
        return "Use a fixed BlockRange with literal start/end addresses for block copy."

    return ""


# ---------------------------------------------------------------------------
# Stage 2 rule evaluation (R1-R5)
# ---------------------------------------------------------------------------


def _evaluate_fact(
    fact: OperandFact,
    tag_map: TagMap,
    mode: ValidationMode,
) -> list[ClickFinding]:
    """Apply Stage 2 rules to a single OperandFact."""
    findings: list[ClickFinding] = []
    loc = fact.location
    location_str = _format_location(loc)

    if fact.value_kind == "indirect_ref":
        allowed = loc.instruction_type == "CopyInstruction" and loc.arg_path in {
            "instruction.source",
            "instruction.target",
        }
        if not allowed:
            findings.append(
                ClickFinding(
                    code=CLK_PTR_CONTEXT_ONLY_COPY,
                    severity=_route_severity(CLK_PTR_CONTEXT_ONLY_COPY, mode),
                    message=f"Pointer (IndirectRef) used outside copy instruction at {location_str}.",
                    location=location_str,
                    suggestion=_build_suggestion(CLK_PTR_CONTEXT_ONLY_COPY, fact, tag_map),
                )
            )
        else:
            pointer_name = str(fact.metadata.get("pointer_name", ""))
            memory_type = _resolve_pointer_memory_type(pointer_name, tag_map)
            if memory_type is None:
                code = CLK_PTR_DS_UNVERIFIED
                findings.append(
                    ClickFinding(
                        code=code,
                        severity=_route_severity(code, mode),
                        message=(
                            f"Pointer '{pointer_name}' memory type could not be verified "
                            f"as DS at {location_str}."
                        ),
                        location=location_str,
                        suggestion=_build_suggestion(code, fact, tag_map),
                    )
                )
            elif memory_type != "DS":
                code = CLK_PTR_POINTER_MUST_BE_DS
                findings.append(
                    ClickFinding(
                        code=code,
                        severity=_route_severity(code, mode),
                        message=(
                            f"Pointer '{pointer_name}' is mapped to {memory_type}, "
                            f"not DS at {location_str}."
                        ),
                        location=location_str,
                        suggestion=_build_suggestion(code, fact, tag_map),
                    )
                )

    if fact.value_kind == "indirect_expr_ref":
        findings.append(
            ClickFinding(
                code=CLK_PTR_EXPR_NOT_ALLOWED,
                severity=_route_severity(CLK_PTR_EXPR_NOT_ALLOWED, mode),
                message=f"Computed pointer expression (IndirectExprRef) not allowed at {location_str}.",
                location=location_str,
                suggestion=_build_suggestion(CLK_PTR_EXPR_NOT_ALLOWED, fact, tag_map),
            )
        )

    if fact.value_kind == "expression":
        allowed = (
            loc.instruction_type == "MathInstruction" and loc.arg_path == "instruction.expression"
        )
        if not allowed:
            findings.append(
                ClickFinding(
                    code=CLK_EXPR_ONLY_IN_MATH,
                    severity=_route_severity(CLK_EXPR_ONLY_IN_MATH, mode),
                    message=f"Expression used outside math instruction at {location_str}.",
                    location=location_str,
                    suggestion=_build_suggestion(CLK_EXPR_ONLY_IN_MATH, fact, tag_map),
                )
            )

    if fact.value_kind == "indirect_block_range":
        findings.append(
            ClickFinding(
                code=CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED,
                severity=_route_severity(CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED, mode),
                message=(
                    f"IndirectBlockRange not allowed at {location_str}. "
                    "Click hardware does not support computed block ranges."
                ),
                location=location_str,
                suggestion=_build_suggestion(CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED, fact, tag_map),
            )
        )

    return findings


# ---------------------------------------------------------------------------
# Stage 3 helpers (R6-R8)
# ---------------------------------------------------------------------------


def _load_default_profile() -> HardwareProfile | None:
    from pyrung.click.profile import load_default_profile

    return load_default_profile()


def _instruction_location(base: ProgramLocation, arg_path: str) -> ProgramLocation:
    return ProgramLocation(
        scope=base.scope,
        subroutine=base.subroutine,
        rung_index=base.rung_index,
        branch_path=base.branch_path,
        instruction_index=base.instruction_index,
        instruction_type=base.instruction_type,
        arg_path=arg_path,
    )


def _resolve_direct_tag(tag: Tag, tag_map: TagMap) -> _ResolvedSlot | None:
    try:
        mapped_address = tag_map.resolve(tag)
        memory_type, address = parse_address(mapped_address)
        return _ResolvedSlot(memory_type=memory_type, address=address)
    except (KeyError, TypeError, ValueError):
        pass

    try:
        memory_type, address = parse_address(tag.name)
    except ValueError:
        return None
    return _ResolvedSlot(memory_type=memory_type, address=address)


def _resolve_block_memory_type(block_name: str, tag_map: TagMap) -> str | None:
    entry = tag_map.block_entry_by_name(block_name)
    if entry is not None and entry.hardware_addresses:
        hardware_slot = entry.hardware.block[entry.hardware_addresses[0]]
        try:
            memory_type, _ = parse_address(hardware_slot.name)
            return memory_type
        except ValueError:
            return None

    if block_name in _KNOWN_BANKS:
        return block_name

    return None


def _unique_slots(slots: list[_ResolvedSlot]) -> tuple[_ResolvedSlot, ...]:
    seen: set[tuple[str, int | None]] = set()
    ordered: list[_ResolvedSlot] = []
    for slot in slots:
        key = (slot.memory_type, slot.address)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(slot)
    return tuple(ordered)


def _resolve_operand_slots(value: Any, tag_map: TagMap) -> _OperandResolution:
    if isinstance(value, Tag):
        resolved = _resolve_direct_tag(value, tag_map)
        if resolved is None:
            return _OperandResolution(unresolved=True)
        return _OperandResolution(slots=(resolved,))

    if isinstance(value, BlockRange):
        slots: list[_ResolvedSlot] = []
        unresolved = False
        for tag in value.tags():
            resolved = _resolve_direct_tag(tag, tag_map)
            if resolved is None:
                unresolved = True
                continue
            slots.append(resolved)
        if unresolved:
            return _OperandResolution(unresolved=True)
        return _OperandResolution(slots=_unique_slots(slots))

    if isinstance(value, IndirectBlockRange):
        memory_type = _resolve_block_memory_type(value.block.name, tag_map)
        if memory_type is None:
            return _OperandResolution(unresolved=True)
        return _OperandResolution(slots=(_ResolvedSlot(memory_type=memory_type, address=None),))

    if isinstance(value, (IndirectRef, IndirectExprRef)):
        memory_type = _resolve_block_memory_type(value.block.name, tag_map)
        if memory_type is None:
            return _OperandResolution(unresolved=True)
        return _OperandResolution(slots=(_ResolvedSlot(memory_type=memory_type, address=None),))

    return _OperandResolution()


def _iter_instruction_sites(program: Program) -> list[tuple[Any, ProgramLocation]]:
    sites: list[tuple[Any, ProgramLocation]] = []

    def walk_rung(
        rung: Any,
        *,
        scope: Literal["main", "subroutine"],
        subroutine: str | None,
        rung_index: int,
        branch_path: tuple[int, ...],
    ) -> None:
        def walk_instruction(instruction: Any, instruction_index: int) -> None:
            sites.append(
                (
                    instruction,
                    ProgramLocation(
                        scope=scope,
                        subroutine=subroutine,
                        rung_index=rung_index,
                        branch_path=branch_path,
                        instruction_index=instruction_index,
                        instruction_type=type(instruction).__name__,
                        arg_path="instruction",
                    ),
                )
            )

            # ForLoopInstruction captures nested instructions in-order.
            if hasattr(instruction, "instructions"):
                for child_instruction in instruction.instructions:
                    walk_instruction(child_instruction, instruction_index)

        for instruction_index, instruction in enumerate(rung._instructions):
            walk_instruction(instruction, instruction_index)

        for branch_index, branch in enumerate(rung._branches):
            walk_rung(
                branch,
                scope=scope,
                subroutine=subroutine,
                rung_index=rung_index,
                branch_path=branch_path + (branch_index,),
            )

    for rung_index, rung in enumerate(program.rungs):
        walk_rung(rung, scope="main", subroutine=None, rung_index=rung_index, branch_path=())

    for subroutine_name in sorted(program.subroutines):
        for rung_index, rung in enumerate(program.subroutines[subroutine_name]):
            walk_rung(
                rung,
                scope="subroutine",
                subroutine=subroutine_name,
                rung_index=rung_index,
                branch_path=(),
            )

    return sites


def _bank_label(slot: _ResolvedSlot) -> str:
    if slot.address is None:
        return slot.memory_type
    return f"{slot.memory_type}{slot.address}"


def _unresolved_finding(
    location: ProgramLocation, mode: ValidationMode, reason: str
) -> ClickFinding:
    location_text = _format_location(location)
    return ClickFinding(
        code=CLK_BANK_UNRESOLVED,
        severity=_route_severity(CLK_BANK_UNRESOLVED, mode),
        message=f"Bank resolution failed at {location_text}: {reason}.",
        location=location_text,
    )


def _evaluate_r6(
    instruction: Any,
    base_location: ProgramLocation,
    tag_map: TagMap,
    profile: HardwareProfile,
    mode: ValidationMode,
) -> list[ClickFinding]:
    findings: list[ClickFinding] = []
    instruction_type = type(instruction).__name__

    for candidate_type, field_name in _R6_WRITE_TARGETS:
        if instruction_type != candidate_type:
            continue

        location = _instruction_location(base_location, f"instruction.{field_name}")
        resolution = _resolve_operand_slots(getattr(instruction, field_name), tag_map)

        if resolution.unresolved:
            findings.append(_unresolved_finding(location, mode, "mapping missing or ambiguous"))
            continue

        for slot in resolution.slots:
            if slot.address is None and slot.memory_type in {"SC", "SD"}:
                findings.append(
                    _unresolved_finding(location, mode, "address required for SC/SD writability")
                )
                continue

            if not profile.is_writable(slot.memory_type, slot.address):
                location_text = _format_location(location)
                findings.append(
                    ClickFinding(
                        code=CLK_BANK_NOT_WRITABLE,
                        severity=_route_severity(CLK_BANK_NOT_WRITABLE, mode),
                        message=(
                            f"Write target {_bank_label(slot)} is not writable for Click "
                            f"at {location_text}."
                        ),
                        location=location_text,
                    )
                )

    return findings


def _evaluate_r7(
    instruction: Any,
    base_location: ProgramLocation,
    tag_map: TagMap,
    profile: HardwareProfile,
    mode: ValidationMode,
) -> list[ClickFinding]:
    findings: list[ClickFinding] = []
    instruction_type = type(instruction).__name__

    for (candidate_type, field_name), role in _R7_ROLE_FIELDS.items():
        if instruction_type != candidate_type:
            continue

        value = getattr(instruction, field_name)
        if field_name == "setpoint" and not isinstance(value, Tag):
            continue

        location = _instruction_location(base_location, f"instruction.{field_name}")
        resolution = _resolve_operand_slots(value, tag_map)

        if resolution.unresolved:
            findings.append(_unresolved_finding(location, mode, "mapping missing or ambiguous"))
            continue

        for slot in resolution.slots:
            if not profile.valid_for_role(slot.memory_type, role):
                location_text = _format_location(location)
                findings.append(
                    ClickFinding(
                        code=CLK_BANK_WRONG_ROLE,
                        severity=_route_severity(CLK_BANK_WRONG_ROLE, mode),
                        message=(
                            f"Bank {_bank_label(slot)} is invalid for role {role} "
                            f"at {location_text}."
                        ),
                        location=location_text,
                    )
                )

    return findings


def _evaluate_r8(
    instruction: Any,
    base_location: ProgramLocation,
    tag_map: TagMap,
    profile: HardwareProfile,
    mode: ValidationMode,
) -> list[ClickFinding]:
    findings: list[ClickFinding] = []
    instruction_type = type(instruction).__name__
    mapping = _R8_COPY_FIELDS.get(instruction_type)
    if mapping is None:
        return findings

    operation, source_field, dest_field = mapping
    source_location = _instruction_location(base_location, f"instruction.{source_field}")
    dest_location = _instruction_location(base_location, f"instruction.{dest_field}")

    source_resolution = _resolve_operand_slots(getattr(instruction, source_field), tag_map)
    dest_resolution = _resolve_operand_slots(getattr(instruction, dest_field), tag_map)

    if source_resolution.unresolved:
        findings.append(_unresolved_finding(source_location, mode, "source bank unresolved"))
    if dest_resolution.unresolved:
        findings.append(_unresolved_finding(dest_location, mode, "destination bank unresolved"))
    if source_resolution.unresolved or dest_resolution.unresolved:
        return findings

    if not source_resolution.slots or not dest_resolution.slots:
        return findings

    for source_slot in source_resolution.slots:
        for dest_slot in dest_resolution.slots:
            if not profile.copy_compatible(
                operation, source_slot.memory_type, dest_slot.memory_type
            ):
                location_text = _format_location(dest_location)
                findings.append(
                    ClickFinding(
                        code=CLK_COPY_BANK_INCOMPATIBLE,
                        severity=_route_severity(CLK_COPY_BANK_INCOMPATIBLE, mode),
                        message=(
                            f"Copy operation {operation} is incompatible for "
                            f"{source_slot.memory_type} -> {dest_slot.memory_type} "
                            f"at {location_text}."
                        ),
                        location=location_text,
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_click_program(
    program: Program,
    tag_map: TagMap,
    mode: ValidationMode = "warn",
    profile: HardwareProfile | None = None,
) -> ClickValidationReport:
    """Validate a Program against Click portability rules."""
    facts = walk_program(program)

    findings: list[ClickFinding] = []
    for fact in facts.operands:
        findings.extend(_evaluate_fact(fact, tag_map, mode))

    active_profile = profile if profile is not None else _load_default_profile()

    if active_profile is None:
        findings.append(
            ClickFinding(
                code=CLK_PROFILE_UNAVAILABLE,
                severity=_route_severity(CLK_PROFILE_UNAVAILABLE, mode),
                message="Click hardware profile is unavailable; skipped Stage 3 checks (R6-R8).",
                location="program",
            )
        )
    else:
        for instruction, base_location in _iter_instruction_sites(program):
            findings.extend(_evaluate_r6(instruction, base_location, tag_map, active_profile, mode))
            findings.extend(_evaluate_r7(instruction, base_location, tag_map, active_profile, mode))
            findings.extend(_evaluate_r8(instruction, base_location, tag_map, active_profile, mode))

    errors: list[ClickFinding] = []
    warnings: list[ClickFinding] = []
    hints: list[ClickFinding] = []

    for finding in findings:
        if finding.severity == "error":
            errors.append(finding)
        elif finding.severity == "warning":
            warnings.append(finding)
        else:
            hints.append(finding)

    return ClickValidationReport(
        errors=tuple(errors),
        warnings=tuple(warnings),
        hints=tuple(hints),
    )
