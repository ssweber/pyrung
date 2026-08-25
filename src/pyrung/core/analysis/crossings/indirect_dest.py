"""Indirect-destination writer attribution — the addressable-region crossing.

An indirect *destination* write ``copy(src, block[ptr])`` names no static slot:
the destination address is a runtime pointer value, so the ordinary write-target
extraction (``pdg._extract_write_targets``) either narrows by the pointer's
*declared* ``choices=`` / ``min``/``max`` or — failing that, over a block too
large to enumerate — drops the write entirely and keeps only an
``IndirectWriteRef`` for the recorded (runtime) walk.  The projected/static side
then sees the destination band as *never program-written*, so an alarm-status
word masquerades as a free operator lever.

This crossing closes that gap.  It is the DESTINATION twin of the indirect-SOURCE
reasoning in ``prove/classify`` (``_indirect_constant_table_index`` /
``_domain_from_indirect_source``) and reuses its soundness rule **verbatim**: the
addressable region is bounded by the POINTER's *derivable* value domain, hopping
an affine ``calc(root ± k)`` pointer back to a root register whose domain is
declared (``choices`` / ``min``/``max``), a literal-write set, or an
init-constant default.  Over-approximating that domain is sound for attribution —
a superset of write targets only ever *removes* a tag from the operator-lever set
(marks more slots program-written); it never invents a lever.  Where the pointer
domain is not derivable, the region is unbounded → **punt** (return ``None``, no
attribution — today's behavior).

Low layer: imports only instructions / memory_block / tag / expression, never
``pdg`` / ``prove`` / ``walk`` / ``pilot`` (they consume this).  ``pdg`` calls
:func:`writable_slots` in a post-pass to augment ``writers_of``.
"""

from __future__ import annotations

from typing import Any, TypeGuard

from pyrung.core.expression import BinaryExpr, LiteralExpr, TagExpr, UnaryExpr
from pyrung.core.instruction.calc import CalcInstruction
from pyrung.core.instruction.data_transfer import CopyInstruction
from pyrung.core.memory_block import Block, IndirectExprRef, IndirectRef
from pyrung.core.tag import Tag, TagType

#: Cap on the resolved region size — mirrors ``_declared_domain`` / the
#: ``> 1000`` guards in ``prove/classify`` so a wide declared range does not
#: enumerate a runaway slot list.
_REGION_CAP = 1000
#: Affine-hop depth bound (mirrors ``prove/classify._hop_affine_index``).
_MAX_HOPS = 3


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _forward_affine(instr: Any) -> tuple[str, int, int | float] | None:
    """``(source_tag, scale, offset)`` for ``dest = scale*source + offset``.

    The self-contained twin of ``prove/classify._extract_forward_affine`` (kept
    here so this low layer never imports ``prove``): an identity ``copy(src, D)``
    and a ``calc(src ± k)`` / ``calc(k ± src)`` / unary ``±src`` with ``scale ∈
    {1, -1}``.  Anything else → ``None``.
    """
    if isinstance(instr, CopyInstruction):
        if instr.convert is not None:
            return None
        src = instr.source
        name = getattr(src, "name", None)
        if name is not None and not isinstance(src, (IndirectRef, IndirectExprRef)):
            return (name, 1, 0)
        return None
    if isinstance(instr, CalcInstruction):
        return _affine_of_expr(instr.expression)
    return None


def _affine_of_expr(expr: Any) -> tuple[str, int, int | float] | None:
    """``(tag, scale, offset)`` when *expr* is an affine map over one tag."""
    if isinstance(expr, UnaryExpr):
        if isinstance(expr.operand, TagExpr):
            if expr.symbol == "+":
                return expr.operand.tag.name, 1, 0
            if expr.symbol == "-":
                return expr.operand.tag.name, -1, 0
        return None
    if not isinstance(expr, BinaryExpr) or expr.symbol not in ("+", "-"):
        return None
    left, right = expr.left, expr.right
    left_tag = left.tag.name if isinstance(left, TagExpr) else None
    right_tag = right.tag.name if isinstance(right, TagExpr) else None
    left_lit = left.value if isinstance(left, LiteralExpr) else None
    right_lit = right.value if isinstance(right, LiteralExpr) else None
    if left_tag is not None and _is_int(right_lit):
        return (left_tag, 1, right_lit if expr.symbol == "+" else -right_lit)
    if right_tag is not None and _is_int(left_lit):
        return (right_tag, 1 if expr.symbol == "+" else -1, left_lit)
    return None


def _hop_affine_index(
    idx_tag: str,
    writer_instrs: dict[str, list[Any]],
) -> tuple[str, int, int | float]:
    """Follow a single-writer bijective ``copy`` / ``calc(src ± k)`` pointer back
    to its root register, tracking address-as-a-function-of-root.

    Mirrors ``prove/classify._hop_affine_index``: only bijective affine hops
    (``scale ∈ {1, -1}``) are followed, bounded to three hops.  Returns
    ``(root, scale, offset)`` where the addressed slot is ``scale*root + offset``.
    """
    tag = idx_tag
    scale_acc: int = 1
    offset_acc: int | float = 0
    for _ in range(_MAX_HOPS):
        writers = writer_instrs.get(tag)
        if writers is None or len(writers) != 1:
            break
        fwd = _forward_affine(writers[0])
        if fwd is None:
            break
        source, scale, offset = fwd
        if source == tag or abs(scale) != 1:
            break
        offset_acc = scale_acc * offset + offset_acc
        scale_acc = scale_acc * scale
        tag = source
    return tag, scale_acc, offset_acc


