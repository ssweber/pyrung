"""Reachability path types and semantic constraint rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from pyrung.core.validation.render import operand_name, render_condition

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
    existing_keys: dict[str, set[tuple[str, str, Any, bool, int | float, int | float]]] = {
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

                if atom.operand_is_tag:
                    if composed_invert is not IDENTITY:
                        continue
                    new_atom = Atom(
                        tag=source,
                        form=atom.form,
                        operand=atom.operand,
                        operand_is_tag=True,
                        operand_scale=atom.operand_scale,
                        operand_offset=atom.operand_offset,
                    )
                else:
                    new_threshold = composed_invert(atom.operand)
                    if new_threshold is None or not isinstance(new_threshold, (int, float)):
                        continue
                    new_atom = Atom(tag=source, form=atom.form, operand=new_threshold)

                key = new_atom._key()
                if key not in existing_keys.get(source, set()):
                    enriched.setdefault(source, []).append(new_atom)
                    existing_keys.setdefault(source, set()).add(key)
                    if new_atom.operand_is_tag:
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
                new_atom = Atom(
                    tag=left_name,
                    form=atom.form,
                    operand=right_name,
                    operand_is_tag=True,
                )
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
    - ``"pulse"`` — a candidate pulse (button press).
    - ``"patch"`` — user-style configuration applied before one execution.
    - ``"force"`` — a temporary-logic installation (discovered prerequisite).
    - ``"coast"``   — a bearing coast or let-run dwell (waiting for timers/sequences).
    - ``"accelerator"`` — a timer/counter accumulator patch (proved-safe skip).
    """

    kind: str
    scan: int
    scans: int
    inputs: tuple[tuple[str, Any], ...]
    label: str
    transition: str = ""
    waiting_for: tuple[str, ...] = ()
    steady_holds: tuple[str, ...] = ()
    pulsing_holds: tuple[str, ...] = ()
    accelerators: tuple[tuple[str, Any], ...] = ()
    # Relational lever reports for inputs on this step — "held Band < -100.0 to
    # satisfy PV < Lower (e.g., Band = -100.000001; heuristic value — relation is
    # the requirement, not this number)".  The relation is the requirement; the
    # value is an example.
    notes: tuple[str, ...] = ()
    # Exact PilotRungs behind an installation or present during a coast.
    # ``steady_holds`` / ``pulsing_holds`` remain as a compact compatibility
    # summary; the renderer prefers this lossless representation.
    rungs: tuple[Any, ...] = ()
    source: str = ""


def _format_value(value: Any) -> str:
    """Format a tag value for use in a console command."""
    if value is True:
        return "True"
    if value is False:
        return "False"
    s = str(value)
    if " " in s:
        return f'"{s}"'
    return s


def _rungs_by_tag(rungs: tuple[Any, ...]) -> dict[str, list[Any]]:
    grouped: dict[str, list[Any]] = {}
    for rung in rungs:
        grouped.setdefault(rung.dest, []).append(rung)
    return grouped


def _rung_values(rungs: list[Any]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_format_value(rung.value) for rung in rungs))


def _condition_terms(condition: Any) -> tuple[Any, ...]:
    from pyrung.core.condition import AllCondition

    if isinstance(condition, AllCondition):
        return tuple(term for child in condition.conditions for term in _condition_terms(child))
    return (condition,)


def _self_toggle_scope(rung: Any) -> tuple[Any, ...] | None:
    """Return non-self guards for a Boolean reset/overwrite oscillator."""
    from pyrung.core.condition import CompareNe

    terms = _condition_terms(rung.guard)
    self_terms = tuple(
        term
        for term in terms
        if isinstance(term, CompareNe)
        and operand_name(term.tag) == rung.dest
        and term.value == rung.value
    )
    if rung.value is not True or not self_terms:
        return None
    return tuple(term for term in terms if term not in self_terms)


def _oscillator_tags(rungs: tuple[Any, ...]) -> set[str]:
    return {
        tag
        for tag, rules in _rungs_by_tag(rungs).items()
        if len(_rung_values(rules)) > 1
        or any(_self_toggle_scope(rung) is not None for rung in rules)
    }


