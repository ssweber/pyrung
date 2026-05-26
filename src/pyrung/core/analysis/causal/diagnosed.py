"""Backward reachability from a snapshot — ``diagnose()`` mode.

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

from typing import TYPE_CHECKING, Any, Callable

from pyrung.core.analysis.sp_tree import attribute, evaluate_sp

from .models import CausalChain, ChainStep, Transition
from .support import _collect_sp_leaves, _condition_tag_name, _HistoricalView

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph, RungNode
    from pyrung.core.condition import Condition
    from pyrung.core.rung import Rung
    from pyrung.core.state import SystemState


def diagnosed_cause(
    logic: list[Rung],
    state: SystemState,
    tags: list[str],
    pdg: ProgramGraph,
) -> CausalChain:
    """Build a diagnostic causal chain from a snapshot.

    Args:
        logic: The program's rung list.
        state: Frozen PLC state (snapshot).
        tags: Tag names to diagnose (at least one).
        pdg: Static program dependency graph.

    Returns:
        A ``CausalChain`` with ``mode='diagnosed'``.
    """
    visited: set[str] = set()
    steps: list[ChainStep] = []
    conjunctive_roots: list[Transition] = []
    ambiguous_roots: list[Transition] = []

    view = _HistoricalView(state)

    def snapshot_eval(cond: Condition) -> bool:
        return cond.evaluate(view)  # type: ignore[arg-type]

    for tag_name in tags:
        _walk_backward(
            logic,
            state,
            pdg,
            tag_name,
            snapshot_eval,
            visited,
            steps,
            conjunctive_roots,
            ambiguous_roots,
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

    return CausalChain(
        effect=primary,
        mode="diagnosed",
        steps=steps,
        conjunctive_roots=conjunctive_roots,
        ambiguous_roots=ambiguous_roots,
        effects=effects,
    )


def _resolve_rung(logic: list[Rung], node: RungNode) -> Rung:
    """Navigate from a PDG node to the actual ``Rung`` object."""
    rung = logic[node.rung_index]
    for branch_idx in node.branch_path:
        rung = rung._branches[branch_idx]
    return rung


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
    logic: list[Rung],
    state: SystemState,
    pdg: ProgramGraph,
    tag_name: str,
    snapshot_eval: Callable[[Condition], bool],
    visited: set[str],
    steps: list[ChainStep],
    conjunctive_roots: list[Transition],
    ambiguous_roots: list[Transition],
) -> None:
    writer_indices = pdg.writers_of.get(tag_name, frozenset())

    if not writer_indices:
        conjunctive_roots.append(
            Transition(
                tag_name=tag_name,
                scan_id=0,
                from_value=None,
                to_value=state.tags.get(tag_name),
            )
        )
        return

    if tag_name in visited:
        return
    visited.add(tag_name)

    tag_value = state.tags.get(tag_name)

    set_writers: list[int] = []
    reset_writers: list[int] = []
    for node_idx in writer_indices:
        node = pdg.rung_nodes[node_idx]
        if node.rung_index >= len(logic):
            continue
        rung = _resolve_rung(logic, node)
        if _is_reset_for_tag(rung, tag_name):
            reset_writers.append(node_idx)
        else:
            set_writers.append(node_idx)

    for node_idx in set_writers:
        node = pdg.rung_nodes[node_idx]
        rung = _resolve_rung(logic, node)
        sp_tree = rung.sp_tree()
        is_ote = tag_name in node.ote_writes

        if sp_tree is None:
            continue

        rung_fires = evaluate_sp(sp_tree, snapshot_eval)

        if not is_ote and not rung_fires:
            _walk_stateful_cleared(
                logic,
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
            )
        else:
            _walk_attributed(
                logic,
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
            )

    if tag_value and reset_writers:
        _walk_reset_path(
            logic,
            state,
            pdg,
            tag_name,
            tag_value,
            reset_writers,
            snapshot_eval,
            steps,
        )


def _walk_stateful_cleared(
    logic: list[Rung],
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
            to_value=state.tags.get(contact_tag),
        )
        triggers.append(contact_transition)
        if not pdg.writers_of.get(contact_tag, frozenset()):
            ambiguous_roots.append(contact_transition)
        else:
            _walk_backward(
                logic,
                state,
                pdg,
                contact_tag,
                snapshot_eval,
                visited,
                steps,
                conjunctive_roots,
                ambiguous_roots,
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
        )
    )


def _walk_attributed(
    logic: list[Rung],
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
            to_value=state.tags.get(contact_tag),
        )
        triggers.append(contact_transition)
        if not pdg.writers_of.get(contact_tag, frozenset()):
            conjunctive_roots.append(contact_transition)
        else:
            _walk_backward(
                logic,
                state,
                pdg,
                contact_tag,
                snapshot_eval,
                visited,
                steps,
                conjunctive_roots,
                ambiguous_roots,
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
        )
    )


def _walk_reset_path(
    logic: list[Rung],
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
        rung = _resolve_rung(logic, node)
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
                )
            )
