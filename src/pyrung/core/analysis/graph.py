"""Reachability path types and semantic constraint rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from pyrung.core.validation.render import operand_name


@dataclass(frozen=True)
class PlanStep:
    """One move in the PILOT drive journal — what was done and why.

    ``kind`` classifies the move so the repr can group/filter:
    - ``"pulse"`` — a candidate pulse (button press).
    - ``"patch"`` — user-style configuration applied before one execution.
    - ``"force"`` — a temporary-logic installation (discovered prerequisite).
    - ``"coast"``   — a bearing coast or let-run dwell (waiting for timers/sequences).
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
    # The temporary logic this step put in (or took out), printed from the rung that ran, so the
    # text cannot drift from it.  A step built by hand leaves it empty, and the text builds its
    # rules alone.
    logic: tuple[str, ...] = ()
    # The logic that runs after a removal that leaves some of a tag's rules in.
    kept_logic: tuple[str, ...] = ()


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


def rules_logic(rungs: tuple[Any, ...]) -> tuple[str, ...]:
    """*rungs* built into a rung on their own and printed, for logic no recording ran."""
    from pyrung.core.analysis.pilot.overlay import pilot_rules_rung
    from pyrung.core.synthesis import bracket_lines

    built = pilot_rules_rung(rungs, {})
    return () if built is None else bracket_lines(built)


def _format_step_refs(steps: tuple[int, ...]) -> str:
    labels = [str(step) for step in steps]
    if len(labels) == 1:
        return f"step {labels[0]}"
    if len(labels) == 2:
        return f"steps {labels[0]} and {labels[1]}"
    return f"steps {', '.join(labels[:-1])}, and {labels[-1]}"


def _format_rung_install(
    prefix: str,
    step: PlanStep,
    replaced: dict[str, int] | None = None,
    *,
    next_step: int | None = None,
) -> str:
    """Print the temporary logic a step put in, as it ran."""
    source = _source_suffix(step.source, next_step=next_step)
    logic = step.logic or rules_logic(step.rungs)
    if logic:
        line = f"{prefix} Install temporary logic{source}:"
    else:
        # A non-Bool tag whose every condition is never true gets no row at all.
        whose = "their" if len(dict.fromkeys(rung.dest for rung in step.rungs)) > 1 else "its"
        line = (
            f"{prefix} Install temporary logic for {_step_tags(step)}{source}."
            f" None of {whose} conditions can be true, so it writes nothing."
        )
    detail = [f"   {row}" for row in logic]
    for tag, earlier in (replaced or {}).items():
        detail.append(f"   This replaces the logic for {tag} from step {earlier}.")
    detail.extend(f"   {_format_plan_note(note)}" for note in step.notes)
    return "\n".join((line, *detail))


def _step_tags(step: PlanStep) -> str:
    return ", ".join(dict.fromkeys(rung.dest for rung in step.rungs))


def _format_rung_revoke(
    prefix: str,
    step: PlanStep,
    installed_steps: tuple[int, ...],
    *,
    ran: tuple[Any, ...] = (),
    kept: tuple[str, ...] = (),
) -> str:
    """Print the temporary logic a step took out, as it ran until then.

    *ran* is every rule of the step's tags until then, for a step no recording printed.  With
    *kept*, some of those rules stay in, and the text shows the logic before and after.
    """
    origin = f" from {_format_step_refs(installed_steps)}" if installed_steps else ""
    logic = step.logic or rules_logic(ran or step.rungs)
    if kept:
        return "\n".join(
            (
                f"{prefix} Remove some of the temporary logic{origin}. Until now it ran as:",
                *(f"   {row}" for row in logic),
                "   From here on it runs as:",
                *(f"   {row}" for row in kept),
            )
        )
    if not logic:
        return f"{prefix} Remove temporary logic for {_step_tags(step)}{origin}. It wrote nothing."
    return "\n".join((f"{prefix} Remove temporary logic{origin}:", *(f"   {row}" for row in logic)))


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
    replaced: dict[str, int] | None = None,
    ran: tuple[Any, ...] = (),
    kept: tuple[str, ...] = (),
) -> str:
    prefix = f"{idx}."

    def _with_notes(line: str) -> str:
        if step.notes:
            notes = (_format_plan_note(note) for note in step.notes)
            return line + "\n" + "\n".join(f"   {note}" for note in notes)
        return line

    if step.kind == "force":
        if step.rungs:
            return _format_rung_install(prefix, step, replaced, next_step=idx + 1)
        tags = ", ".join(f"{t}={_format_value(v)}" for t, v in step.inputs)
        return _with_notes(f"{prefix} Keep {tags}.")

    if step.kind == "revoke":
        return _format_rung_revoke(prefix, step, control_steps, ran=ran, kept=kept)

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
        if sub:
            return line + "\n" + "\n".join(sub)
        return line

    inputs = ", ".join(f"{t}={_format_value(v)}" for t, v in step.inputs)
    line = f"{prefix} Set {inputs}."
    if step.transition:
        line += f"\n   Observed: {step.transition}."
    return _with_notes(line)


