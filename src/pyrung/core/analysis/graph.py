"""Reachability path types and semantic constraint rendering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable as _Callable

    from pyrung.core.analysis.simplified import Atom

    StateKey = tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class ReachabilityStep:
    action: dict[str, Any]
    source_key: tuple[Any, ...]
    dest_key: tuple[Any, ...]
    scans: int
    intermediates: tuple[Any, ...] = ()
    constraints: dict[str, str] | None = None
    # Runner-native reactive oscillators (``ConditionalHold`` keyed by held tag)
    # that animated a let-run coast over this step's span — keeps the public path
    # self-describing so a replay re-installs ``when(guard).do(patch)`` and
    # reproduces the coast.  ``None`` for plain command/BFS steps.
    reactive_holds: dict[str, Any] | None = None


_FORM_TO_OP = {"gt": ">", "ge": ">=", "lt": "<", "le": "<=", "eq": "==", "ne": "!="}
_FORM_FLIP = {"gt": "<", "ge": "<=", "lt": ">", "le": ">=", "eq": "==", "ne": "!="}
_TIER3_SOURCES = frozenset({"bool", "choices", "done_acc_tri_state"})
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


def _eval_comparison(form: str, left: Any, right: Any) -> bool:
    """Evaluate whether a comparison holds for given values."""
    try:
        if form == "gt":
            return left > right
        if form == "ge":
            return left >= right
        if form == "lt":
            return left < right
        if form == "le":
            return left <= right
        if form == "eq":
            return left == right
        if form == "ne":
            return left != right
    except TypeError:
        pass
    return False


def _classify_step_inputs(
    action: dict[str, Any],
    atom_index: dict[str, list[Atom]],
    domain_sources: dict[str, str],
    dest_tags: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Classify each input in a step and return semantic display strings.

    Returns a dict mapping tag names to their display string. Tags consumed
    by a Tier 2 group are keyed under a synthetic ``_group:<tag1>,<tag2>`` key
    so the renderer can suppress their individual entries.
    """
    action_tags = set(action.keys())
    constraints: dict[str, str] = {}
    suppressed: set[str] = set()
    seen_pairs: set[frozenset[str]] = set()

    # --- Tier 2: tag-vs-tag relational constraints ---
    from pyrung.core.analysis.simplified import ArithAtom

    for tag in sorted(action_tags):
        source = domain_sources.get(tag, "unknown")
        if source in _TIER3_SOURCES:
            continue
        atoms = atom_index.get(tag, [])
        # First pass: prefer plain Atom tag-vs-tag (e.g. A > B)
        found = False
        for atom in atoms:
            if isinstance(atom, ArithAtom):
                continue
            if atom.form not in _FORM_TO_OP:
                continue
            if not isinstance(atom.operand, str):
                continue
            other = atom.operand if atom.tag == tag else atom.tag
            if other not in action_tags:
                continue
            other_source = domain_sources.get(other, "unknown")
            if other_source in _TIER3_SOURCES:
                continue
            pair = frozenset({tag, other})
            if pair in seen_pairs:
                continue
            if dest_tags is not None:
                left_val = dest_tags.get(atom.tag)
                right_val = dest_tags.get(atom.operand)
                if left_val is not None and right_val is not None:
                    if not _eval_comparison(atom.form, left_val, right_val):
                        continue
            seen_pairs.add(pair)
            if atom.tag == tag:
                op = _FORM_TO_OP[atom.form]
                display = f"{atom.tag} {op} {atom.operand}"
            else:
                op = _FORM_FLIP[atom.form]
                display = f"{tag} {op} {other}"
            group_key = f"_group:{min(tag, other)},{max(tag, other)}"
            constraints[group_key] = display
            suppressed.add(tag)
            suppressed.add(other)
            found = True
            break
        if found:
            continue
        # Second pass: ArithAtom (e.g. A - B > 5, A + B > 100)
        for atom in atoms:
            if not isinstance(atom, ArithAtom):
                continue
            if atom.form not in _FORM_TO_OP:
                continue
            other = atom.right if atom.left == tag else atom.left
            if other not in action_tags:
                continue
            other_source = domain_sources.get(other, "unknown")
            if other_source in _TIER3_SOURCES:
                continue
            pair = frozenset({atom.left, atom.right})
            if pair in seen_pairs:
                continue
            if dest_tags is not None:
                left_val = dest_tags.get(atom.left)
                right_val = dest_tags.get(atom.right)
                if left_val is not None and right_val is not None:
                    try:
                        if atom.arith_op == "+":
                            computed = left_val + right_val
                        elif atom.arith_op == "-":
                            computed = left_val - right_val
                        elif atom.arith_op == "*":
                            computed = left_val * right_val
                        else:
                            continue
                    except TypeError:
                        continue
                    if not _eval_comparison(atom.form, computed, atom.operand):
                        continue
            seen_pairs.add(pair)
            op = _FORM_TO_OP[atom.form]
            display = f"{atom.left} {atom.arith_op} {atom.right} {op} {atom.operand}"
            group_key = f"_group:{min(atom.left, atom.right)},{max(atom.left, atom.right)}"
            constraints[group_key] = display
            suppressed.add(atom.left)
            suppressed.add(atom.right)
            break

    # --- Tier 1 / Tier 2 solo: remaining non-bool tags ---
    for tag in sorted(action_tags):
        if tag in suppressed:
            continue
        source = domain_sources.get(tag, "unknown")
        if source in _TIER3_SOURCES:
            continue
        atoms = atom_index.get(tag, [])
        value = action[tag]

        # Collect literal thresholds and tag-vs-tag constraints for this tag
        best_literal: tuple[str, Any] | None = None
        best_relational: str | None = None
        has_literal = False
        for atom in atoms:
            if isinstance(atom, ArithAtom):
                if atom.form not in _FORM_TO_OP:
                    continue
                if dest_tags is not None:
                    left_val = dest_tags.get(atom.left)
                    right_val = dest_tags.get(atom.right)
                    if left_val is not None and right_val is not None:
                        try:
                            if atom.arith_op == "+":
                                computed = left_val + right_val
                            elif atom.arith_op == "-":
                                computed = left_val - right_val
                            elif atom.arith_op == "*":
                                computed = left_val * right_val
                            else:
                                continue
                        except TypeError:
                            continue
                        if not _eval_comparison(atom.form, computed, atom.operand):
                            continue
                op = _FORM_TO_OP[atom.form]
                best_relational = f"{atom.left} {atom.arith_op} {atom.right} {op} {atom.operand}"
                continue
            if atom.form not in _FORM_TO_OP:
                continue
            if isinstance(atom.operand, str):
                # Tag-vs-tag — check if satisfied using dest state
                if dest_tags is not None:
                    left_val = dest_tags.get(atom.tag)
                    right_val = dest_tags.get(atom.operand)
                    if left_val is not None and right_val is not None:
                        if not _eval_comparison(atom.form, left_val, right_val):
                            continue
                if atom.tag == tag:
                    best_relational = f"{atom.tag} {_FORM_TO_OP[atom.form]} {atom.operand}"
                else:
                    best_relational = f"{tag} {_FORM_FLIP[atom.form]} {atom.tag}"
            elif isinstance(atom.operand, (int, float)) and atom.tag == tag:
                has_literal = True
                threshold = atom.operand
                if best_literal is None or abs(value - threshold) < abs(value - best_literal[1]):
                    best_literal = (_FORM_TO_OP[atom.form], threshold)

        if best_literal is not None:
            op, thresh = best_literal
            constraints[tag] = f"{tag}={value} ({op} {thresh})"
        elif not has_literal and best_relational is not None:
            # No literal anchor — value is arbitrary, show the constraint
            constraints[tag] = best_relational

    if suppressed:
        for tag in suppressed:
            constraints.setdefault(f"_suppress:{tag}", "")

    return constraints if constraints else {}


