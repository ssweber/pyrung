"""Reachability path types and semantic constraint rendering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable as _Callable

    from pyrung.core.analysis.simplified import Atom

    StateKey = tuple[Any, ...]


_COMPARISON_FORMS = frozenset({"eq", "ne", "lt", "le", "gt", "ge"})


def _enrich_atom_index(
    atom_index: dict[str, list[Atom]],
    reverse_edge_map: dict[str, list[tuple[str, _Callable[[Any], Any]]]],
) -> dict[str, list[Atom]]:
    """Propagate comparison atoms backward through copy/calc chains.

    Given ``copy(Source, Target)`` and ``Target > 50``, adds an effective
    atom ``Source > 50`` so the path renderer can display constraints in
    terms of the input the user controls, not an intermediate variable.

    Tag-vs-tag operands only propagate through identity transforms (copy);
    literal operands are transformed via the inverse function (calc).
    """
    from pyrung.core.analysis.reverse_edges import IDENTITY, compose_invert
    from pyrung.core.analysis.simplified import Atom

    target_to_sources: dict[str, list[tuple[str, _Callable[[Any], Any]]]] = {}
    for source, edges in reverse_edge_map.items():
        for target, invert in edges:
            target_to_sources.setdefault(target, []).append((source, invert))

    if not target_to_sources:
        return atom_index

    enriched: dict[str, list[Atom]] = {tag: list(atoms) for tag, atoms in atom_index.items()}
    existing_keys: dict[str, set[tuple[str, str, Any]]] = {
        tag: {a._key() for a in atoms} for tag, atoms in enriched.items()
    }

    for tag, atoms in atom_index.items():
        for atom in atoms:
            if atom.form not in _COMPARISON_FORMS or atom.tag != tag:
                continue

            queue: list[tuple[str, _Callable[[Any], Any]]] = list(target_to_sources.get(tag, []))
            visited: set[str] = {tag}

            while queue:
                source, composed_invert = queue.pop(0)
                if source in visited:
                    continue
                visited.add(source)

                if isinstance(atom.operand, str):
                    if composed_invert is not IDENTITY:
                        continue
                    new_atom = Atom(tag=source, form=atom.form, operand=atom.operand)
                else:
                    new_threshold = composed_invert(atom.operand)
                    if new_threshold is None or not isinstance(new_threshold, (int, float)):
                        continue
                    new_atom = Atom(tag=source, form=atom.form, operand=new_threshold)

                key = new_atom._key()
                if key not in existing_keys.get(source, set()):
                    enriched.setdefault(source, []).append(new_atom)
                    existing_keys.setdefault(source, set()).add(key)
                    if isinstance(new_atom.operand, str):
                        enriched.setdefault(new_atom.operand, []).append(new_atom)
                        existing_keys.setdefault(new_atom.operand, set()).add(key)

                for next_src, next_inv in target_to_sources.get(source, []):
                    if next_src not in visited:
                        queue.append((next_src, compose_invert(next_inv, composed_invert)))

    return enriched


def _enrich_from_relational_calcs(
    atom_index: dict[str, list[Atom]],
    program: Any,
) -> dict[str, list]:
    """Synthesize relational atoms from two-tag arithmetic calc expressions.

    Given ``calc(A - B, C)`` and ``C > 0`` in the atom index, adds
    ``Atom(tag="A", form="gt", operand="B")`` so the path shows ``A > B``.
    For non-zero thresholds or non-subtraction operators, produces an
    ``ArithAtom`` instead (e.g. ``A - B > 5``, ``A + B > 100``).
    """
    from pyrung.core.analysis.reverse_edges import tag_name_from_value
    from pyrung.core.analysis.simplified import ArithAtom, Atom
    from pyrung.core.expression import BinaryExpr
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.validation._common import walk_instructions

    enriched: dict[str, list[Atom | ArithAtom]] = {
        tag: list(atoms) for tag, atoms in atom_index.items()
    }
    existing_keys: dict[str, set[tuple]] = {
        tag: {a._key() for a in atoms} for tag, atoms in enriched.items()
    }

    def _add(tag_name: str, new_atom: Atom | ArithAtom) -> None:
        key = new_atom._key()
        if key not in existing_keys.get(tag_name, set()):
            enriched.setdefault(tag_name, []).append(new_atom)
            existing_keys.setdefault(tag_name, set()).add(key)

    for instr in walk_instructions(program):
        if not isinstance(instr, CalcInstruction):
            continue
        expr = instr.expression
        if not isinstance(expr, BinaryExpr) or expr.symbol not in ("+", "-", "*"):
            continue
        left_name = tag_name_from_value(expr.left)
        right_name = tag_name_from_value(expr.right)
        if left_name is None or right_name is None:
            continue
        target_name = tag_name_from_value(instr.dest)
        if target_name is None:
            continue

        for atom in atom_index.get(target_name, []):
            if atom.form not in _COMPARISON_FORMS:
                continue
            if not isinstance(atom.operand, (int, float)):
                continue

            if expr.symbol == "-" and atom.operand == 0:
                new_atom = Atom(tag=left_name, form=atom.form, operand=right_name)
                _add(left_name, new_atom)
                _add(right_name, new_atom)
            else:
                new_atom = ArithAtom(
                    left=left_name,
                    arith_op=expr.symbol,
                    right=right_name,
                    form=atom.form,
                    operand=atom.operand,
                )
                _add(left_name, new_atom)
                _add(right_name, new_atom)

    return enriched


@dataclass(frozen=True)
class PlanStep:
    """One move in the PILOT drive journal — what was done and why.

    ``kind`` classifies the move so the repr can group/filter:
    - ``"command"`` — a candidate pulse (button press).
    - ``"coast"``   — a zoom or let-run dwell (waiting for timers/sequences).
    - ``"accelerator"`` — a timer/counter accumulator patch (proved-safe skip).
    """

    kind: str
    scan: int
    scans: int
    inputs: tuple[tuple[str, Any], ...]
    label: str


def _format_value(value: Any) -> str:
    """Format a tag value for use in a console command."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    s = str(value)
    if " " in s:
        return f'"{s}"'
    return s