def _source_suffix(source: str, *, next_step: int | None = None) -> str:
    labels = {
        "investigation": "found during investigation",
        "prerequisite": (
            f"needed for step {next_step}" if next_step is not None else "needed for the next step"
        ),
        "excursion": "found while testing",
        # Working Theory remains the semantic owner on PlanStep; its internal
        # lifecycle name is not an instruction or rationale for a technician.
        "working-theory-composition": "",
    }
    label = labels.get(source, source)
    return f" ({label})" if label else ""


def _lever_requirement(note: str) -> str:
    """Extract the constraint from a relational lever's witness note."""
    note = note.removeprefix("held ")
    return re.sub(r"\s+\(e\.g\., .+\)$", "", note)


def _format_plan_note(note: str) -> str:
    if note.startswith("held "):
        return "Satisfies: " + _lever_requirement(note)
    return note[:1].upper() + note[1:]


def _rung_guard(rung: Any) -> str:
    return render_condition(rung.guard)


def _format_synthesis_instruction(rung: Any) -> str:
    """Render the instruction shape used by the synthesis rung factory."""
    if rung.value is True:
        return f"latch({rung.dest})"
    if rung.value is False:
        return f"reset({rung.dest})"
    return f"copy({operand_name(rung.value)}, {rung.dest})"


def _display_rung_key(rung: Any) -> tuple[str, str]:
    """Identity of one installed rung as presented in the plan."""
    return (_rung_guard(rung), _format_synthesis_instruction(rung))


def _format_step_refs(steps: tuple[int, ...]) -> str:
    labels = [str(step) for step in steps]
    if len(labels) == 1:
        return f"step {labels[0]}"
    if len(labels) == 2:
        return f"steps {labels[0]} and {labels[1]}"
    return f"steps {', '.join(labels[:-1])}, and {labels[-1]}"


def _format_rung_install(
    prefix: str,
    rungs: tuple[Any, ...],
    source: str,
    notes: tuple[str, ...],
    *,
    next_step: int | None = None,
) -> str:
    """Render the actual guarded rungs installed by PILOT."""
    by_guard: dict[str, list[Any]] = {}
    for rung in rungs:
        by_guard.setdefault(_rung_guard(rung), []).append(rung)

    line = f"{prefix} Install temporary logic{_source_suffix(source, next_step=next_step)}:"
    detail: list[str] = []
    for guard, guarded_rungs in by_guard.items():
        detail.append(f"   with rung({guard}):")
        detail.extend(f"     {_format_synthesis_instruction(rung)}" for rung in guarded_rungs)
    detail.extend(f"   {_format_plan_note(note)}" for note in notes)
    return line + "\n" + "\n".join(detail)


def _format_rung_revoke(
    prefix: str,
    rungs: tuple[Any, ...],
    installed_steps: tuple[int, ...],
) -> str:
    """Render the actual removal of previously installed temporary logic."""
    origin = f" from {_format_step_refs(installed_steps)}" if installed_steps else ""
    line = f"{prefix} Remove temporary logic{origin}:"
    by_guard: dict[str, list[Any]] = {}
    for rung in rungs:
        by_guard.setdefault(_rung_guard(rung), []).append(rung)
    detail: list[str] = []
    for guard, guarded_rungs in by_guard.items():
        detail.append(f"   with rung({guard}):")
        detail.extend(f"     {_format_synthesis_instruction(rung)}" for rung in guarded_rungs)
    return line + "\n" + "\n".join(detail)


