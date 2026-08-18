"""Static program facts used to prepare Pilot's navigation world.

These classifiers read program structure only. They do not trace a target,
choose a route, execute a scan, or own execution evidence.
"""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis import steerable as _steerable
from pyrung.core.analysis.pdg import ProgramGraph, TagRole, resolve_rung
from pyrung.core.analysis.pilot.static_expressions import single_calc_source
from pyrung.core.analysis.simplified import _sp_to_expr
from pyrung.core.analysis.sp_values import _expr_tag_names, _values_match


def compute_reference_constants(
    pdg: ProgramGraph, program: Any, known: dict[str, Any] | None = None
) -> frozenset[str]:
    """Never-written, non-external tags used as program reference values.

    Two structural families qualify:

    * a copy/fill source feeding a lookup-table pointer chain (the generated
      state-machine reference idiom); or
    * a tag used as both ``copy(REFERENCE, State)`` and the live RHS of
      ``State == REFERENCE`` (the direct named-state idiom).

    In either family, all four conditions hold:

    1. Tag has no writers (initial-value only)
    2. Used as a copy/fill source feeding some destination D
    3. D consumes that tag as a static reference, either through a table
       pipeline or an explicit tag-valued comparison
    4. Tag is **not** ``external`` — a declared program constant, not an
       operator/field interface (given *known*; unchecked when *known* is None).
    """
    from pyrung.core.instruction.data_transfer import CopyInstruction, FillInstruction
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef

    pointer_tags: set[str] = set()

    def _scan_pointers(rungs: Any) -> None:
        for rung in rungs:
            for instruction in getattr(rung, "_instructions", ()):
                if isinstance(instruction, CopyInstruction):
                    source = instruction.source
                    if isinstance(source, IndirectRef):
                        name = getattr(source.pointer, "name", None)
                        if name:
                            pointer_tags.add(name)
                    elif isinstance(source, IndirectExprRef):
                        names = _expr_tag_names(source.expr)
                        if names:
                            pointer_tags.update(names)
            _scan_pointers(getattr(rung, "_branches", ()))

    _scan_pointers(program.rungs)
    for subroutine_rungs in getattr(program, "subroutines", {}).values():
        _scan_pointers(subroutine_rungs)

    pipeline_tags = set(pointer_tags)
    for pointer in list(pointer_tags):
        tag = pointer
        for _ in range(3):
            definition = single_calc_source(tag, pdg, program)
            if definition is None:
                break
            _expression, representative = definition
            pipeline_tags.add(representative)
            tag = representative

    from pyrung.core.analysis.simplified import And, Atom, Or, _conditions_list_to_expr

    comparison_reference_pairs: set[tuple[str, str]] = set()

    def _scan_reference_expr(expression: Any) -> None:
        if isinstance(expression, Atom):
            if expression.operand_is_tag:
                comparison_reference_pairs.add((expression.operand, expression.tag))
            return
        if isinstance(expression, (And, Or)):
            for term in expression.terms:
                _scan_reference_expr(term)

    def _scan_reference_conditions(rungs: Any) -> None:
        for rung in rungs:
            _scan_reference_expr(_conditions_list_to_expr(getattr(rung, "_conditions", [])))
            _scan_reference_conditions(getattr(rung, "_branches", ()))

    _scan_reference_conditions(program.rungs)
    for subroutine_rungs in getattr(program, "subroutines", {}).values():
        _scan_reference_conditions(subroutine_rungs)

    candidates: set[str] = set()

    def _is_direct_declaration(name: str) -> bool:
        return known is None or getattr(known.get(name), "_pyrung_block", None) is None

    def _scan_sources(rungs: Any) -> None:
        for rung in rungs:
            for instruction in getattr(rung, "_instructions", ()):
                if isinstance(instruction, CopyInstruction):
                    source_name = getattr(instruction.source, "name", None)
                    destination_name = getattr(instruction.dest, "name", None)
                    if (
                        source_name
                        and destination_name
                        and (
                            destination_name in pipeline_tags
                            or (
                                (source_name, destination_name) in comparison_reference_pairs
                                and _is_direct_declaration(source_name)
                            )
                        )
                    ):
                        candidates.add(source_name)
                elif isinstance(instruction, FillInstruction):
                    source_name = getattr(instruction.value, "name", None)
                    destination_name = getattr(instruction.dest, "name", None)
                    if (
                        source_name
                        and destination_name
                        and (
                            destination_name in pipeline_tags
                            or (
                                (source_name, destination_name) in comparison_reference_pairs
                                and _is_direct_declaration(source_name)
                            )
                        )
                    ):
                        candidates.add(source_name)
            _scan_sources(getattr(rung, "_branches", ()))

    _scan_sources(program.rungs)
    for subroutine_rungs in getattr(program, "subroutines", {}).values():
        _scan_sources(subroutine_rungs)

    from pyrung.core.analysis.pdg import _indirect_expr_base_tag, _indirect_ref_tags

    def _indirect_read_slots(source: Any) -> list[str]:
        if isinstance(source, IndirectRef):
            tags = _indirect_ref_tags(source.block, source.pointer)
        elif isinstance(source, IndirectExprRef):
            base = _indirect_expr_base_tag(source.expr)
            tags = _indirect_ref_tags(source.block, base) if base is not None else None
        else:
            return []
        return [tag.name for tag in tags] if tags is not None else []

    def _scan_indirect_reads(rungs: Any) -> None:
        for rung in rungs:
            for instruction in getattr(rung, "_instructions", ()):
                if isinstance(instruction, CopyInstruction):
                    candidates.update(_indirect_read_slots(instruction.source))
            _scan_indirect_reads(getattr(rung, "_branches", ()))

    _scan_indirect_reads(program.rungs)
    for subroutine_rungs in getattr(program, "subroutines", {}).values():
        _scan_indirect_reads(subroutine_rungs)

    def _is_program_constant(name: str) -> bool:
        if pdg.writers_of.get(name, frozenset()):
            return False
        if known is not None and getattr(known.get(name), "external", False):
            return False
        return True

    return frozenset(name for name in candidates if _is_program_constant(name))