@dataclass(frozen=True)
class RouteAlt:
    """A road not taken at a pivot — what ``via=`` would switch to.

    ``via_hint`` is the concrete ``(tag, value)`` the engineer names to redirect
    onto this alternative (``via=(MaintMode, True)`` / the bare tag ``MaintMode``).
    """

    label: str
    via_hint: tuple[str, Any] | None = None


@dataclass(frozen=True)
class RoutePivot:
    """A redirectable decision the chosen route committed to.

    PILOT picked ``(tag, value)`` where ≥1 other viable option existed.  The
    engineer steers away with ``avoid=<via_hint>`` or toward an alternative with
    ``via=<that alt's via_hint>``.  ``via_hint`` is the bridge from the human
    label to the ``avoid=``/``via=`` predicate: the concrete condition the
    redirect names (the committed writer's gating coil, or a representative
    steerable leaf of an OR arm).  ``salient`` is False for trivial cost-0 forks
    (``Or(Auto, Manual)``) — still redirectable, but hidden from the headline.
    """

    tag: str
    value: Any
    label: str
    kind: str  # "writer" | "or-arm"
    via_hint: tuple[str, Any] | None = None
    alternatives: tuple[RouteAlt, ...] = ()
    salient: bool = True


@dataclass(frozen=True)
class RouteTaken:
    """How PILOT reached a Bool target — the legible "here's where I went".

    ``how()`` never reports ambiguous: it picks a deterministic default route
    (cheapest by trace score, rung order breaking ties), reaches the goal, and
    records the route here so the engineer can redirect with ``avoid=``/``via=``.
    ``dominant`` is True when the default was the unique cheapest (no real fork).
    """

    label: str
    pivots: tuple[RoutePivot, ...] = ()
    dominant: bool = True

    @property
    def salient_pivots(self) -> tuple[RoutePivot, ...]:
        return tuple(p for p in self.pivots if p.salient)


def _format_hint(hint: tuple[str, Any] | None) -> str:
    """Render a ``(tag, value)`` redirect hint as the engineer would type it."""
    if hint is None:
        return ""
    tag, value = hint
    return tag if value is True else f"{tag}=={_format_value(value)}"


def _render_pivot_redirect(pivot: RoutePivot) -> str:
    """One-line ``avoid=``/``via=`` hint for redirecting off a salient pivot."""
    bits: list[str] = []
    avoid_expr = _format_hint(pivot.via_hint)
    if avoid_expr:
        bits.append(f"avoid={avoid_expr}")
    for alt in pivot.alternatives:
        via_expr = _format_hint(alt.via_hint)
        if via_expr:
            bits.append(f"via={via_expr}")
    return "redirect: " + " | ".join(bits) if bits else ""


def _format_plan_step(step: PlanStep) -> str:
    if step.kind == "coast":
        return f"  coast {step.label} ({step.scans} scans)"
    if step.kind == "accelerator":
        tags = ", ".join(f"{t}={v}" for t, v in step.inputs)
        return f"  skip  {tags}"
    inputs = ", ".join(f"{t}={_format_value(v)}" for t, v in step.inputs)
    suffix = f"  ({step.label})" if step.label != inputs else ""
    return f"  {inputs}{suffix}"