def _render_step_inputs(
    step: ReachabilityStep,
    tag_defaults: dict[str, Any] | None = None,
) -> str:
    """Render a step's inputs using semantic constraints when available."""
    action = step.action
    if tag_defaults:
        action = {k: v for k, v in action.items() if v != tag_defaults.get(k)}

    if not step.constraints:
        return ", ".join(f"{k}={v}" for k, v in sorted(action.items()))

    suppressed = {k.split(":", 1)[1] for k in step.constraints if k.startswith("_suppress:")}
    groups = [(k, v) for k, v in sorted(step.constraints.items()) if k.startswith("_group:")]

    parts: list[str] = []
    for _, display in groups:
        parts.append(display)
    for tag in sorted(action.keys()):
        if tag in suppressed:
            continue
        if tag in step.constraints:
            parts.append(step.constraints[tag])
        else:
            parts.append(f"{tag}={action[tag]}")
    return ", ".join(parts)


def _render_step_diff(step: ReachabilityStep, prev_action: dict[str, Any]) -> str:
    """Render only the inputs that changed from the previous step."""
    changed = {k: v for k, v in step.action.items() if prev_action.get(k) != v}
    if not changed:
        return ""
    if not step.constraints:
        return ", ".join(f"{k}={v}" for k, v in sorted(changed.items()))

    suppressed = {k.split(":", 1)[1] for k in step.constraints if k.startswith("_suppress:")}
    groups = [(k, v) for k, v in sorted(step.constraints.items()) if k.startswith("_group:")]

    parts: list[str] = []
    for _, display in groups:
        parts.append(display)
    for tag in sorted(changed.keys()):
        if tag in suppressed:
            continue
        if tag in step.constraints:
            parts.append(step.constraints[tag])
        else:
            parts.append(f"{tag}={changed[tag]}")
    return ", ".join(parts)


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
class TriangleRow:
    """One hold's row in the triangle table: a protection interval over steps.

    ``start`` is the 1-based step whose action establishes the held value
    (``None`` when the hold references no write in the plan's steps — e.g. a
    hold surviving from a dropped subtree); ``end`` is the 1-based step whose
    action rewrites the input (the divest point), or ``None`` when the value
    persists through plan end.  ``scans`` is the folded scan count the hold
    spans — its timing window width.  ``divested`` marks holds the walker
    released before plan end (the row the hold leaves is an emergent phase
    boundary, discovered by walking).
    """

    name: str
    value: Any
    goal: str
    start: int | None
    end: int | None
    scans: int
    divested: bool

    def render(self) -> str:
        head = f"{self.name}={_format_value(self.value)} (for {self.goal})"
        if self.start is None:
            return f"{head}: no establishing step in plan"
        if self.end is not None:
            span = f"established step {self.start}, divested step {self.end}"
        elif self.divested:
            span = f"established step {self.start}, released (value persists)"
        else:
            span = f"established step {self.start}, held through end"
        return f"{head}: {span}, window {self.scans} scan(s)"