def compute_edge_tags(pdg: ProgramGraph, program: Any) -> set[str]:
    """Tag names read through ``rise()``/``fall()`` anywhere in the program."""
    from pyrung.core.analysis.simplified import And, Atom, Or

    result: set[str] = set()

    def visit(expression: Any) -> None:
        if isinstance(expression, Atom):
            if expression.form in ("rise", "fall"):
                result.add(expression.tag)
        elif isinstance(expression, (And, Or)):
            for term in expression.terms:
                visit(term)

    seen: set[int] = set()
    for rung_node in pdg.rung_nodes:
        rung = resolve_rung(program, rung_node)
        if rung is None or id(rung) in seen:
            continue
        seen.add(id(rung))
        series_parallel = rung.sp_tree()
        if series_parallel is not None:
            visit(_sp_to_expr(series_parallel))
    return result


def compute_resting_values(
    steerable: frozenset[str],
    known: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
) -> dict[str, Any]:
    """Map each steerable input to its declared or proved resting value."""

    resting: dict[str, Any] = {}
    for name in steerable:
        tag = known.get(name)
        if tag is None:
            resting[name] = False
            continue
        transient, rest_value = scan_transient_rest(name, pdg, program)
        if transient and rest_value is not None:
            resting[name] = rest_value
        else:
            resting[name] = getattr(tag, "default", False)
    return resting


def scan_transient_rest(
    tag: str,
    pdg: ProgramGraph,
    program: Any,
) -> tuple[bool, Any]:
    """Whether *tag* provably rests at one value at every scan boundary."""
    from pyrung.core.analysis.simplified import Atom, Or

    if pdg.tag_roles.get(tag) == TagRole.INPUT:
        return False, None

    writer_indices = pdg.writers_of.get(tag, frozenset())
    if not writer_indices:
        return False, None
    writes: list[tuple[Any, Any, Any]] = []
    for rung_index in writer_indices:
        rung_node = pdg.rung_nodes[rung_index]
        rung = resolve_rung(program, rung_node)
        if rung is None or tag in rung_node.ote_writes:
            return False, None
        literal_write = _steerable._literal_write(rung, tag)
        if literal_write is None:
            return False, None
        writes.append((rung_node, rung, literal_write))

    candidate_rests: list[Any] = []
    for _node, _rung, value in writes:
        if not any(_values_match(value, candidate) for candidate in candidate_rests):
            candidate_rests.append(value)

    def _main_execution_position(node: Any) -> int | None:
        if node.subroutine is None:
            return node.rung_index
        sites = pdg.call_site_rung_indices().get(node.subroutine, frozenset())
        return next(iter(sites)) if len(sites) == 1 else None

    for rest in candidate_rests:
        producers = [(node, value) for node, _rung, value in writes if not _values_match(value, rest)]
        clearers = [(node, rung) for node, rung, value in writes if _values_match(value, rest)]
        if not producers or not clearers:
            continue
        producer_scopes = {node.subroutine for node, _value in producers}
        produced_values = [value for _node, value in producers]

        def _fires_when_set(
            expression: Any,
            produced: tuple[Any, ...] = tuple(produced_values),
        ) -> bool:
            if isinstance(expression, Atom):
                if expression.tag != tag:
                    return False
                if expression.form in ("xic", "truthy"):
                    return all(bool(value) for value in produced)
                return expression.form == "eq" and all(
                    _values_match(expression.operand, value) for value in produced
                )
            if isinstance(expression, Or):
                return any(_fires_when_set(term, produced) for term in expression.terms)
            return False

        if len(producer_scopes) == 1:
            producer_scope = next(iter(producer_scopes))
            last_producer = max(node.rung_index for node, _value in producers)
            for clearer_node, clearer_rung in clearers:
                series_parallel = clearer_rung.sp_tree()
                if series_parallel is not None and not _fires_when_set(
                    _sp_to_expr(series_parallel)
                ):
                    continue
                if clearer_node.subroutine == producer_scope:
                    if clearer_node.rung_index > last_producer:
                        return True, rest
                    continue
                if clearer_node.subroutine is None:
                    continue
                for call_node in pdg.rung_nodes:
                    if (
                        clearer_node.subroutine in call_node.calls
                        and call_node.subroutine == producer_scope
                        and call_node.rung_index > last_producer
                    ):
                        call_rung = resolve_rung(program, call_node)
                        if call_rung is None:
                            continue
                        call_sp = call_rung.sp_tree()
                        if call_sp is not None and _fires_when_set(_sp_to_expr(call_sp)):
                            return True, rest
            continue

        producer_positions = [_main_execution_position(node) for node, _value in producers]
        if any(position is None for position in producer_positions):
            continue
        last_producer_position = max(
            position for position in producer_positions if position is not None
        )
        for clearer_node, clearer_rung in clearers:
            if clearer_node.subroutine is not None:
                continue
            series_parallel = clearer_rung.sp_tree()
            if series_parallel is not None and not _fires_when_set(_sp_to_expr(series_parallel)):
                continue
            if clearer_node.rung_index > last_producer_position:
                return True, rest
    return False, None
