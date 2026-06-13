"""Backward reachability from a snapshot — ``why()`` mode.

Given a frozen PLC state (no history), walk the program graph backward
from target tag(s) using SP-tree attribution to reconstruct how the
state was reached.  Terminates at external inputs (no ``writers_of``).

Three branches per writer rung:

- **Stateless (OTE):** rung condition IS the explanation.  ``attribute()``
  gives minimal load-bearing contacts regardless of TRUE/FALSE evaluation.
- **Stateful (latch), trigger cleared:** rung is FALSE but tag holds its
  value.  Enumerate all SP-tree leaves as candidate triggers (ambiguous).
- **Stateful (latch), still active:** rung is TRUE.  ``attribute()`` is
  definitive, same as stateless.

Reset path: for latched tags currently TRUE, also check reset rungs to
explain why the latch hasn't cleared.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.sp_tree import attribute, evaluate_sp

from .models import CausalChain, ChainStep, Transition
from .support import _collect_sp_leaves, _condition_tag_name, _HistoricalView

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph, RungNode
    from pyrung.core.condition import Condition
    from pyrung.core.program import Program
    from pyrung.core.rung import Rung
    from pyrung.core.state import SystemState


def _snapshot_value(state: SystemState, tag_name: str, inferred: dict[str, Any] | None) -> Any:
    """Return snapshot value, falling back to back-propagated inference."""
    val = state.tags.get(tag_name)
    if val is not None:
        return val
    if inferred:
        return inferred.get(tag_name)
    return None


def why_cause(
    logic: list[Rung],
    state: SystemState,
    tags: list[str],
    pdg: ProgramGraph,
    *,
    program: Program | None = None,
) -> CausalChain:
    """Build a diagnostic causal chain from a snapshot.

    Args:
        logic: The program's main rung list (kept for API compat;
            ignored when *program* is provided).
        state: Frozen PLC state (snapshot).
        tags: Tag names to explain (at least one).
        pdg: Static program dependency graph.
        program: Program for full subroutine resolution and
            inference integration (cone scoping, back-propagation,
            init-constant pinning, write-before-read skipping).

    Returns:
        A ``CausalChain`` with ``mode='why'``.
    """
    visited: set[str] = set()
    steps: list[ChainStep] = []
    conjunctive_roots: list[Transition] = []
    ambiguous_roots: list[Transition] = []

    init_constants: frozenset[str] = frozenset()
    wbr_tags: frozenset[str] = frozenset()
    inferred: dict[str, Any] = {}

    if program is not None:
        resolver = _RungResolver(program, program.rungs)

        cone: set[str] = set()
        for t in tags:
            cone |= pdg.upstream_slice(t)
            cone.add(t)

        from pyrung.core.analysis.reverse_edges import (
            back_propagate_value,
            build_reverse_edge_map,
        )

        edge_map = build_reverse_edge_map(program)
        for t in tags:
            val = state.tags.get(t)
            if val is not None:
                for src, src_val in back_propagate_value(edge_map, t, val).items():
                    if src in cone:
                        inferred.setdefault(src, src_val)

        from pyrung.core.analysis.init_constants import detect_init_constants
        from pyrung.core.validation._common import _collect_write_sites

        sites_by_target: dict[str, list[Any]] = {}
        for site in _collect_write_sites(program):
            sites_by_target.setdefault(site.target_name, []).append(site)
        projected = detect_init_constants(
            program=program,
            graph=pdg,
            sites_by_target=sites_by_target,
            candidate_tags=cone & set(pdg.writers_of.keys()),
        )
        init_constants = frozenset(projected.keys())

        wbr: set[str] = set()
        for t in cone:
            if t not in tags and pdg.unconditional_write_before_read(t):
                wbr.add(t)
        wbr_tags = frozenset(wbr)
    else:
        resolver = _RungResolver(None, list(logic))

    view = _HistoricalView(state)

    def snapshot_eval(cond: Condition) -> bool:
        return cond.evaluate(view)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    for tag_name in tags:
        _walk_backward(
            resolver,
            state,
            pdg,
            tag_name,
            snapshot_eval,
            visited,
            steps,
            conjunctive_roots,
            ambiguous_roots,
            init_constants=init_constants,
            wbr_tags=wbr_tags,
            inferred=inferred,
        )

    primary = Transition(
        tag_name=tags[0],
        scan_id=0,
        from_value=None,
        to_value=state.tags.get(tags[0]),
    )
    effects = (
        [
            Transition(
                tag_name=t,
                scan_id=0,
                from_value=None,
                to_value=state.tags.get(t),
            )
            for t in tags
        ]
        if len(tags) > 1
        else []
    )

    choice_labels: dict[str, dict[Any, str]] = {}
    for name, tag_obj in pdg.tags.items():
        choices = getattr(tag_obj, "choices", None)
        if choices:
            choice_labels[name] = dict(choices.items())

    return CausalChain(
        effect=primary,
        mode="why",
        steps=steps,
        conjunctive_roots=conjunctive_roots,
        ambiguous_roots=ambiguous_roots,
        effects=effects,
        choice_labels=choice_labels,
    )


class _RungResolver:
    """Resolve any PDG node to its ``Rung`` — main or subroutine.

    Delegates to :func:`~pyrung.core.analysis.pdg.resolve_rung` when a
    full *program* is available, with a fallback for the legacy
    ``program=None`` path (main rungs only).
    """

    __slots__ = ("_program", "_main")

    def __init__(self, program: Program | None, main_rungs: list[Rung]) -> None:
        self._program = program
        self._main = main_rungs

    def resolve(self, node: RungNode) -> Rung:
        if self._program is not None:
            result = resolve_rung(self._program, node)
            if result is not None:
                return result
            raise LookupError(f"Cannot resolve rung: {node.subroutine}[{node.rung_index}]")
        if node.subroutine is not None:
            raise LookupError(
                f"Cannot resolve subroutine rung without program: "
                f"{node.subroutine}[{node.rung_index}]"
            )
        rung = self._main[node.rung_index]
        for branch_idx in node.branch_path:
            rung = rung._branches[branch_idx]
        return rung


_INSTRUCTION_LABELS: dict[str, str] = {
    "OutInstruction": "out",
    "LatchInstruction": "latch",
    "ResetInstruction": "reset",
    "CopyInstruction": "copy",
    "CalcInstruction": "calc",
    "OnDelayInstruction": "on_delay",
    "OffDelayInstruction": "off_delay",
    "CountUpInstruction": "count_up",
    "CountDownInstruction": "count_down",
    "EventDrumInstruction": "event_drum",
    "TimeDrumInstruction": "time_drum",
    "FillInstruction": "fill",
    "BlockCopyInstruction": "blockcopy",
    "PackBitsInstruction": "pack_bits",
    "PackTextInstruction": "pack_text",
    "UnpackToBitsInstruction": "unpack_to_bits",
    "PackWordsInstruction": "pack_words",
    "UnpackToWordsInstruction": "unpack_to_words",
    "SearchInstruction": "search",
    "ShiftInstruction": "shift",
    "FunctionCallInstruction": "run_function",
    "EnabledFunctionCallInstruction": "run_enabled_function",
    "ModbusSendInstruction": "send",
    "ModbusReceiveInstruction": "receive",
    "ForLoopInstruction": "forloop",
}


def _instruction_label(rung: Rung, tag_name: str) -> str:
    """Derive the instruction name that writes *tag_name* in *rung*."""
    from pyrung.core.analysis.pdg import _extract_instruction_writes
    from pyrung.core.instruction.control import ForLoopInstruction

    def label_for(instr: Any) -> str:
        cls_name = type(instr).__name__
        base = cls_name.replace("Oneshot", "")
        return _INSTRUCTION_LABELS.get(cls_name, _INSTRUCTION_LABELS.get(base, cls_name))

    def walk(instructions: list[Any]) -> str | None:
        tag_refs: dict[str, Any] = {}
        for instr in instructions:
            writes, _reads = _extract_instruction_writes(instr, tag_refs)
            if tag_name in writes:
                return label_for(instr)
            if isinstance(instr, ForLoopInstruction):
                child_label = walk(list(instr.instructions))
                if child_label is not None:
                    return child_label
        return None

    found = walk(list(rung._instructions))
    if found is not None:
        return found
    return "write"


def _is_reset_for_tag(rung: Rung, tag_name: str) -> bool:
    """True if *rung* contains a ``ResetInstruction`` targeting *tag_name*."""
    from pyrung.core.instruction.coils import ResetInstruction
    from pyrung.core.tag import ImmediateRef
    from pyrung.core.tag import Tag as TagClass

    for instr in rung._instructions:
        if not isinstance(instr, ResetInstruction):
            continue
        target = instr.target
        if isinstance(target, ImmediateRef):
            target = object.__getattribute__(target, "value")
        if isinstance(target, TagClass) and target.name == tag_name:
            return True
    return False


def _walk_backward(
    resolver: _RungResolver,
    state: SystemState,
    pdg: ProgramGraph,
    tag_name: str,
    snapshot_eval: Callable[[Condition], bool],
    visited: set[str],
    steps: list[ChainStep],
    conjunctive_roots: list[Transition],
    ambiguous_roots: list[Transition],
    *,
    init_constants: frozenset[str] = frozenset(),
    wbr_tags: frozenset[str] = frozenset(),
    inferred: dict[str, Any] | None = None,
) -> None:
    if tag_name in wbr_tags:
        return

    writer_indices = pdg.writers_of.get(tag_name, frozenset())

    if not writer_indices or tag_name in init_constants:
        conjunctive_roots.append(
            Transition(
                tag_name=tag_name,
                scan_id=0,
                from_value=None,
                to_value=_snapshot_value(state, tag_name, inferred),
            )
        )
        return

    if tag_name in visited:
        return
    visited.add(tag_name)

    tag_value = _snapshot_value(state, tag_name, inferred)

    set_writers: list[int] = []
    reset_writers: list[int] = []
    for node_idx in writer_indices:
        node = pdg.rung_nodes[node_idx]
        rung = resolver.resolve(node)
        if _is_reset_for_tag(rung, tag_name):
            reset_writers.append(node_idx)
        else:
            set_writers.append(node_idx)

    for node_idx in set_writers:
        node = pdg.rung_nodes[node_idx]
        rung = resolver.resolve(node)
        sp_tree = rung.sp_tree()
        is_ote = tag_name in node.ote_writes
        instr_label = _instruction_label(rung, tag_name)

        if sp_tree is None:
            continue

        rung_fires = evaluate_sp(sp_tree, snapshot_eval)

        if is_ote:
            is_transient = tag_value is not None and bool(tag_value) != rung_fires
        else:
            is_transient = rung_fires and tag_value is not None and not tag_value

        if not is_ote and not rung_fires and tag_value:
            _walk_stateful_cleared(
                resolver,
                state,
                pdg,
                tag_name,
                tag_value,
                node,
                sp_tree,
                snapshot_eval,
                visited,
                steps,
                conjunctive_roots,
                ambiguous_roots,
                init_constants=init_constants,
                wbr_tags=wbr_tags,
                inferred=inferred,
                instruction=instr_label,
            )
        elif not is_ote and not rung_fires and not tag_value:
            _walk_attributed(
                resolver,
                state,
                pdg,
                tag_name,
                tag_value,
                node,
                sp_tree,
                snapshot_eval,
                visited,
                steps,
                conjunctive_roots,
                ambiguous_roots,
                init_constants=init_constants,
                wbr_tags=wbr_tags,
                inferred=inferred,
                kind="latch_blocked",
                instruction=instr_label,
            )
        else:
            kind = "transient" if is_transient else "attributed"
            _walk_attributed(
                resolver,
                state,
                pdg,
                tag_name,
                tag_value,
                node,
                sp_tree,
                snapshot_eval,
                visited,
                steps,
                conjunctive_roots,
                ambiguous_roots,
                init_constants=init_constants,
                wbr_tags=wbr_tags,
                inferred=inferred,
                kind=kind,
                instruction=instr_label,
            )

    if reset_writers:
        if tag_value:
            _walk_reset_path(
                resolver,
                state,
                pdg,
                tag_name,
                tag_value,
                reset_writers,
                snapshot_eval,
                steps,
            )
        else:
            _walk_reset_cause(
                resolver,
                state,
                pdg,
                tag_name,
                reset_writers,
                snapshot_eval,
                visited,
                steps,
                conjunctive_roots,
                ambiguous_roots,
                init_constants=init_constants,
                wbr_tags=wbr_tags,
                inferred=inferred,
            )


def _walk_stateful_cleared(
    resolver: _RungResolver,
    state: SystemState,
    pdg: ProgramGraph,
    tag_name: str,
    tag_value: Any,
    node: RungNode,
    sp_tree: Any,
    snapshot_eval: Callable[[Condition], bool],
    visited: set[str],
    steps: list[ChainStep],
    conjunctive_roots: list[Transition],
    ambiguous_roots: list[Transition],
    *,
    init_constants: frozenset[str] = frozenset(),
    wbr_tags: frozenset[str] = frozenset(),
    inferred: dict[str, Any] | None = None,
    instruction: str | None = None,
) -> None:
    """Handle stateful writer whose trigger has cleared."""
    leaves = _collect_sp_leaves(sp_tree)
    triggers: list[Transition] = []

    for leaf in leaves:
        contact_tag = _condition_tag_name(leaf.condition)
        if contact_tag is None:
            continue
        contact_transition = Transition(
            tag_name=contact_tag,
            scan_id=0,
            from_value=None,
            to_value=_snapshot_value(state, contact_tag, inferred),
        )
        triggers.append(contact_transition)
        if not pdg.writers_of.get(contact_tag, frozenset()):
            ambiguous_roots.append(contact_transition)
        else:
            _walk_backward(
                resolver,
                state,
                pdg,
                contact_tag,
                snapshot_eval,
                visited,
                steps,
                conjunctive_roots,
                ambiguous_roots,
                init_constants=init_constants,
                wbr_tags=wbr_tags,
                inferred=inferred,
            )

    steps.append(
        ChainStep(
            transition=Transition(
                tag_name=tag_name,
                scan_id=0,
                from_value=None,
                to_value=tag_value,
            ),
            rung_index=node.rung_index,
            triggers=tuple(triggers),
            enablers=(),
            fidelity="structural",
            kind="trigger_cleared",
            instruction=instruction,
            subroutine=node.subroutine,
        )
    )


def _walk_attributed(
    resolver: _RungResolver,
    state: SystemState,
    pdg: ProgramGraph,
    tag_name: str,
    tag_value: Any,
    node: RungNode,
    sp_tree: Any,
    snapshot_eval: Callable[[Condition], bool],
    visited: set[str],
    steps: list[ChainStep],
    conjunctive_roots: list[Transition],
    ambiguous_roots: list[Transition],
    *,
    init_constants: frozenset[str] = frozenset(),
    wbr_tags: frozenset[str] = frozenset(),
    inferred: dict[str, Any] | None = None,
    kind: Literal[
        "attributed",
        "trigger_cleared",
        "latch_blocked",
        "reset_blocked",
        "reset_active",
        "reset_inconsistent",
        "transient",
    ] = "attributed",
    instruction: str | None = None,
) -> None:
    """Handle stateless writer or active stateful writer via attribution."""
    attributions = attribute(sp_tree, snapshot_eval)
    triggers: list[Transition] = []

    for attr in attributions:
        contact_tag = _condition_tag_name(attr.condition)
        if contact_tag is None:
            continue
        contact_transition = Transition(
            tag_name=contact_tag,
            scan_id=0,
            from_value=None,
            to_value=_snapshot_value(state, contact_tag, inferred),
        )
        triggers.append(contact_transition)
        if not pdg.writers_of.get(contact_tag, frozenset()):
            conjunctive_roots.append(contact_transition)
        else:
            _walk_backward(
                resolver,
                state,
                pdg,
                contact_tag,
                snapshot_eval,
                visited,
                steps,
                conjunctive_roots,
                ambiguous_roots,
                init_constants=init_constants,
                wbr_tags=wbr_tags,
                inferred=inferred,
            )

    steps.append(
        ChainStep(
            transition=Transition(
                tag_name=tag_name,
                scan_id=0,
                from_value=None,
                to_value=tag_value,
            ),
            rung_index=node.rung_index,
            triggers=tuple(triggers),
            enablers=(),
            fidelity="structural",
            kind=kind,
            instruction=instruction,
            subroutine=node.subroutine,
        )
    )


def _walk_reset_path(
    resolver: _RungResolver,
    state: SystemState,
    pdg: ProgramGraph,
    tag_name: str,
    tag_value: Any,
    reset_writer_indices: list[int],
    snapshot_eval: Callable[[Condition], bool],
    steps: list[ChainStep],
) -> None:
    """Explain why reset rungs haven't cleared a latched tag."""
    for node_idx in reset_writer_indices:
        node = pdg.rung_nodes[node_idx]
        rung = resolver.resolve(node)
        sp_tree = rung.sp_tree()

        if sp_tree is None:
            continue

        rung_fires = evaluate_sp(sp_tree, snapshot_eval)
        transition = Transition(
            tag_name=tag_name,
            scan_id=0,
            from_value=None,
            to_value=tag_value,
        )

        if rung_fires:
            steps.append(
                ChainStep(
                    transition=transition,
                    rung_index=node.rung_index,
                    triggers=(),
                    enablers=(),
                    fidelity="structural",
                    kind="reset_inconsistent",
                    instruction="reset",
                    subroutine=node.subroutine,
                )
            )
        else:
            attributions = attribute(sp_tree, snapshot_eval)
            reset_blockers: list[Transition] = []
            for attr in attributions:
                contact_tag = _condition_tag_name(attr.condition)
                if contact_tag is not None:
                    reset_blockers.append(
                        Transition(
                            tag_name=contact_tag,
                            scan_id=0,
                            from_value=None,
                            to_value=state.tags.get(contact_tag),
                        )
                    )
            steps.append(
                ChainStep(
                    transition=transition,
                    rung_index=node.rung_index,
                    triggers=tuple(reset_blockers),
                    enablers=(),
                    fidelity="structural",
                    kind="reset_blocked",
                    instruction="reset",
                    subroutine=node.subroutine,
                )
            )