def _rung_coast_summary(rungs: tuple[Any, ...], *, same_holds: bool = False) -> list[str]:
    """Summarize installed control machinery without implying every guard is active."""
    by_tag = _rungs_by_tag(rungs)
    oscillator_tags = _oscillator_tags(rungs)
    steady: list[str] = []
    for rung in rungs:
        if rung.dest in oscillator_tags:
            continue
        assignment = f"{rung.dest}={_format_value(rung.value)}"
        if assignment not in steady:
            steady.append(assignment)

    lines: list[str] = []
    if steady:
        keep = "(same)" if same_holds else ", ".join(steady)
        lines.append(f"   Keep: {keep}.")
    if oscillator_tags:
        rendered: list[str] = []
        for tag in dict.fromkeys(rung.dest for rung in rungs if rung.dest in oscillator_tags):
            rules = by_tag[tag]
            self_toggle = (
                next(
                    (
                        (rung, scope)
                        for rung in rules
                        if (scope := _self_toggle_scope(rung)) is not None
                    ),
                    None,
                )
                if len(_rung_values(rules)) == 1
                else None
            )
            if self_toggle is not None:
                # Its exact condition and write are already shown by the
                # installation step.  Do not recast guarded logic as a steady
                # hold or give it a new behavioral name here.
                continue
            else:
                rendered.append(f"{tag} ({' <-> '.join(_rung_values(rules))})")
        if rendered:
            oscillators = ", ".join(rendered)
            lines.append(f"   Oscillate: {oscillators}.")
    return lines


@dataclass(frozen=True)
class RouteAlt:
    """A road not taken at a reported pivot."""

    label: str


@dataclass(frozen=True)
class RoutePivot:
    """An excludable decision the chosen route committed to.

    PILOT picked ``(tag, value)`` where ≥1 other viable option existed.  The
    engineer can exclude it with ``avoid=<avoid_hint>``. ``avoid_hint`` is the
    concrete condition behind the human label: the committed writer's gating
    coil, or a representative steerable leaf of an OR arm. ``salient`` is False
    for trivial cost-0 forks (``Or(Auto, Manual)``), which stay hidden from the
    headline.
    """

    tag: str
    value: Any
    label: str
    kind: str  # "writer" | "or-arm"
    avoid_hint: tuple[str, Any] | None = None
    alternatives: tuple[RouteAlt, ...] = ()
    salient: bool = True


@dataclass(frozen=True)
class RouteTaken:
    """How PILOT reached a Bool target — the legible "here's where I went".

    ``how()`` never reports ambiguous: it starts with a deterministic preferred
    route (cheapest by trace score, rung order breaking ties) and records the
    route that actually reached the goal here. The engineer can exclude it
    with ``avoid=``. ``dominant`` is True when preparation found no real root
    fork.
    """

    label: str
    pivots: tuple[RoutePivot, ...] = ()
    dominant: bool = True

    @property
    def salient_pivots(self) -> tuple[RoutePivot, ...]:
        return tuple(p for p in self.pivots if p.salient)


def _format_hint(hint: tuple[str, Any] | None) -> str:
    """Render a ``(tag, value)`` exclusion hint as the engineer would type it."""
    if hint is None:
        return ""
    tag, value = hint
    return tag if value is True else f"{tag}=={_format_value(value)}"


def _render_pivot_redirect(pivot: RoutePivot) -> str:
    """One-line ``avoid=`` hint for excluding a salient chosen route."""
    avoid_expr = _format_hint(pivot.avoid_hint)
    return f"avoid={avoid_expr}" if avoid_expr else ""