@dataclass(frozen=True)
class TriangleTable:
    """Triangle table over a walk plan (Fikes-Hart-Nilsson 1972 / PLANEX).

    Derived once from holds + steps at Path-build time.  ``kernel(i)`` is the
    set of external-input conditions that must hold at entry to step *i* for
    steps ``i..n`` to remain valid — input holds only; program state is not
    carried (the walker's holds never assert anything about the program).
    ``kernel(n_steps + 1)`` is the post-plan must-stay set: ``Path.holds``,
    plus (conservatively) any released hold whose input was never rewritten
    in the realized steps — its release point is unknown, and extra
    monitoring is the safe direction.
    """

    rows: tuple[TriangleRow, ...]
    n_steps: int

    def kernel(self, i: int) -> frozenset[tuple[str, Any]]:
        """Conditions required at entry to step *i* (``1 <= i <= n_steps + 1``)."""
        if i < 1 or i > self.n_steps + 1:
            raise ValueError(f"kernel index {i} out of range 1..{self.n_steps + 1}")
        out: set[tuple[str, Any]] = set()
        for row in self.rows:
            if row.start is None:
                continue
            end = row.end if row.end is not None else self.n_steps + 1
            if row.divested and row.end is None:
                # Released hold whose value happens to persist: no longer a
                # plan requirement after release, but the release step is
                # unknown — conservatively keep it (extra monitoring, never a
                # wrong plan).
                end = self.n_steps + 1
            if row.start < i <= end:
                out.add((row.name, row.value))
        return frozenset(out)

    def highest_true_kernel(self, tags: dict[str, Any]) -> int:
        """The highest step whose kernel *tags* satisfies (divergence resume point).

        Checks input-hold conditions only; ``kernel(1)`` is empty by
        construction, so the result is always at least 1.
        """
        for i in range(self.n_steps + 1, 0, -1):
            if all(tags.get(name) == value for name, value in self.kernel(i)):
                return i
        return 1

    def divest_points(self) -> tuple[TriangleRow, ...]:
        """Rows whose hold leaves at a known step — emergent phase boundaries."""
        return tuple(r for r in self.rows if r.divested and r.end is not None)

    def narrowest_window(self) -> TriangleRow | None:
        """The hold with the smallest scan span — the plan's timing fragility."""
        spanned = [r for r in self.rows if r.start is not None]
        if not spanned:
            return None
        return min(spanned, key=lambda r: r.scans)

    def __str__(self) -> str:
        lines = [f"Triangle table ({self.n_steps} step(s), {len(self.rows)} row(s)):"]
        for row in self.rows:
            lines.append(f"  {row.render()}")
        final = self.kernel(self.n_steps + 1) if self.n_steps >= 0 else frozenset()
        if final:
            rendered = ", ".join(f"{name}={_format_value(value)}" for name, value in sorted(final))
            lines.append(f"  Kernel after plan: {rendered}")
        narrow = self.narrowest_window()
        if narrow is not None:
            lines.append(
                f"  Narrowest window: {narrow.name}={_format_value(narrow.value)}"
                f" ({narrow.scans} scan(s))"
            )
        return "\n".join(lines)