def _walk_reset_cause(
    resolver: _RungResolver,
    state: SystemState,
    pdg: ProgramGraph,
    tag_name: str,
    reset_writer_indices: list[int],
    snapshot_eval: Callable[[Condition], bool],
    visited: set[str],
    steps: list[ChainStep],
    conjunctive_roots: list[Transition],
    ambiguous_roots: list[Transition],
    *,
    init_constants: frozenset[str] = frozenset(),
    wbr_tags: frozenset[str] = frozenset(),
    inferred: dict[str, Any] | None = None,
) -> None:
    """Explain why a latch tag is FALSE by finding active reset rungs."""
    for node_idx in reset_writer_indices:
        node = pdg.rung_nodes[node_idx]
        rung = resolver.resolve(node)
        sp_tree = rung.sp_tree()

        if sp_tree is None:
            continue

        rung_fires = evaluate_sp(sp_tree, snapshot_eval)
        if not rung_fires:
            continue

        attributions = attribute(sp_tree, snapshot_eval)
        triggers: list[Transition] = []
        for attr in attributions:
            contact_tag = _condition_tag_name(attr.condition)
            if contact_tag is None:
                continue
            contact_transition = Transition(
                tag_name=contact_tag,
                scan_id=0,
                from_value=None,
                to_value=_snapshot_value(state, contact_tag, inferred),
            )
            triggers.append(contact_transition)
            if not pdg.writers_of.get(contact_tag, frozenset()):
                conjunctive_roots.append(contact_transition)
            else:
                _walk_backward(
                    resolver,
                    state,
                    pdg,
                    contact_tag,
                    snapshot_eval,
                    visited,
                    steps,
                    conjunctive_roots,
                    ambiguous_roots,
                    init_constants=init_constants,
                    wbr_tags=wbr_tags,
                    inferred=inferred,
                )

        steps.append(
            ChainStep(
                transition=Transition(
                    tag_name=tag_name,
                    scan_id=0,
                    from_value=None,
                    to_value=False,
                ),
                rung_index=node.rung_index,
                triggers=tuple(triggers),
                enablers=(),
                fidelity="structural",
                kind="reset_active",
                instruction="reset",
                subroutine=node.subroutine,
            )
        )