def _format_plan_step(
    idx: int,
    step: PlanStep,
    *,
    dt: float | None = None,
    same_holds: bool = False,
    control_steps: tuple[int, ...] = (),
) -> str:
    prefix = f"{idx}."

    def _with_notes(line: str) -> str:
        if step.notes:
            notes = (_format_plan_note(note) for note in step.notes)
            return line + "\n" + "\n".join(f"   {note}" for note in notes)
        return line

    if step.kind == "force":
        if step.rungs:
            return _format_rung_install(
                prefix,
                step.rungs,
                step.source,
                step.notes,
                next_step=idx + 1,
            )
        tags = ", ".join(f"{t}={_format_value(v)}" for t, v in step.inputs)
        return _with_notes(f"{prefix} Keep {tags}.")

    if step.kind == "revoke":
        return _format_rung_revoke(prefix, step.rungs, control_steps)

    if step.kind == "patch":
        tags = ", ".join(f"{t}={_format_value(v)}" for t, v in step.inputs)
        return _with_notes(f"{prefix} Set {tags} before the next scan.")

    if step.kind == "pulse":
        tags = ", ".join(f"{t}={_format_value(v)}" for t, v in step.inputs)
        line = f"{prefix} Set {tags}."
        if step.transition:
            line += f"\n   Observed: {step.transition}."
        return _with_notes(line)

    if step.kind == "coast":
        scan_label = "scan" if step.scans == 1 else "scans"
        duration = f" ({step.scans} {scan_label})"
        if dt and step.scans > 0:
            secs = step.scans * dt
            t = f"~{secs:.0f}s" if secs >= 1 else f"~{secs * 1000:.0f}ms"
            duration = f" {t} ({step.scans} scans)"
        line = f"{prefix} Wait{duration}."
        if step.transition:
            line += f"\n   Observed: {step.transition}."
        sub: list[str] = []
        if step.rungs:
            if same_holds:
                sub.append("   Temporary logic: (same).")
            elif control_steps:
                sub.append(f"   Temporary logic in effect: {_format_step_refs(control_steps)}.")
            else:
                sub.extend(_rung_coast_summary(step.rungs))
        else:
            if step.steady_holds:
                keep = "(same)" if same_holds else ", ".join(step.steady_holds)
                sub.append(f"   Keep: {keep}.")
            if step.pulsing_holds:
                sub.append(f"   Pulse: {', '.join(step.pulsing_holds)}.")
        if step.accelerators:
            skip_items = ", ".join(f"{t}={_format_value(v)}" for t, v in step.accelerators)
            sub.append(f"   Jump ahead: set {skip_items}.")
        if sub:
            return line + "\n" + "\n".join(sub)
        return line

    if step.kind == "accelerator":
        tags = ", ".join(f"{t}={_format_value(v)}" for t, v in step.inputs)
        return f"{prefix} Jump ahead: set {tags}."

    inputs = ", ".join(f"{t}={_format_value(v)}" for t, v in step.inputs)
    line = f"{prefix} Set {inputs}."
    if step.transition:
        line += f"\n   Observed: {step.transition}."
    return _with_notes(line)


class PlanStatus(Enum):
    """What ``how()`` established about the requested target."""

    REACHED = "reached"
    CANNOT_REACH = "cannot_reach"
    STOPPED = "stopped"


