"""Expression helpers for prove state pruning."""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.simplified import And, Atom, Const, Expr, Or


def _build_atom_index(exprs: list[Expr]) -> dict[str, list[Atom]]:
    """Build a tag-name → atoms index in a single pass over all expressions."""
    index: dict[str, list[Atom]] = {}
    for expr in exprs:
        _index_atoms(expr, index)
    return index


def _index_atoms(expr: Expr, index: dict[str, list[Atom]]) -> None:
    if isinstance(expr, Atom):
        index.setdefault(expr.tag, []).append(expr)
        if isinstance(expr.operand, str):
            index.setdefault(expr.operand, []).append(expr)
    elif isinstance(expr, (And, Or)):
        for t in expr.terms:
            _index_atoms(t, index)


def _collect_atoms_for_tag(exprs: list[Expr], tag_name: str) -> list[Atom]:
    """Collect all Atom nodes referencing a specific tag from a list of expressions."""
    atoms: list[Atom] = []
    for expr in exprs:
        _walk_atoms(expr, tag_name, atoms)
    return atoms


def _walk_atoms(expr: Expr, tag_name: str, out: list[Atom]) -> None:
    if isinstance(expr, Atom):
        if expr.tag == tag_name or expr.operand == tag_name:
            out.append(expr)
    elif isinstance(expr, (And, Or)):
        for t in expr.terms:
            _walk_atoms(t, tag_name, out)


def _eval_atom(atom: Atom, value: Any) -> bool | None:
    """Evaluate a single atom against a concrete value.

    Returns None for rise/fall (needs prev — cannot evaluate statically).
    """
    form = atom.form
    if form == "xic":
        return bool(value)
    if form == "xio":
        return not bool(value)
    if form == "truthy":
        return bool(value)
    if form == "rise" or form == "fall":
        return None
    op = atom.operand
    if form == "eq":
        return value == op
    if form == "ne":
        return value != op
    if form == "lt":
        return value < op
    if form == "le":
        return value <= op
    if form == "gt":
        return value > op
    if form == "ge":
        return value >= op
    return None


def _partial_eval(expr: Expr, known: dict[str, Any]) -> Expr:
    """Substitute known tag values and simplify."""
    if isinstance(expr, Const):
        return expr

    if isinstance(expr, Atom):
        if expr.tag in known:
            eval_expr = expr
            if isinstance(expr.operand, str):
                if expr.operand not in known:
                    return expr
                eval_expr = Atom(expr.tag, expr.form, known[expr.operand])
            result = _eval_atom(eval_expr, known[expr.tag])
            if result is not None:
                return Const(result)
        return expr

    if isinstance(expr, And):
        terms: list[Expr] = []
        for t in expr.terms:
            evaled = _partial_eval(t, known)
            if isinstance(evaled, Const):
                if not evaled.value:
                    return Const(False)
                continue
            terms.append(evaled)
        if not terms:
            return Const(True)
        return And(tuple(terms)) if len(terms) > 1 else terms[0]

    if isinstance(expr, Or):
        terms = []
        for t in expr.terms:
            evaled = _partial_eval(t, known)
            if isinstance(evaled, Const):
                if evaled.value:
                    return Const(True)
                continue
            terms.append(evaled)
        if not terms:
            return Const(False)
        return Or(tuple(terms)) if len(terms) > 1 else terms[0]

    return expr


def _referenced_tags(expr: Expr) -> frozenset[str]:
    """Collect all tag names still referenced in an expression."""
    tags: set[str] = set()
    _walk_tags(expr, tags)
    return frozenset(tags)


def _walk_tags(expr: Expr, out: set[str]) -> None:
    if isinstance(expr, Atom):
        out.add(expr.tag)
        if isinstance(expr.operand, str):
            out.add(expr.operand)
    elif isinstance(expr, (And, Or)):
        for t in expr.terms:
            _walk_tags(t, out)