def _declared_domain(tag: Tag | None) -> tuple[Any, ...] | None:
    """Finite explicit metadata domain (mirrors ``prove/classify._declared_domain``)."""
    if tag is None:
        return None
    if tag.type == TagType.BOOL:
        return (False, True)
    if tag.choices is not None:
        return tuple(sorted(tag.choices.keys()))
    if tag.min is None or tag.max is None:
        return None
    if not isinstance(tag.min, (int, float)) or not isinstance(tag.max, (int, float)):
        return None
    if int(tag.min) != tag.min or int(tag.max) != tag.max:
        return None
    if tag.max - tag.min + 1 > _REGION_CAP:
        return None
    return tuple(range(int(tag.min), int(tag.max) + 1))


def _pointer_affine(
    dest: IndirectRef | IndirectExprRef,
    writer_instrs: dict[str, list[Any]],
) -> tuple[str, int, int | float] | None:
    """Resolve ``(root, scale, offset)`` for the pointer of an indirect dest.

    A named pointer (``IndirectRef``) hops through ``writer_instrs``; an inline
    expression pointer (``IndirectExprRef`` — ``block[root ± k]``) reads the
    affine straight off the expression.  ``None`` when neither yields a bijective
    affine over a single root.
    """
    if isinstance(dest, IndirectRef):
        ptr_name = getattr(dest.pointer, "name", None)
        if ptr_name is None:
            return None
        root, scale, offset = _hop_affine_index(ptr_name, writer_instrs)
        return (root, scale, offset)
    # IndirectExprRef: the address IS the expression.  A bare tag is the identity
    # pointer; otherwise read the affine off the expression tree.
    expr = dest.expr
    if isinstance(expr, TagExpr):
        root, scale, offset = _hop_affine_index(expr.tag.name, writer_instrs)
        return (root, scale, offset)
    aff = _affine_of_expr(expr)
    if aff is None:
        return None
    root, scale, offset = aff
    # One further hop through the root's own affine writer (e.g. root is itself a
    # scratch computed from the real driver).
    r2, s2, o2 = _hop_affine_index(root, writer_instrs)
    return (r2, s2 * scale, s2 * offset + o2) if r2 != root else (root, scale, offset)


def _literal_write_values(root: str, writer_instrs: dict[str, list[Any]]) -> set[Any] | None:
    """The integer literal values *root* is ever assigned by its writers.

    ``None`` when any writer is non-literal (derives the value from live state) —
    the domain is then not a finite literal set, so the caller must not treat the
    literal values as complete.  Mirrors the ``prove/classify`` literal-domain
    collection (disqualify on a non-literal writer).
    """
    writers = writer_instrs.get(root)
    if not writers:
        return None
    values: set[Any] = set()
    for instr in writers:
        if isinstance(instr, CopyInstruction) and instr.convert is None:
            src = instr.source
        elif type(instr).__name__ == "FillInstruction":
            src = getattr(instr, "value", None)
        else:
            return None  # calc / non-literal writer → not a pure literal domain
        if _is_int(src):
            values.add(src)
        else:
            return None  # non-literal source → domain not a finite literal set
    return values or None


def writable_slots(
    dest: Any,
    *,
    block: Block,
    writer_instrs: dict[str, list[Any]],
    tags: dict[str, Tag],
) -> list[str] | None:
    """Slot names an indirect-dest write can target, or ``None`` (punt).

    *dest* is the ``IndirectRef`` / ``IndirectExprRef`` destination; *block* its
    block; *writer_instrs* maps every tag to its writer instructions (for the
    affine hop and the root's literal domain); *tags* the program's tag table.
    Returns the concrete slot names in the pointer's derivable region (a sound
    over-approximation), or ``None`` when the pointer domain cannot be bounded.
    """
    if not isinstance(dest, (IndirectRef, IndirectExprRef)):
        return None
    affine = _pointer_affine(dest, writer_instrs)
    if affine is None:
        return None
    root, scale, offset = affine
    if scale == 0:
        return None

    domain = _declared_domain(tags.get(root))
    if not domain:
        lits = _literal_write_values(root, writer_instrs)
        if lits:
            # Root written only by literals — that set is its derivable domain.
            domain = tuple(sorted(lits))
        elif root not in writer_instrs:
            # Genuinely never-written init constant (e.g. ``A_Alm{n}_ID`` with
            # ``default=n``): its default is its only value.  A root that HAS
            # writers but no literal/declared domain is program-computed and its
            # domain is not derivable — punt (never fall back to a stale default,
            # which would address a wrong slot and mark a real lever written).
            default = getattr(tags.get(root), "default", None)
            domain = (default,) if _is_int(default) else None
    if not domain:
        return None

    slots: list[str] = []
    seen: set[str] = set()
    for value in domain:
        if not _is_int(value):
            continue
        addr = scale * value + offset
        if not _is_int(addr) or addr < block.start or addr > block.end:
            continue
        if not block._is_sparse_address_valid(int(addr)):
            continue
        name = block._effective_slot_name(int(addr))
        if name not in seen:
            seen.add(name)
            slots.append(name)
        if len(slots) > _REGION_CAP:
            return None
    return slots or None


__all__ = ["writable_slots"]