@dataclass(frozen=True)
class Plan:
    """The result of :meth:`PLC.how` — a reached recording, or a reason it can't be.

    On success, :attr:`fork` is the PLC that PILOT drove to the target.  Its
    ``scan_log`` + ``_synthesis`` holds *are* the replayable recording: every
    steered input and every hold is already in the timeline, so :meth:`replay`
    reconstructs the reached state with no re-derivation.  This is deliberately
    *not* a reconstructed step list — the fork is the artifact.

    On failure, :attr:`fork` is ``None`` and :attr:`reason` explains why (e.g. a
    physical link holding the target out of reach).  :attr:`route` records which
    way PILOT went to a Bool target so the engineer can redirect with
    ``avoid=`` / ``via=``.
    """

    reachable: bool
    target_tag: str
    target_value: Any
    fork: Any = None  # PLC | None — the reached recording (None when unreachable)
    reason: str | None = None
    route: RouteTaken | None = None
    journal: tuple[PlanStep, ...] = ()
    # Scan the drive started from (the anchor). The recording's log inherits the
    # pre-drive setup below this scan; PILOT's own steering is strictly above it.
    anchor_scan: int = 0

    @property
    def total_scans(self) -> int:
        """Scans PILOT took from the anchor to the reached state."""
        if self.fork is None:
            return 0
        return self.fork.state.scan_id - self.anchor_scan

    @property
    def state(self) -> Any:
        """The reached :class:`SystemState` (``None`` when unreachable)."""
        return None if self.fork is None else self.fork.state

    @property
    def changes(self) -> dict[str, Any]:
        """Inputs PILOT explicitly steered over the drive, net last value.

        Read straight from the recording's ``scan_log`` (pulses + forces).  Steady
        and conditional holds are synthesis rungs, not steered inputs, so they do
        not appear here — this is the honest "what did PILOT command" view.
        """
        if self.fork is None:
            return {}
        snap = self.fork._scan_log.snapshot()
        out: dict[str, Any] = {}
        scans = set(snap.patches_by_scan) | set(snap.force_changes_by_scan)
        for scan_id in sorted(s for s in scans if s > self.anchor_scan):
            out.update(snap.patches_by_scan.get(scan_id, {}))
            out.update(snap.force_changes_by_scan.get(scan_id, {}))
        return out

    @property
    def total_changes(self) -> int:
        """Number of distinct inputs PILOT steered over the drive."""
        return len(self.changes)

    @property
    def ordered_steps(self) -> list[tuple[int, dict[str, Any]]]:
        """Steered inputs grouped by scan, in order.

        Returns ``[(scan_id, {tag: value, ...}), ...]`` for each scan where
        PILOT applied a patch or force above the anchor.  Consecutive scans
        with no steering are gaps (coasts) — the caller infers them from the
        scan_id jumps.
        """
        if self.fork is None:
            return []
        snap = self.fork._scan_log.snapshot()
        scans = sorted(
            s
            for s in set(snap.patches_by_scan) | set(snap.force_changes_by_scan)
            if s > self.anchor_scan
        )
        result: list[tuple[int, dict[str, Any]]] = []
        for scan_id in scans:
            inputs: dict[str, Any] = {}
            inputs.update(snap.patches_by_scan.get(scan_id, {}))
            inputs.update(snap.force_changes_by_scan.get(scan_id, {}))
            if inputs:
                result.append((scan_id, inputs))
        return result

    @property
    def tags(self) -> Any:
        """The reached tag map (``None`` when unreachable)."""
        return None if self.fork is None else self.fork.state.tags

    def replay(self) -> Any:
        """Replay the recording on a fresh PLC and return it at the reached state.

        Reconstructs the reached state from the fork's ``scan_log`` + synthesis
        holds — the independent, hold-complete reproduction of the drive.  Raises
        if the plan is unreachable (there is no recording to replay).
        """
        if self.fork is None:
            raise ValueError("unreachable Plan has no recording to replay")
        return self.fork.replay_to(self.fork.state.scan_id)

    def __str__(self) -> str:
        if not self.reachable:
            return f"Unreachable: {self.reason}"
        lines = [
            f"Plan: {self.target_tag}={_format_value(self.target_value)} "
            f"reached in {self.total_scans} scan(s)"
        ]
        if self.route is not None and not self.route.dominant:
            lines.append(f"  Route: {self.route.label}")
            for pivot in self.route.salient_pivots:
                redirect = _render_pivot_redirect(pivot)
                if redirect:
                    lines.append(f"    {redirect}")
        if self.journal:
            lines.append("")
            for step in self.journal:
                lines.append(_format_plan_step(step))
        return "\n".join(lines)

    def __repr__(self) -> str:
        return (
            f"Plan(reachable={self.reachable}, "
            f"target={self.target_tag}={self.target_value!r}, scans={self.total_scans})"
        )