@dataclass(frozen=True)
class Plan:
    """The result of :meth:`PLC.how` and what the drive established.

    On success, :attr:`fork` is the PLC that PILOT drove to the target.  Its
    ``scan_log`` + ``_synthesis`` holds *are* the replayable recording: every
    steered input and every hold is already in the timeline, so :meth:`replay`
    reconstructs the reached state with no re-derivation.  This is deliberately
    *not* a reconstructed step list — the fork is the artifact.

    On failure, :attr:`fork` is ``None``. :attr:`status` distinguishes a proved
    ``CANNOT_REACH`` result from ``STOPPED``, where PILOT ran out of safe,
    evidence-backed actions. :attr:`reason` names the proof or outstanding
    frontier. :attr:`route` records which way PILOT went to a Bool target so the
    engineer can exclude with ``avoid=``.
    """

    reachable: bool
    target_tag: str
    target_value: Any
    fork: Any = None  # PLC | None — the reached recording (None when unreachable)
    reason: str | None = None
    route: RouteTaken | None = None
    journal: tuple[PlanStep, ...] = ()
    # Multi-target ``how(A, B, …)`` only: the conjunction of goals that had to hold at
    # the same committed scan.  Empty for a single target, whose goal is
    # ``target_tag``/``target_value``.  ``__str__`` renders this instead of the
    # synthetic ``target_tag`` label so the headline reads as the conjunction it is.
    targets: tuple[tuple[str, Any], ...] = ()
    # Scan the drive started from (the anchor). The recording's log inherits the
    # pre-drive setup below this scan; PILOT's own steering is strictly above it.
    anchor_scan: int = 0
    # Knowledge threaded off the drive's ``_PilotState`` (recording only — never
    # consulted by ``replay`` or the reachability verdict).  ``journey`` is the full
    # attempt log incl. reverted rounds; ``hold_log`` the installed holds; the
    # ``lever_notes`` the relational reports per steered tag; ``avoid_names`` the
    # route-exclusion evidence a terminal miss reads. These are
    # the Knowledge half of the World/Knowledge split — they survive every revert,
    # so the Plan can explain the same drive it recorded.
    journey: tuple[Any, ...] = ()
    hold_log: tuple[Any, ...] = ()
    lever_notes: dict[str, str] = field(default_factory=dict)
    avoid_names: tuple[str, ...] = ()
    status: PlanStatus | None = None

    def __post_init__(self) -> None:
        status = self.status
        if status is None:
            status = PlanStatus.REACHED if self.reachable else PlanStatus.CANNOT_REACH
            object.__setattr__(self, "status", status)
        if self.reachable != (status is PlanStatus.REACHED):
            raise ValueError("Plan.reachable and Plan.status disagree")

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

    @property
    def dt(self) -> float | None:
        """Scan period in seconds (``None`` when unreachable)."""
        return getattr(self.fork, "_dt", None) if self.fork is not None else None

    def __str__(self) -> str:
        if self.targets:
            goal = " & ".join(f"{t}={_format_value(v)}" for t, v in self.targets)
        else:
            goal = f"{self.target_tag}={_format_value(self.target_value)}"
        if not self.reachable:
            headline = (
                f"Cannot reach {goal}."
                if self.status is PlanStatus.CANNOT_REACH
                else f"Stopped before reaching {goal}."
            )
            reason = self.reason or "No productive next action was found."
            reason = reason.removeprefix("pilot: ").removeprefix("stuck: ")
            if "; still waiting on " in reason:
                reason, waiting = reason.split("; still waiting on ", 1)
                reason = reason if reason.endswith((".", "!", "?")) else reason + "."
                return f"{headline}\n  Reason: {reason}\n  Waiting for: {waiting}"
            reason = reason if reason.endswith((".", "!", "?")) else reason + "."
            return f"{headline}\n  Reason: {reason}"

        dt = self.dt
        elapsed = ""
        if dt and self.total_scans > 0:
            seconds = self.total_scans * dt
            duration = f"{seconds:.1f}s" if seconds >= 1 else f"{seconds * 1000:.0f}ms"
            elapsed = f" (~{duration})"
        scan_label = "scan" if self.total_scans == 1 else "scans"
        lines = [f"Reached {goal} in {self.total_scans} {scan_label}{elapsed}."]
        if self.route is not None and not self.route.dominant:
            lines.append(f"Route: {self.route.label}")
            redirects: list[str] = []
            for pivot in self.route.salient_pivots:
                redirect = _render_pivot_redirect(pivot)
                if redirect:
                    redirects.extend(redirect.split(" | "))
            if redirects:
                lines.append(f"  Other routes: {' | '.join(dict.fromkeys(redirects))}")
        if self.journal:
            lines.extend(("", "Steps:", ""))
            previous_holds: tuple[Any, ...] | None = None
            installed_at: dict[tuple[str, str], int] = {}
            for i, step in enumerate(self.journal, 1):
                if step.kind == "force":
                    for rung in step.rungs:
                        installed_at.setdefault(_display_rung_key(rung), i)
                holds = step.rungs or tuple(step.steady_holds)
                same_holds = step.kind == "coast" and bool(holds) and holds == previous_holds
                control_steps = tuple(
                    sorted(
                        {
                            installed_at[key]
                            for rung in step.rungs
                            if (key := _display_rung_key(rung)) in installed_at
                        }
                    )
                )
                lines.append(
                    _format_plan_step(
                        i,
                        step,
                        dt=dt,
                        same_holds=same_holds,
                        control_steps=control_steps,
                    )
                )
                if step.kind == "revoke":
                    for rung in step.rungs:
                        installed_at.pop(_display_rung_key(rung), None)
                if step.kind == "coast" and holds:
                    previous_holds = holds
                if i != len(self.journal):
                    lines.append("")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return (
            f"Plan(reachable={self.reachable}, "
            f"target={self.target_tag}={self.target_value!r}, scans={self.total_scans})"
        )
