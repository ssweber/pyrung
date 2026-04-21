"""Physical realism validation for pyrung programs.

Detects statically visible violations of tag range hints and conservative
physical feedback hazards.  Dynamic writes and ambiguous timing cases are
skipped by design.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.condition import (
    AllCondition,
    FallingEdgeCondition,
    RisingEdgeCondition,
)
from pyrung.core.tag import Tag, TagType
from pyrung.core.validation._common import (
    WriteSite,
    _build_tag_map,
    _chain_pair_mutually_exclusive,
    _collect_write_sites,
    _format_site_location,
    _resolve_tag_names,
    _resolve_tag_objects,
)

if TYPE_CHECKING:
    from pyrung.core.program import Program

CORE_RANGE_VIOLATION = "CORE_RANGE_VIOLATION"
CORE_MISSING_PROFILE = "CORE_MISSING_PROFILE"
CORE_ANTITOGGLE = "CORE_ANTITOGGLE"


@dataclass(frozen=True)
class PhysicalRealismFinding:
    code: str
    target_name: str
    message: str
    site: WriteSite | None = None
    sites: tuple[WriteSite, ...] = ()
    value: Any = None


@dataclass(frozen=True)
class PhysicalRealismReport:
    findings: tuple[PhysicalRealismFinding, ...]

    def summary(self) -> str:
        if not self.findings:
            return "No physical realism findings."
        return f"{len(self.findings)} physical realism finding(s)."


def _is_numeric_literal(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _literal_range_targets(instr: Any) -> list[tuple[Tag, str, int | float]]:
    from pyrung.core.instruction.data_transfer import CopyInstruction, FillInstruction

    itype = type(instr).__name__

    if isinstance(instr, CopyInstruction):
        source = instr.source
        if not _is_numeric_literal(source):
            return []
        return [(tag, itype, source) for tag in _resolve_tag_objects(instr.target)]

    if isinstance(instr, FillInstruction):
        source = instr.value
        if not _is_numeric_literal(source):
            return []
        return [(tag, itype, source) for tag in _resolve_tag_objects(instr.dest)]

    return []


def _literal_write_sites(program: Program) -> list[tuple[WriteSite, int | float, Tag]]:
    from pyrung.core.instruction.control import ForLoopInstruction
    from pyrung.core.validation._common import FactScope

    literal_sites: list[tuple[WriteSite, int | float, Tag]] = []

    def _walk_instructions(
        instructions: list[Any],
        scope: FactScope,
        subroutine: str | None,
        rung_index: int,
        branch_path: tuple[int, ...],
        conditions: tuple[Any, ...],
    ) -> None:
        for instr_idx, instr in enumerate(instructions):
            for tag, itype, value in _literal_range_targets(instr):
                literal_sites.append(
                    (
                        WriteSite(
                            target_name=tag.name,
                            scope=scope,
                            subroutine=subroutine,
                            rung_index=rung_index,
                            branch_path=branch_path,
                            instruction_index=instr_idx,
                            instruction_type=itype,
                            conditions=conditions,
                            source_file=getattr(instr, "source_file", None),
                            source_line=getattr(instr, "source_line", None),
                        ),
                        value,
                        tag,
                    )
                )
            if isinstance(instr, ForLoopInstruction) and hasattr(instr, "instructions"):
                _walk_instructions(
                    instr.instructions, scope, subroutine, rung_index, branch_path, conditions
                )

    def _walk_rung(
        rung: Any,
        scope: FactScope,
        subroutine: str | None,
        rung_index: int,
        branch_path: tuple[int, ...],
    ) -> None:
        conditions = tuple(rung._conditions)
        _walk_instructions(
            rung._instructions, scope, subroutine, rung_index, branch_path, conditions
        )
        for branch_idx, branch_rung in enumerate(rung._branches):
            _walk_rung(branch_rung, scope, subroutine, rung_index, branch_path + (branch_idx,))

    for rung_index, rung in enumerate(program.rungs):
        _walk_rung(rung, "main", None, rung_index, ())
    for sub_name in sorted(program.subroutines):
        for rung_index, rung in enumerate(program.subroutines[sub_name]):
            _walk_rung(rung, "subroutine", sub_name, rung_index, ())

    return literal_sites


def _linked_command_name(feedback: Tag, tag_map: dict[str, Tag]) -> str | None:
    link = feedback.link
    if link is None:
        return None

    runtime = getattr(feedback, "_pyrung_structure_runtime", None)
    index = getattr(feedback, "_pyrung_structure_index", None)
    blocks = getattr(runtime, "_blocks", None)
    if isinstance(blocks, dict) and isinstance(index, int):
        block = blocks.get(link)
        if block is None:
            return None
        try:
            return block[index].name
        except Exception:
            return None

    if link in tag_map:
        return link
    return link


def _linked_feedback_by_command(tag_map: dict[str, Tag]) -> dict[str, list[Tag]]:
    result: dict[str, list[Tag]] = defaultdict(list)
    for tag in tag_map.values():
        if tag.link is None:
            continue
        command_name = _linked_command_name(tag, tag_map)
        if command_name is not None:
            result[command_name].append(tag)
    return result


def _contains_edge_condition(conditions: tuple[Any, ...]) -> bool:
    for condition in conditions:
        if isinstance(condition, (RisingEdgeCondition, FallingEdgeCondition)):
            return True
        if isinstance(condition, AllCondition) and _contains_edge_condition(
            tuple(condition.conditions)
        ):
            return True
    return False


def _has_full_bool_cycle_timing(feedback: Tag) -> bool:
    physical = feedback.physical
    return (
        feedback.type == TagType.BOOL
        and physical is not None
        and physical.on_delay_ms is not None
        and physical.off_delay_ms is not None
    )


def _cycle_floor_ms(feedback: Tag) -> int | None:
    if not _has_full_bool_cycle_timing(feedback):
        return None
    assert feedback.physical is not None
    assert feedback.physical.on_delay_ms is not None
    assert feedback.physical.off_delay_ms is not None
    return feedback.physical.on_delay_ms + feedback.physical.off_delay_ms


def _out_targets(instr: Any) -> list[tuple[str, str]]:
    from pyrung.core.instruction.coils import OutInstruction

    if isinstance(instr, OutInstruction):
        return [(name, type(instr).__name__) for name in _resolve_tag_names(instr.target)]
    return []


def _opposing_out_pair(site_a: WriteSite, site_b: WriteSite) -> bool:
    if not site_a.conditions and not site_b.conditions:
        return False
    if site_a.conditions and site_b.conditions:
        return False
    return not _chain_pair_mutually_exclusive(site_a.conditions, site_b.conditions)


def _validate_ranges(program: Program, tag_map: dict[str, Tag]) -> list[PhysicalRealismFinding]:
    findings: list[PhysicalRealismFinding] = []
    for site, value, literal_target in _literal_write_sites(program):
        tag = tag_map.get(site.target_name, literal_target)
        too_low = tag.min is not None and value < tag.min
        too_high = tag.max is not None and value > tag.max
        if not too_low and not too_high:
            continue
        loc = _format_site_location(site)
        message = (
            f"Tag '{site.target_name}' has range {tag.min!r}..{tag.max!r} "
            f"but write site copies literal {value!r}:\n  - {loc}"
        )
        findings.append(
            PhysicalRealismFinding(
                code=CORE_RANGE_VIOLATION,
                target_name=site.target_name,
                value=value,
                site=site,
                message=message,
            )
        )
    return findings


def _validate_missing_profiles(tag_map: dict[str, Tag]) -> list[PhysicalRealismFinding]:
    findings: list[PhysicalRealismFinding] = []
    for tag_name in sorted(tag_map):
        tag = tag_map[tag_name]
        if tag.link is None or tag.type == TagType.BOOL:
            continue
        if tag.physical is not None and tag.physical.profile is not None:
            continue
        findings.append(
            PhysicalRealismFinding(
                code=CORE_MISSING_PROFILE,
                target_name=tag.name,
                message=(
                    f"Linked analog feedback '{tag.name}' should declare "
                    "physical=Physical(..., profile=...)."
                ),
            )
        )
    return findings


def _validate_antitoggle(
    program: Program,
    tag_map: dict[str, Tag],
    *,
    dt: float,
) -> list[PhysicalRealismFinding]:
    linked_by_command = _linked_feedback_by_command(tag_map)
    out_sites = _collect_write_sites(program, target_extractor=_out_targets)
    out_by_target: dict[str, list[WriteSite]] = defaultdict(list)
    for site in out_sites:
        out_by_target[site.target_name].append(site)

    findings: list[PhysicalRealismFinding] = []
    seen: set[tuple[str, str, int, tuple[int, ...], int]] = set()
    dt_ms = dt * 1000

    for command_name in sorted(linked_by_command):
        timed_feedbacks = [
            feedback
            for feedback in linked_by_command[command_name]
            if _has_full_bool_cycle_timing(feedback)
        ]
        if not timed_feedbacks:
            continue
        cycle_floor = max(_cycle_floor_ms(feedback) or 0 for feedback in timed_feedbacks)
        if dt_ms >= cycle_floor:
            continue

        for site in out_by_target.get(command_name, []):
            if _contains_edge_condition(site.conditions):
                key = (
                    command_name,
                    site.scope,
                    site.rung_index,
                    site.branch_path,
                    site.instruction_index,
                )
                if key in seen:
                    continue
                seen.add(key)
                loc = _format_site_location(site)
                findings.append(
                    PhysicalRealismFinding(
                        code=CORE_ANTITOGGLE,
                        target_name=command_name,
                        site=site,
                        sites=(site,),
                        message=(
                            f"Linked command '{command_name}' is driven by a one-scan edge "
                            f"pulse ({dt_ms:g} ms) faster than feedback cycle floor "
                            f"{cycle_floor:g} ms:\n  - {loc}"
                        ),
                    )
                )

        target_sites = out_by_target.get(command_name, [])
        for i in range(len(target_sites)):
            for j in range(i + 1, len(target_sites)):
                site_a = target_sites[i]
                site_b = target_sites[j]
                if not _opposing_out_pair(site_a, site_b):
                    continue
                findings.append(
                    PhysicalRealismFinding(
                        code=CORE_ANTITOGGLE,
                        target_name=command_name,
                        sites=(site_a, site_b),
                        message=(
                            f"Linked command '{command_name}' has same-scan opposing OUT "
                            f"write sites faster than feedback cycle floor {cycle_floor:g} ms:\n"
                            f"  - {_format_site_location(site_a)}\n"
                            f"  - {_format_site_location(site_b)}"
                        ),
                    )
                )

    return findings


def validate_physical_realism(program: Program, *, dt: float = 0.010) -> PhysicalRealismReport:
    """Validate static physical realism hints on a Program.

    ``dt`` is the scan period in seconds, defaulting to the runner's fixed-step
    default of 10 ms.
    """
    if dt <= 0:
        raise ValueError("dt must be > 0 seconds.")

    tag_map = _build_tag_map(program)
    findings: list[PhysicalRealismFinding] = []
    findings.extend(_validate_ranges(program, tag_map))
    findings.extend(_validate_missing_profiles(tag_map))
    findings.extend(_validate_antitoggle(program, tag_map, dt=dt))
    return PhysicalRealismReport(findings=tuple(findings))