def _value_runs(writes: list[tuple[int, Any]]) -> list[tuple[int, Any, int | None]]:
    """Collapse a write history into value runs ``(start, value, end)``.

    *writes* is ``(1-based step, value)`` in step order; consecutive writes of
    the same value merge.  ``end`` is the step whose write changes the value
    (``None`` for the final run — external inputs are sticky).
    """
    runs: list[tuple[int, Any, int | None]] = []
    for idx, val in writes:
        if runs and runs[-1][1] == val:
            continue
        if runs:
            prev = runs[-1]
            runs[-1] = (prev[0], prev[1], idx)
        runs.append((idx, val, None))
    return runs


def _build_triangle_table(
    steps: tuple[tuple[dict[str, Any], int], ...],
    final_holds: tuple[tuple[str, Any, str], ...],
    released_holds: tuple[tuple[str, Any, str], ...],
) -> TriangleTable | None:
    """Derive the triangle table from realized steps + the walk's hold history.

    Each hold is matched to a value run of its input's write history —
    backwards, so the surviving hold claims the last matching run and earlier
    (divested) holds claim earlier runs.  Released holds with no remaining
    matching run were inconsequential to the realized plan (released and
    re-established at the same value) and get no row; a *final* hold with no
    matching run keeps a span-less row so the table acknowledges everything
    in ``Path.holds``.  Returns ``None`` when no holds were tracked at all.
    """
    if not final_holds and not released_holds:
        return None
    n = len(steps)
    rows: list[TriangleRow] = []
    names = {name for name, _v, _g in final_holds} | {name for name, _v, _g in released_holds}
    final_by_name = {name: (value, goal) for name, value, goal in final_holds}
    for name in sorted(names):
        writes = [
            (i, action[name]) for i, (action, _scans) in enumerate(steps, 1) if name in action
        ]
        runs = _value_runs(writes)
        # Chronological hold sequence for this input: divested first, the
        # surviving hold (if any) last.
        seq: list[tuple[Any, str, bool]] = [
            (value, goal, True) for hname, value, goal in released_holds if hname == name
        ]
        if name in final_by_name:
            value, goal = final_by_name[name]
            seq.append((value, goal, False))
        run_hi = len(runs) - 1
        matched: list[TriangleRow] = []
        for value, goal, divested in reversed(seq):
            j = run_hi
            while j >= 0 and runs[j][1] != value:
                j -= 1
            if j < 0:
                if not divested:
                    matched.append(TriangleRow(name, value, goal, None, None, 0, False))
                continue
            start, _v, end = runs[j]
            last = (end - 1) if end is not None else n
            scans = sum(steps[k - 1][1] for k in range(start, last + 1))
            matched.append(TriangleRow(name, value, goal, start, end, scans, divested))
            run_hi = j - 1
        rows.extend(reversed(matched))
    rows.sort(key=lambda r: (r.start if r.start is not None else n + 2, r.name))
    return TriangleTable(rows=tuple(rows), n_steps=n)


