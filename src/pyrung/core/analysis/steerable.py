"""Steerability — is the program the authoritative source of a tag's value?

One predicate, asked by anything that needs to know whether the ladder *owns* a
value or merely reads one the world supplies:

* :func:`compute_steerable` — tags the program does not author, so their value
  arrives from an operator, the field, a patch, or a force.
* :func:`compute_clear_only` — the ack-cleared subset: the program only ever
  resets them to rest, so the *active* value must come from outside.

Two callers want this for opposite reasons, and both are right:

* **PILOT** reads it as *"what may I command?"* — a steerable tag is a lever.
* **The hang-forever survey** reads it as *"what might never happen?"* — a
  steerable tag is precisely what the program cannot guarantee.  A guard clause
  on one is a clause the ladder cannot satisfy unaided.

That second reading is why this lives here rather than under ``pilot/``.  A
validator must not import the planner to ask a static question about the ladder.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.return_guards import _return_early_guard_exprs
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph

__all__ = ["compute_clear_only", "compute_steerable"]


def _literal_write(ro: Any, tag: str) -> Any | None:
    """The literal value rung *ro* writes to *tag*, or ``None``."""
    from pyrung.core.instruction.coils import (
        LatchInstruction,
        ResetInstruction,
        reset_value_for_type,
    )
    from pyrung.core.instruction.data_transfer import CopyInstruction, FillInstruction

    for instr in ro._instructions:
        target = getattr(instr, "target", None)
        if target is None:
            target = getattr(instr, "dest", None)
        if target is None:
            continue
        name = getattr(target, "name", None)
        if name is not None:
            names = {name}
        elif hasattr(target, "tags"):
            names = {getattr(t, "name", None) for t in target.tags()}
        else:
            continue
        if tag not in names:
            continue
        if isinstance(instr, ResetInstruction):
            if getattr(target, "type", None) is not None:
                return reset_value_for_type(target.type)
            if hasattr(target, "tags"):
                for member in target.tags():
                    if getattr(member, "name", None) == tag:
                        return reset_value_for_type(member.type)
            return None
        if isinstance(instr, LatchInstruction):
            return True
        if isinstance(instr, CopyInstruction):
            src = instr.source
            if hasattr(src, "name"):
                return getattr(src, "default", None) if getattr(src, "readonly", False) else None
            return src if isinstance(src, (bool, int, float, str)) else None
        if isinstance(instr, FillInstruction):
            val = instr.value
            if hasattr(val, "name"):
                return None
            return val if isinstance(val, (bool, int, float, str)) else None
        return None
    return None


def _rung_unconditional(rung_node: Any, pdg: ProgramGraph, program: Any, _depth: int = 0) -> bool:
    """Whether *rung_node* fires on every scan — no gate, no upstream return_early,
    and (if in a subroutine) reached only through unconditional callers."""
    if rung_node.condition_reads or _return_early_guard_exprs(program, rung_node):
        return False
    if rung_node.subroutine is None:
        return True
    if _depth > 3:
        return False  # give up conservatively on deep call chains
    callers = [cn for cn in pdg.rung_nodes if rung_node.subroutine in cn.calls]
    return bool(callers) and all(
        _rung_unconditional(cn, pdg, program, _depth + 1) for cn in callers
    )


def _is_bulk_fill_reset(ro: Any, tag: str) -> bool:
    """Whether *ro* writes *tag* only as a member of a multi-slot ``fill`` band.

    ``fill(0, ds.select(201, 350))`` zeroes a whole status band; the write to any
    one member is bulk housekeeping, not a targeted per-register reset.  A
    single-slot ``fill`` (``fill(0, one_word)``) is a targeted reset and returns
    ``False``.  An indirect/unresolvable range is left to the caller (returns
    ``False`` — no positive housekeeping signal)."""
    from pyrung.core.instruction.data_transfer import FillInstruction

    for instr in ro._instructions:
        if not isinstance(instr, FillInstruction):
            continue
        dest = getattr(instr, "dest", None)
        if dest is None or not hasattr(dest, "tags"):
            continue
        try:
            names = {getattr(dt, "name", None) for dt in dest.tags()}
        except Exception:
            continue
        if tag in names and len(names) > 1:
            return True
    return False


def _clear_only_command(tag: str, t: Any, pdg: ProgramGraph, program: Any) -> bool:
    """Every writer merely clears *tag* to its OFF/rest value — the ack-cleared idiom.

    ``reset()`` on a Bool, ``copy(0, flag)`` / ``fill(0, flag)`` on an Int/Word: the
    program never asserts the active value, so it must come from outside — a
    *momentary* operator/field command (``C_Clear``, ``C_UnitModeChgRequest``,
    ``Heat_xInit``).  Requires the tag be program-written *and* read; an out coil or
    non-literal (live-state) write means the program authors it, so not clear-only.
    Sound without ``external`` and even under an unconditional clear every scan.

    The ack idiom clears a *specific* register.  A writer that resets the tag only
    as one member of a **multi-slot bulk fill** (``fill(0, ds.select(201, 350))``
    zeroing a whole alarm/status band) is the program's own housekeeping, not an
    operator command the field supplies the active value for — so it does not make
    the tag clear-only (:func:`_is_bulk_fill_reset`).  A single-slot fill is still a
    targeted per-register reset and qualifies.
    """
    writers = pdg.writers_of.get(tag, frozenset())
    if not writers or not pdg.readers_of.get(tag, frozenset()):
        return False
    from pyrung.core.instruction.coils import reset_value_for_type

    tag_ref = t if t is not None else pdg.tags.get(tag)
    if tag_ref is None:
        return False
    rest_value = reset_value_for_type(tag_ref.type)
    for ri in writers:
        rung_node = pdg.rung_nodes[ri]
        if tag in rung_node.ote_writes:
            return False
        ro = resolve_rung(program, rung_node)
        if ro is None:
            return False
        if _is_bulk_fill_reset(ro, tag):
            # Only reset as a member of a multi-slot bulk fill — the program's
            # own housekeeping, not an operator ack of this specific register.
            return False
        lw = _literal_write(ro, tag)
        if lw is None or not _values_match(lw, rest_value):
            return False
    return True


def _operator_interface(
    tag: str,
    t: Any,
    pdg: ProgramGraph,
    program: Any,
) -> bool:
    """Whether *tag* is an operator/field-chosen interface the program only nudges.

    The type-independent core of steerability — identical terms for
    bool/int/dint/word/real/char.  A tag qualifies when the program is **not** its
    authoritative source of value, one of three ways:

    * **never program-written** — a pure input; its value comes from outside the
      program (operator / field / patch / force).  Steerable in any type, provided
      it is read somewhere (a wholly-unused declaration is not a lever).
    * **clear-only (ack-cleared)** — every writer merely clears it to its
      OFF/rest value (``reset()`` on a Bool, ``copy(0, flag)`` on an Int/Word).
      The program never asserts the active value, so that value must come from
      outside — the acknowledge pattern (PackML command bits like ``C_Clear`` /
      ``C_UnitModeChgRequest``).  Steerable in any type regardless of ``external``,
      and even when the clear is unconditional every scan.
    * **externally declared and only nudged** — ``external=True`` and every writer
      stamps a literal (any value) under a condition, so the operator's value
      persists between the program's nudges.

    A writer that derives the value from live state (a non-literal write) or drives
    the tag through an ``out`` coil means the program authors it — the interface is
    upstream, not here.  For the external-nudge arm an *unconditional* clobber also
    disqualifies (the program owns the rest state); the clear-only arm is exempt,
    since resetting to rest is precisely what an ack-cleared command expects.
    """
    writers = pdg.writers_of.get(tag, frozenset())
    if not writers:
        # Pure input: chosen entirely outside the program.  Require a reader so a
        # wholly-unused declaration is not surfaced as a phantom lever.
        return bool(pdg.readers_of.get(tag, frozenset()))
    if not pdg.readers_of.get(tag, frozenset()):
        return False
    if _clear_only_command(tag, t, pdg, program):
        # Program only ever resets it to rest; the operator/field supplies the
        # active value.  An ack-cleared command interface — steerable in any type,
        # external or not, unconditional clear or not.
        return True
    # External-nudge arm: every writer stamps a literal (not necessarily the
    # default) under a condition, so the operator's value persists between the
    # program's nudges.  An out coil / non-literal write (program authors the
    # value) disqualifies, and only an externally declared register with no
    # unconditional every-scan clobber qualifies.
    for ri in writers:
        rung_node = pdg.rung_nodes[ri]
        if tag in rung_node.ote_writes:
            return False  # out-coil driven: a computed output, not a nudge
        ro = resolve_rung(program, rung_node)
        if ro is None or _literal_write(ro, tag) is None:
            return False  # derives from live state — the program authors it
    if not getattr(t, "external", False):
        return False
    return not any(_rung_unconditional(pdg.rung_nodes[ri], pdg, program) for ri in writers)


def compute_steerable(
    pdg: ProgramGraph,
    known: dict[str, Any],
    program: Any,
) -> frozenset[str]:
    """Tags the program does not author, by intrinsic characteristics — any type.

    A tag is steerable when its value is an operator/field-chosen interface the
    program does not author each scan (see :func:`_operator_interface`).  Read-only
    and system tags (``rtc.*``, ``sys.*``) are never steerable.

    Read as *"what may I command?"* this is a lever set.  Read as *"what might
    never happen?"* it is the set of values the ladder cannot guarantee — the two
    faces of the same fact.  Callers wanting only genuine program constants that
    seed lookup-table pointers must subtract those separately
    (``pilot.trace.compute_reference_constants``, a drive-layer concern).
    """
    from pyrung.core.system_points import READ_ONLY_SYSTEM_TAG_NAMES

    out: set[str] = set()
    for tag in set(pdg.readers_of) | set(pdg.writers_of):
        if tag in READ_ONLY_SYSTEM_TAG_NAMES:
            continue
        if getattr(known.get(tag), "readonly", False):
            continue
        if _operator_interface(tag, known.get(tag), pdg, program):
            out.add(tag)
    return frozenset(out)


def compute_clear_only(
    pdg: ProgramGraph,
    known: dict[str, Any],
    program: Any,
) -> frozenset[str]:
    """Clear-only (ack-cleared momentary) command tags — the pulse-treatment set.

    A subset of :func:`compute_steerable`: every writer only ever resets the tag to
    rest (:func:`_clear_only_command`).  The program's own clear declares the idiom
    is pulse-and-release.  Read-only / system tags are excluded, mirroring
    :func:`compute_steerable`.
    """
    from pyrung.core.system_points import READ_ONLY_SYSTEM_TAG_NAMES

    out: set[str] = set()
    for tag in set(pdg.readers_of) | set(pdg.writers_of):
        if tag in READ_ONLY_SYSTEM_TAG_NAMES:
            continue
        if getattr(known.get(tag), "readonly", False):
            continue
        if _clear_only_command(tag, known.get(tag), pdg, program):
            out.add(tag)
    return frozenset(out)