def _live_inputs(
    state: dict[str, Any],
    nd_dims: dict[str, tuple[Any, ...]],
    all_exprs: list[Expr],
    hidden_input_deps: dict[str, frozenset[str]] | None = None,
) -> frozenset[str]:
    """Determine which nondeterministic inputs are live at a given state."""
    nd_names = frozenset(nd_dims)
    known = {k: v for k, v in state.items() if k not in nd_names}
    hidden_deps = hidden_input_deps or {}

    live: set[str] = set()
    for expr in all_exprs:
        residual = _partial_eval(expr, known)
        if isinstance(residual, Const):
            continue
        refs = _referenced_tags(residual)
        live.update(refs & nd_names)
        for tag_name in refs - nd_names:
            live.update(hidden_deps.get(tag_name, ()))

    return frozenset(live)


def _has_edge_atom(expr: Expr, tag_name: str) -> bool:
    """True if *expr* contains a rise/fall atom for *tag_name*."""
    if isinstance(expr, Atom):
        return expr.tag == tag_name and expr.form in ("rise", "fall")
    if isinstance(expr, (And, Or)):
        return any(_has_edge_atom(t, tag_name) for t in expr.terms)
    return False


def _collect_edge_input_tags(
    exprs: list[Expr],
    nd_dims: dict[str, Any],
) -> frozenset[str]:
    """Return ND tag names that appear in rise()/fall() atoms."""
    result: set[str] = set()
    for expr in exprs:
        _collect_edge_atoms(expr, nd_dims, result)
    return frozenset(result)


def _collect_edge_atoms(
    expr: Expr,
    nd_dims: dict[str, Any],
    out: set[str],
) -> None:
    if isinstance(expr, Atom):
        if expr.form in ("rise", "fall") and expr.tag in nd_dims:
            out.add(expr.tag)
    elif isinstance(expr, (And, Or)):
        for t in expr.terms:
            _collect_edge_atoms(t, nd_dims, out)


def _walk_implicit_edge_inputs(
    program: Any,
    nd_dims: dict[str, Any],
) -> frozenset[str]:
    """Return ND tags referenced in implicit-edge instruction conditions.

    ShiftInstruction clock, drum jog/jump, and EventDrum per-step events
    use manual rising-edge detection via memory keys rather than rise()/fall()
    atoms.  ND inputs feeding those conditions are edge-bearing.
    """
    from pyrung.core.analysis.simplified import _condition_to_expr
    from pyrung.core.instruction.advanced import ShiftInstruction
    from pyrung.core.instruction.control import ForLoopInstruction
    from pyrung.core.instruction.drums import EventDrumInstruction, TimeDrumInstruction

    result: set[str] = set()
    nd_keys = frozenset(nd_dims)

    def _check(cond: Any) -> None:
        if cond is None:
            return
        refs = _referenced_tags(_condition_to_expr(cond))
        result.update(refs & nd_keys)

    def _walk(instructions: list[Any]) -> None:
        for instr in instructions:
            if isinstance(instr, ShiftInstruction):
                _check(instr.clock_condition)
            elif isinstance(instr, (EventDrumInstruction, TimeDrumInstruction)):
                _check(instr.jog_condition)
                _check(instr.jump_condition)
                if isinstance(instr, EventDrumInstruction):
                    for event_cond in instr.events:
                        _check(event_cond)
            if isinstance(instr, ForLoopInstruction) and hasattr(instr, "instructions"):
                _walk(instr.instructions)

    def _walk_rung(rung: Any) -> None:
        _walk(rung._instructions)
        for branch in rung._branches:
            _walk_rung(branch)

    for rung in program.rungs:
        _walk_rung(rung)
    for sub_name in program.subroutines:
        for rung in program.subroutines[sub_name]:
            _walk_rung(rung)

    return frozenset(result)


def _partition_edge_bearing_inputs(
    all_exprs: list[Expr],
    nd_dims: dict[str, tuple[Any, ...]],
    program: Any,
) -> frozenset[str]:
    """Partition ND inputs into edge-bearing (returned) and free (remainder).

    Edge-bearing inputs appear in rise()/fall() atoms or in implicit-edge
    instruction conditions (shift clock, drum jog/jump/events).  Their
    previous-scan value affects successor behaviour, so they must remain
    in the BFS state key.

    Free inputs can take any value on any scan — their current value does
    not constrain future behaviour, so omitting them from the state key
    merges equivalent states without under-approximation.
    """
    explicit = _collect_edge_input_tags(all_exprs, nd_dims)
    implicit = _walk_implicit_edge_inputs(program, nd_dims)
    return explicit | implicit
