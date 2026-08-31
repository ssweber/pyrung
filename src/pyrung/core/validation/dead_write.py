"""Conservative ordered-scan detection of overwritten direct writes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.write_sites import instruction_write_targets
from pyrung.core.instruction.calc import CalcInstruction
from pyrung.core.instruction.coils import LatchInstruction, OutInstruction, ResetInstruction
from pyrung.core.instruction.control import (
    CallInstruction,
    EnabledFunctionCallInstruction,
    ForLoopInstruction,
    FunctionCallInstruction,
)
from pyrung.core.instruction.data_transfer import CopyInstruction
from pyrung.core.memory_block import IndirectExprRef, IndirectRef
from pyrung.core.tag import ImmediateRef, Tag
from pyrung.core.validation._common import WriteSite, site_frame
from pyrung.core.validation.display import FindingDisplay, _FindingTextMixin
from pyrung.core.validation.severity import Severity

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.program import Program
    from pyrung.core.rung import Rung


TAG_DEAD_WRITE = "TAG_DEAD_WRITE"
_AMBIGUOUS_INSTRUCTIONS = (
    CallInstruction,
    EnabledFunctionCallInstruction,
    ForLoopInstruction,
    FunctionCallInstruction,
)


@dataclass(frozen=True)
class _SimpleSite:
    rung_index: int
    instruction: Any
    tag: Tag
    site: WriteSite


@dataclass(frozen=True)
class DeadWriteFinding(_FindingTextMixin):
    code: str
    target_name: str
    overwritten_at: str
    display: FindingDisplay
    severity: Severity = "warning"

    @property
    def message(self) -> str:
        return self.display.as_text()


@dataclass(frozen=True)
class DeadWriteReport:
    findings: tuple[DeadWriteFinding, ...]

    def summary(self) -> str:
        if not self.findings:
            return "No dead-write findings."
        return f"{len(self.findings)} dead-write finding(s)."


def _direct_scalar_target(instr: Any) -> Tag | None:
    targets = instruction_write_targets(instr)
    if len(targets) != 1:
        return None
    target = targets[0]
    if isinstance(target, ImmediateRef):
        target = target.value
    return target if isinstance(target, Tag) else None


def _simple_sites(program: Program, graph: ProgramGraph) -> tuple[_SimpleSite, ...]:
    node_by_rung = graph.main_node_by_rung()
    sites: list[_SimpleSite] = []
    for rung_index, rung in enumerate(program.rungs):
        if rung._branches or len(rung._instructions) != 1:
            continue
        instr = rung._instructions[0]
        if isinstance(instr, _AMBIGUOUS_INSTRUCTIONS):
            continue
        target = _direct_scalar_target(instr)
        node_index = node_by_rung.get(rung_index)
        if target is None or node_index is None:
            continue
        sites.append(
            _SimpleSite(
                rung_index=rung_index,
                instruction=instr,
                tag=target,
                site=WriteSite(
                    target_name=target.name,
                    scope="main",
                    subroutine=None,
                    rung_index=rung_index,
                    branch_path=(),
                    instruction_index=0,
                    instruction_type=type(instr).__name__,
                    conditions=tuple(rung._conditions),
                    source_file=getattr(instr, "source_file", None),
                    source_line=getattr(instr, "source_line", None),
                    instruction=instr,
                ),
            )
        )
    return tuple(sites)


def _guaranteed_write(site: _SimpleSite, rung: Rung) -> bool:
    if getattr(site.instruction, "_oneshot", False):
        return False
    instr = site.instruction
    if isinstance(instr, OutInstruction):
        return True
    if rung._conditions:
        return False
    if isinstance(instr, CopyInstruction):
        return instr.convert is None and not isinstance(
            instr.source, (IndirectRef, IndirectExprRef)
        )
    return isinstance(instr, (CalcInstruction, LatchInstruction, ResetInstruction))


def _ambiguous_between(program: Program, start: int, end: int) -> bool:
    """Whether the main interval contains control flow this first pass punts on."""
    for rung in program.rungs[start : end + 1]:
        if rung._branches:
            return True
        if any(isinstance(instr, _AMBIGUOUS_INSTRUCTIONS) for instr in rung._instructions):
            return True
    return False


def validate_dead_writes(program: Program) -> DeadWriteReport:
    """Report simple direct writes guaranteed to be overwritten before a read."""
    from pyrung.core.analysis.pdg import build_program_graph

    graph = build_program_graph(program)
    sites = _simple_sites(program, graph)
    findings: list[DeadWriteFinding] = []

    for index, first in enumerate(sites):
        for later in sites[index + 1 :]:
            if later.tag.name != first.tag.name:
                continue
            if _ambiguous_between(program, first.rung_index, later.rung_index):
                break
            if not _guaranteed_write(later, program.rungs[later.rung_index]):
                continue
            reader_nodes = graph.all_readers_of.get(first.tag.name, frozenset())
            intervening_nodes = {
                graph.main_node_by_rung()[rung_index]
                for rung_index in range(first.rung_index + 1, later.rung_index + 1)
                if rung_index in graph.main_node_by_rung()
            }
            if reader_nodes & intervening_nodes:
                break

            display = FindingDisplay(
                code=TAG_DEAD_WRITE,
                severity="warning",
                problem=f"The write to {first.tag.name} is overwritten before it can be read.",
                frames=(
                    site_frame(first.site, caret_label="dead write"),
                    site_frame(later.site, caret_label="overwrites it"),
                ),
                hint="remove the first write or move the read before the overwrite",
            )
            findings.append(
                DeadWriteFinding(
                    code=TAG_DEAD_WRITE,
                    target_name=first.tag.name,
                    overwritten_at=f"Main:R{later.rung_index + 1}",
                    display=display,
                )
            )
            break

    return DeadWriteReport(findings=tuple(findings))


__all__ = [
    "TAG_DEAD_WRITE",
    "DeadWriteFinding",
    "DeadWriteReport",
    "validate_dead_writes",
]