@dataclass(frozen=True)
class Diagnosis:
    """Why a walk failed — a consumer of the plan tree, holds, nogoods, and
    the pass journal (Stage D4), never a mechanism.

    ``verdict`` distinguishes the two honest failure claims: ``"unsolvable"``
    (every failure the search hit was structural — no steer moved the
    governing tag and the causal oracle named no path) from ``"not-found"``
    (the search was limited: a corridor diverged, recovery rounds ran out, or
    the global budget was exhausted — carrying the best partial plan size,
    the first failing edge, and the learned nogoods).  The walker is a
    planner, not a verifier: ``unsolvable`` is a diagnosis with a
    certificate, not a proof of unreachability.
    """

    verdict: str  # "unsolvable" | "not-found"
    reason: str
    failing_goal: tuple[str, Any] | None = None
    failure_kind: str | None = None
    blockers: tuple[tuple[str, Any], ...] = ()
    nogoods: tuple[str, ...] = ()
    partial_steps: int = 0
    notes: tuple[str, ...] = ()

    def __str__(self) -> str:
        lines = [f"Diagnosis: {self.verdict} — {self.reason}"]
        if self.failing_goal is not None:
            tag, value = self.failing_goal
            kind = f" ({self.failure_kind})" if self.failure_kind else ""
            lines.append(f"  first failing goal: {tag} -> {_format_value(value)}{kind}")
        if self.blockers:
            rendered = ", ".join(f"{t}={_format_value(v)}" for t, v in self.blockers)
            lines.append(f"  blocked by: {rendered}")
        if self.nogoods:
            lines.append(f"  learned nogoods: {len(self.nogoods)}")
            for ng in self.nogoods[:3]:
                lines.append(f"    {ng}")
        if self.partial_steps:
            lines.append(f"  best partial plan: {self.partial_steps} step(s)")
        for note in self.notes:
            lines.append(f"  note: {note}")
        return "\n".join(lines)


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
        return "\n".join(lines)

    def __repr__(self) -> str:
        return (
            f"Plan(reachable={self.reachable}, "
            f"target={self.target_tag}={self.target_value!r}, scans={self.total_scans})"
        )