MODBUS_NOT_SIMULATED = (
    "how() does not simulate Modbus send()/receive(). Their status bits and destinations"
    " keep their values unless a step sets them."
)
# A step's note on a tag a Modbus device would supply.
MODBUS_STEP_NOTE = "This value stands for the Modbus device's answer."


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
    # Preserve relational and conjunctive goal semantics in the public result;
    # the scalar pairs above are display representatives for such predicates.
    target_predicate: Any = None
    # How a restart went, when how() tried one because nothing worked from the session.  On a
    # reached plan the restart is its first step, and ``fork`` and ``anchor_scan`` are the new
    # run: scan 0 is the restart, not the session's.
    restart: Any = None
    # The forces this plan releases, when nothing worked with them on.  Its first step.
    released: Any = None
    # The move this plan rests on, which nothing in the trace pointed to.  how() tried it because
    # it was about to stop, and a plan that has one says so.
    last_resort: Any = None
    # The session's own forces and pending patches.  They run in the plan but are not its steps.
    session_inputs: dict[str, Any] = field(default_factory=dict)
    # Those of them no scan had applied, so the plan's first scan is theirs.
    waiting_overrides: tuple[str, ...] = ()
    # The tags a Modbus device would supply, which how() may set.  None means the program
    # has no send() or receive().  Otherwise the text ends by saying how() did no Modbus I/O.
    modbus_levers: frozenset[str] | None = None

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
        out: dict[str, Any] = {}
        for _scan_id, inputs in self.ordered_steps:
            out.update(inputs)
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
            inputs.update(self._not_the_sessions(snap.patches_by_scan.get(scan_id, {}), scan_id))
            inputs.update(self._not_the_sessions(snap.force_changes_by_scan.get(scan_id, {})))
            if inputs:
                result.append((scan_id, inputs))
        return result

    def _not_the_sessions(
        self, inputs: dict[str, Any], patched_on: int | None = None
    ) -> dict[str, Any]:
        """Drop what the session set itself: its forces on any scan, its patches on the first."""
        if patched_on is not None and patched_on != self.anchor_scan + 1:
            return dict(inputs)
        own = self.session_inputs
        return {
            name: value for name, value in inputs.items() if name not in own or own[name] != value
        }

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
        text = self._text()
        return text if self.modbus_levers is None else f"{text}\n\n{MODBUS_NOT_SIMULATED}"

    def _sets_a_modbus_lever(self, inputs: Any) -> bool:
        return any(name in (self.modbus_levers or ()) for name, _value in inputs)

    def _text(self) -> str:
        if self.target_predicate is not None:
            from pyrung.core.analysis.simplified import And, render

            predicate = self.target_predicate
            goal = (
                " & ".join(render(term) for term in predicate.terms)
                if isinstance(predicate, And)
                else render(predicate)
            )
        elif self.targets:
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
            restart = (
                "".join(f"\n  {line}" for line in self.restart.lines())
                if self.restart is not None
                else ""
            )
            if "; still waiting on " in reason:
                reason, waiting = reason.split("; still waiting on ", 1)
                reason = reason if reason.endswith((".", "!", "?")) else reason + "."
                return f"{headline}\n  Reason: {reason}\n  Waiting for: {waiting}{restart}"
            reason = reason if reason.endswith((".", "!", "?")) else reason + "."
            return f"{headline}\n  Reason: {reason}{restart}"

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
        leading = self._leading_steps()
        first_step = len(leading) + 1
        if leading:
            lines.extend(("", "Steps:"))
            for step_line in leading:
                lines.extend(("", step_line))
        if self.journal:
            lines.extend(("",) if leading else ("", "Steps:", ""))
            from pyrung.core.analysis.pilot.world_key import _rung_identity

            previous_holds: tuple[Any, ...] | None = None
            # The step whose text last showed each tag's logic.  A later step that changes the
            # tag's logic says which step's logic it replaces.
            installed_at: dict[str, int] = {}
            # Each rule in so far, with the step that put it in.  A removal can take out some of
            # a tag's rules and leave the rest.
            rules_in: dict[tuple[Any, ...], tuple[Any, int]] = {}
            for j, (i, step) in enumerate(enumerate(self.journal, first_step)):
                dests = dict.fromkeys(rung.dest for rung in step.rungs)
                earlier = {dest: installed_at[dest] for dest in dests if dest in installed_at}
                control = tuple(sorted(set(earlier.values())))
                ran: tuple[Any, ...] = ()
                kept: tuple[str, ...] = ()
                if step.kind == "force":
                    for dest in dests:
                        installed_at[dest] = i
                    for rung in step.rungs:
                        rules_in[_rung_identity(rung)] = (rung, i)
                elif step.kind == "revoke":
                    ran = tuple(rung for rung, _i in rules_in.values() if rung.dest in dests)
                    gone = [_rung_identity(rung) for rung in step.rungs]
                    # The steps that put in the rules this one takes out.
                    put_in_by = {rules_in[key][1] for key in gone if key in rules_in}
                    if put_in_by:
                        control = tuple(sorted(put_in_by))
                    for key in gone:
                        rules_in.pop(key, None)
                    left = tuple(rung for rung, _i in rules_in.values() if rung.dest in dests)
                    # Logic put in on the same scan prints the tag's whole logic itself.
                    rebuilt = any(
                        later.kind == "force"
                        and later.scan == step.scan
                        and any(rung.dest in dests for rung in later.rungs)
                        for later in self.journal[j + 1 :]
                    )
                    # The recording also knows rules put in before the plan's first step.  On a
                    # rebuilt scan its rows after the removal are the new logic's, not kept ones.
                    kept_dests = {rung.dest for rung in left} or (
                        set(dests) if step.kept_logic and not rebuilt else set()
                    )
                    if kept_dests and not rebuilt:
                        kept = step.kept_logic or rules_logic(left)
                    for dest in dests:
                        if dest in kept_dests:
                            if not rebuilt:
                                installed_at[dest] = i
                        else:
                            installed_at.pop(dest, None)
                holds = step.rungs or tuple(step.steady_holds)
                same_holds = step.kind == "coast" and bool(holds) and holds == previous_holds
                lines.append(
                    _format_plan_step(
                        i,
                        replace(step, notes=(*step.notes, MODBUS_STEP_NOTE))
                        if self._sets_a_modbus_lever(step.inputs)
                        else step,
                        dt=dt,
                        same_holds=same_holds,
                        control_steps=control,
                        replaced=earlier,
                        ran=ran,
                        kept=kept,
                    )
                )
                if step.kind == "coast" and holds:
                    previous_holds = holds
                if i != len(self.journal) + first_step - 1:
                    lines.append("")
        return "\n".join(lines)

    def _leading_steps(self) -> list[str]:
        """What a plan has to do before PILOT's own steps, each numbered in order."""
        steps: list[str] = []
        if self.restart is not None:
            steps.append(self.restart.step_line(len(steps) + 1))
        if self.released is not None:
            steps.append(self.released.step_line(len(steps) + 1))
        if self.waiting_overrides:
            names = ", ".join(self.waiting_overrides)
            steps.append(
                f"{len(steps) + 1}. Let one scan run, so the session's own patch or force on"
                f" {names} takes effect."
            )
        if self.last_resort is not None:
            line = self.last_resort.step_line(len(steps) + 1)
            if self._sets_a_modbus_lever(((self.last_resort.tag, self.last_resort.value),)):
                line += f"\n   {MODBUS_STEP_NOTE}"
            steps.append(line)
        return steps

    def __repr__(self) -> str:
        return (
            f"Plan(reachable={self.reachable}, "
            f"target={self.target_tag}={self.target_value!r}, scans={self.total_scans})"
        )
