"""Layer 6: Don't Rediscover — observed action/transition mapping.

Detects ``copy(block[ptr], tag)`` patterns statically to seed a steerable
action space, then records transitions observed during fork probes.  The
transition table is generic: an action is a ``(tag, value)`` pair, not a
special command or Bool input.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeGuard

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph

logger = logging.getLogger(__name__)

Action = tuple[str, Any]


@dataclass(frozen=True)
class WaitCause:
    """A transition caused by time passing rather than an explicit tag write."""

    def __repr__(self) -> str:
        return "WAIT"


WAIT = WaitCause()
TransitionCause = Action | WaitCause


def is_action(cause: TransitionCause) -> TypeGuard[Action]:
    return isinstance(cause, tuple)


def _is_declared_mutable_tag(tag: object, pdg: ProgramGraph) -> bool:
    """Filter only tags that the program explicitly marks as immutable."""
    tag_ref = pdg.tags.get(tag) if isinstance(tag, str) else None
    return tag_ref is not None and not tag_ref.readonly


@dataclass(frozen=True)
class PipelineSlice:
    """Steerable tags that may participate in an opaque transition.

    The slice does not choose values.  PILOT turns these tags into concrete
    actions using the current snapshot, known domains, and trace-derived needs.
    """

    action_tags: frozenset[str]


class InfluenceMap:
    """Per-register transition table built from fork-probe observations.

    Seeded at startup with statically-detected opaque pipelines so PILOT
    can go straight to systematic exploration without a first observation.
    """

    def __init__(self, slices: list[PipelineSlice] | None = None) -> None:
        self._slices: list[PipelineSlice] = list(slices or [])
        self._action_tags: frozenset[str] = (
            frozenset().union(*(s.action_tags for s in self._slices))
            if self._slices
            else frozenset()
        )
        self._transitions: dict[str, dict[tuple[Any, TransitionCause], Any]] = {}
        self._probed: dict[str, set[tuple[Any, TransitionCause]]] = {}

    @property
    def action_tags(self) -> frozenset[str]:
        return self._action_tags

    def has_transitions(self, tag: str) -> bool:
        return tag in self._transitions

    def record(
        self,
        tag: str,
        cause: TransitionCause,
        from_val: Any,
        to_val: Any,
    ) -> None:
        table = self._transitions.setdefault(tag, {})
        table[(from_val, cause)] = to_val
        self._probed.setdefault(tag, set()).add((from_val, cause))

    def record_no_change(self, tag: str, cause: TransitionCause, from_val: Any) -> None:
        self._probed.setdefault(tag, set()).add((from_val, cause))

    def find_path(
        self,
        tag: str,
        from_val: Any,
        to_val: Any,
    ) -> list[TransitionCause] | None:
        """BFS shortest transition-cause sequence through the table."""
        from pyrung.core.analysis.sp_values import _values_match

        table = self._transitions.get(tag)
        if not table:
            return None
        if _values_match(from_val, to_val):
            return []

        queue: deque[tuple[Any, list[TransitionCause]]] = deque([(from_val, [])])
        visited: set[Any] = {from_val}

        while queue:
            state, path = queue.popleft()
            for (s, cause), dest in table.items():
                if not _values_match(s, state):
                    continue
                if dest in visited:
                    continue
                new_path = [*path, cause]
                if _values_match(dest, to_val):
                    return new_path
                visited.add(dest)
                queue.append((dest, new_path))

        return None

    def unprobed_actions(
        self,
        tag: str,
        from_val: Any,
        available_actions: set[Action] | frozenset[Action],
    ) -> list[Action]:
        """Available actions not yet tried from *from_val* for *tag*."""
        return sorted(available_actions - self.probed_actions(tag, from_val))

    def probed_actions(self, tag: str, from_val: Any) -> set[Action]:
        """Actions already probed from *from_val* for *tag*."""
        return {
            cause
            for (fv, cause) in self._probed.get(tag, set())
            if fv == from_val and is_action(cause)
        }

    def transition_dest(
        self,
        tag: str,
        from_val: Any,
        cause: TransitionCause,
    ) -> Any | None:
        """Observed destination for one transition cause from *from_val*."""
        from pyrung.core.analysis.sp_values import _values_match

        for (fv, candidate_cause), dest in self._transitions.get(tag, {}).items():
            if candidate_cause == cause and _values_match(fv, from_val):
                return dest
        return None

    def off_path_actions(self, tag: str, from_val: Any, to_val: Any) -> set[Action]:
        """Actions known to move *tag* away from the BFS path toward *to_val*.

        Once we know the shortest path, any action from the current state
        that goes to a state NOT on that path (or with no path to the
        target) is off-path and should be tried after path actions.
        """
        from pyrung.core.analysis.sp_values import _values_match

        path = self.find_path(tag, from_val, to_val)
        if not path:
            return set()
        good_cause = path[0]
        table = self._transitions.get(tag, {})

        # Compute states on the BFS path
        on_path: set[Any] = {from_val}
        state = from_val
        for cause in path:
            dest = table.get((state, cause))
            if dest is not None:
                on_path.add(dest)
                state = dest

        off_path: set[Action] = set()
        for (fv, cause), dest in table.items():
            if not _values_match(fv, from_val):
                continue
            if cause == good_cause or not is_action(cause):
                continue
            if dest not in on_path:
                off_path.add(cause)
        return off_path


def _find_convergent_steers(
    opaque_tag: str,
    pdg: ProgramGraph,
    steerable: frozenset[str],
    *,
    max_hops: int = 8,
    min_writers: int = 2,
) -> frozenset[str]:
    """Bounded upstream BFS to find convergence-point steerable inputs.

    A convergence point is an intermediate tag written by multiple rungs
    where each writer is conditioned on a different steerable input
    (e.g. ``C_CtrlCmd`` written by 10 rungs, each gated by a different
    command button).  Returns the union of those steerable condition reads.

    Falls back to the full ``upstream_slice & steerable`` if no
    convergence point is found within *max_hops*.
    """
    visited_tags: set[str] = set()
    visited_rungs: set[int] = set()
    queue: list[tuple[str, int]] = [(opaque_tag, 0)]
    convergent: set[str] = set()

    while queue:
        tag, depth = queue.pop(0)
        if tag in visited_tags or depth > max_hops:
            continue
        visited_tags.add(tag)
        tag_steer_conds: set[str] = set()
        for ri in pdg.writers_of.get(tag, frozenset()):
            if ri in visited_rungs:
                continue
            visited_rungs.add(ri)
            node = pdg.rung_nodes[ri]
            tag_steer_conds |= node.condition_reads & steerable
            for rt in node.condition_reads | node.data_reads:
                if rt not in visited_tags:
                    queue.append((rt, depth + 1))
        if len(tag_steer_conds) >= min_writers:
            convergent |= tag_steer_conds

    if convergent:
        return frozenset(convergent)
    return pdg.upstream_slice(opaque_tag) & steerable


def _scan_indirect_copy_targets(program: Any) -> set[str]:
    """Destination tag names of ``copy(block[ptr], tag)`` indirect copies."""
    from pyrung.core.instruction.data_transfer import CopyInstruction
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef

    targets: set[str] = set()

    def _scan(rungs: Any) -> None:
        for r in rungs:
            for instr in getattr(r, "_instructions", ()):
                if isinstance(instr, CopyInstruction) and isinstance(
                    instr.source, (IndirectRef, IndirectExprRef)
                ):
                    dest_name = getattr(instr.dest, "name", None)
                    if dest_name:
                        targets.add(dest_name)
            _scan(getattr(r, "_branches", ()))

    _scan(program.rungs)
    for sub_rungs in getattr(program, "subroutines", {}).values():
        _scan(sub_rungs)
    return targets


def detect_opaque_loop(
    pdg: ProgramGraph,
    program: Any,
    *,
    max_hops: int = 3,
) -> frozenset[str]:
    """Tags in a feedback loop through an opaque (indirect-copy) pipeline.

    These are the jump-table state-machine registers (``S_StateCurrent``,
    ``isStateEnbl_Yes``, ``S_StateRequested``, the ``S_<state>`` flags,
    ``C_CtrlCmd`` …) that mutually drive each other through the indirect-copy
    machinery.  ``trace_back`` must not invert them as a finite prerequisite
    chain — it walks the entire state-transition graph backward (e.g.
    ``StateCurrent=6 → enable → StateRequested=2 → Stopping → StateCurrent=7
    → …``), scrambling depth and inflating the unsatisfied count.  They are
    Layer 6 territory: learned by observation, not static inversion.

    A tag qualifies when it is BOTH within *max_hops* downstream of an
    indirect-copy target AND upstream of one — i.e. it participates in the
    loop.  Simple state machines built from direct copies have no
    indirect-copy targets, so this returns empty and ``trace_back`` is
    unaffected.
    """
    targets = _scan_indirect_copy_targets(program)
    if not targets:
        return frozenset()

    # Bounded downstream BFS: tag -> rungs reading it -> their written tags.
    seen: set[str] = set(targets)
    frontier: set[str] = set(targets)
    for _ in range(max_hops):
        nxt: set[str] = set()
        for tag in frontier:
            for ri in pdg.readers_of.get(tag, frozenset()):
                for w in pdg.rung_nodes[ri].all_writes:
                    if w not in seen:
                        seen.add(w)
                        nxt.add(w)
        frontier = nxt

    upstream: set[str] = set()
    for t in targets:
        upstream |= pdg.upstream_slice(t)

    return frozenset(seen & upstream)


def detect_opaque_pipelines(
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
) -> list[PipelineSlice]:
    """Find indirect-copy write targets and their steerable upstream tags.

    Scans the program for ``CopyInstruction`` with ``IndirectRef`` or
    ``IndirectExprRef`` sources (the ``copy(block[ptr], tag)`` pattern).
    For each, follows downstream via the PDG to find affected output tags,
    and uses convergence-point detection to find the steerable inputs that
    actually enter the pipeline (not the full upstream cone).

    Deduplicates slices that share the same free args (e.g. multiple
    indirect copies in the same subroutine).
    """
    opaque_targets = _scan_indirect_copy_targets(program)
    if not opaque_targets:
        return []

    # Deduplicate: multiple opaque targets may share convergent steers.
    seen_tags: set[frozenset[str]] = set()
    slices: list[PipelineSlice] = []
    for opaque_tag in sorted(opaque_targets):
        action_tags = frozenset(
            tag
            for tag in _find_convergent_steers(opaque_tag, pdg, steerable)
            if _is_declared_mutable_tag(tag, pdg)
        )
        if not action_tags or action_tags in seen_tags:
            continue
        seen_tags.add(action_tags)
        slices.append(PipelineSlice(action_tags=frozenset(action_tags)))
        logger.info(
            "pilot: opaque pipeline (%s) -> %d action tags: %s",
            opaque_tag,
            len(action_tags),
            sorted(action_tags),
        )

    return slices