@dataclass(frozen=True)
class Path:
    reachable: bool
    steps: tuple[ReachabilityStep, ...]
    total_changes: int
    total_scans: int
    reason: str | None = None
    tag_defaults: dict[str, Any] | None = None
    # External inputs that must stay held for the plan's goals to persist:
    # (input, value, goal tag the hold protects).  None when the planner did
    # not track holds (BFS paths) or the plan needs none.
    holds: tuple[tuple[str, Any, str], ...] | None = None
    # Triangle table over the plan (kernels, windows, divest points), derived
    # once at Path-build time.  None when the planner tracked no holds.
    triangle: TriangleTable | None = None
    # Failure diagnosis (walk paths only); None on success or legacy paths.
    diagnosis: Diagnosis | None = None
    debug_trace: Any = None
    # The route PILOT took to a Bool target, with the roads not taken as
    # redirectable pivots.  None on non-Bool targets and unreachable plans.
    # ``how()`` never reports ambiguous — it picks a default and names it here.
    route: RouteTaken | None = None
    # Full attempt log incl. reverted rounds (planner journey), populated only by
    # ``how(..., debug=True)``.  ``steps`` stays the clean, sequentially-replayable
    # path; ``journey`` is the "tried this, ejected, learned, retried" record and
    # is NOT replayable in sequence (attempts share overlapping scan spans).
    journey: tuple[ReachabilityStep, ...] | None = None

    def __str__(self) -> str:
        if not self.reachable:
            base = f"Unreachable: {self.reason}"
            if self.diagnosis is not None:
                base = base + "\n" + str(self.diagnosis)
            if self.debug_trace is not None:
                base += (
                    f"\n\n--- Debug Trace ({len(self.debug_trace)} events) ---\n{self.debug_trace}"
                )
            return base
        if not self.steps:
            return "Already at target state"
        lines = [f"Path ({len(self.steps)} step(s), {self.total_changes} input change(s)):"]
        prev_action: dict[str, Any] = {}
        for i, step in enumerate(self.steps, 1):
            if i == 1:
                inputs = _render_step_inputs(step, tag_defaults=self.tag_defaults)
            else:
                diff = _render_step_diff(step, prev_action)
                inputs = diff
            prev_action = step.action
            scans = f"  ({step.scans} scan(s))" if step.scans > 1 else ""
            if inputs:
                lines.append(f"  Step {i}: {inputs}{scans}")
            else:
                lines.append(f"  Step {i}: (wait){scans}")
        if self.route is not None and not self.route.dominant:
            lines.append(f"  Route: {self.route.label}")
            for pivot in self.route.salient_pivots:
                redirect = _render_pivot_redirect(pivot)
                if redirect:
                    lines.append(f"    {redirect}")
        if self.holds:
            rendered = ", ".join(
                f"{name}={_format_value(value)} (for {goal})" for name, value, goal in self.holds
            )
            lines.append(f"  Holds: {rendered}")
        if self.triangle is not None:
            divests = self.triangle.divest_points()
            if divests:
                rendered = ", ".join(
                    f"{r.name} at step {r.end} (was protecting {r.goal})" for r in divests
                )
                lines.append(f"  Divests: {rendered}")
        if self.debug_trace is not None:
            lines.append(f"\n--- Debug Trace ({len(self.debug_trace)} events) ---")
            lines.append(str(self.debug_trace))
        if self.journey is not None:
            lines.append(
                f"\n--- Journey ({len(self.journey)} step(s) tried; "
                f"{len(self.steps)} on the final path) ---"
            )
            for i, step in enumerate(self.journey, 1):
                inputs = (
                    ", ".join(f"{t}={_format_value(v)}" for t, v in sorted(step.action.items()))
                    or "(wait)"
                )
                scans = f"  ({step.scans} scan(s))" if step.scans > 1 else ""
                holds = (
                    f"  [holds: {', '.join(sorted(step.reactive_holds))}]"
                    if step.reactive_holds
                    else ""
                )
                lines.append(f"  Attempt {i}: {inputs}{scans}{holds}")
        return "\n".join(lines)

    def to_commands(self) -> list[str]:
        """Serialize the path as executable console commands."""
        if not self.reachable or not self.steps:
            return []
        commands: list[str] = []
        prev_action: dict[str, Any] = {}
        for step in self.steps:
            for tag in sorted(prev_action):
                if tag not in step.action:
                    commands.append(f"unforce {tag}")
            for tag in sorted(step.action):
                value = step.action[tag]
                if prev_action.get(tag) != value:
                    commands.append(f"force {tag} {_format_value(value)}")
            if step.scans:
                commands.append(f"step {step.scans}")
            prev_action = step.action
        commands.append("clear_forces")
        return commands

    def __repr__(self) -> str:
        return (
            f"Path(reachable={self.reachable}, steps={len(self.steps)}, "
            f"total_changes={self.total_changes}, total_scans={self.total_scans})"
        )
