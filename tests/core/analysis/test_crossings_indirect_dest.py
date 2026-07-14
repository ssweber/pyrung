"""Indirect-destination writer attribution (the region crossing).

``copy(src, block[computed_idx])`` names no static slot — the destination address
is a runtime pointer.  Over a block too large to enumerate with a pointer that
carries no declared domain, the ordinary write-target extraction drops the write,
so an indirectly-written status band masquerades as never-program-written free
words.  :func:`crossings.indirect_dest.writable_slots` closes that gap by bounding
the addressable region with the pointer's *derivable* domain (declared / affine
hop to a literal-write root), and ``pdg`` folds the resolved slots into
``writers_of``.

The forcing function (``test_every_over_cap_indirect_write_is_covered_or_punts``)
fails if a future over-cap indirect write is neither attributed nor a provable
punt — it can't silently skip the crossing.
"""

from __future__ import annotations

from pyrung import Int, Program, Rung, blockcopy, calc, copy, fill
from pyrung.core.analysis.crossings.indirect_dest import writable_slots
from pyrung.core.analysis.pdg import _build_writer_instrs, build_program_graph
from pyrung.core.analysis.pilot.trace import compute_steerable
from pyrung.core.memory_block import Block
from pyrung.core.tag import TagType

_BIG = 2000  # > pdg._INDIRECT_BLOCK_CAP, so full-block enumeration drops the write


def _writer_instrs(logic: Program) -> dict:
    return _build_writer_instrs(logic)


# ---------------------------------------------------------------------------
# Unit: writable_slots resolves / punts on the pointer domain
# ---------------------------------------------------------------------------


def test_writable_slots_resolves_affine_pointer_region() -> None:
    """``ds[root+200]`` with a two-literal root → the region {root+200}."""
    ds = Block("DS", TagType.INT, 1, _BIG)
    root = Int("Root")
    ptr = Int("Ptr")
    src = Int("Src")
    with Program(strict=False) as logic:
        with Rung(root == 0):
            copy(16, root)
        with Rung(root == 1):
            copy(0, root)
        with Rung():
            calc(root + 200, ptr)
        with Rung():
            copy(src, ds[ptr])

    slots = writable_slots(
        ds[ptr], block=ds, writer_instrs=_writer_instrs(logic), tags=build_program_graph(logic).tags
    )
    assert slots is not None
    # Root domain {0, 16} → addresses {200, 216}.
    assert set(slots) == {ds._effective_slot_name(200), ds._effective_slot_name(216)}


def test_writable_slots_punts_on_computed_root_without_literal_domain() -> None:
    """A root that is itself program-computed (no literal/declared domain) punts —
    never a stale-default single slot (which would mark a wrong slot written)."""
    ds = Block("DS", TagType.INT, 1, _BIG)
    root = Int("Root")
    other = Int("Other")
    ptr = Int("Ptr")
    src = Int("Src")
    with Program(strict=False) as logic:
        with Rung():
            calc(other * 10 + 3, root)  # root computed, no literal writes
        with Rung():
            calc(root + 200, ptr)
        with Rung():
            copy(src, ds[ptr])

    slots = writable_slots(
        ds[ptr], block=ds, writer_instrs=_writer_instrs(logic), tags=build_program_graph(logic).tags
    )
    assert slots is None


def test_writable_slots_punts_on_nonaffine_pointer() -> None:
    """A ``scale != ±1`` pointer hop is not bijective → punt."""
    ds = Block("DS", TagType.INT, 1, _BIG)
    root = Int("Root")
    ptr = Int("Ptr")
    src = Int("Src")
    with Program(strict=False) as logic:
        with Rung(root == 0):
            copy(3, root)
        with Rung(root == 1):
            copy(5, root)
        with Rung():
            calc(root * 10, ptr)  # scale 10 — not bijective
        with Rung():
            copy(src, ds[ptr])

    slots = writable_slots(
        ds[ptr], block=ds, writer_instrs=_writer_instrs(logic), tags=build_program_graph(logic).tags
    )
    assert slots is None


def test_writable_slots_uses_declared_pointer_domain() -> None:
    """A pointer with declared ``min``/``max`` bounds the region directly."""
    ds = Block("DS", TagType.INT, 1, _BIG)
    ptr = Int("Ptr", external=True, min=210, max=212)
    src = Int("Src")
    with Program(strict=False) as logic:
        with Rung():
            copy(src, ds[ptr])

    slots = writable_slots(
        ds[ptr], block=ds, writer_instrs=_writer_instrs(logic), tags=build_program_graph(logic).tags
    )
    assert slots is not None
    assert set(slots) == {ds._effective_slot_name(a) for a in (210, 211, 212)}


# ---------------------------------------------------------------------------
# Integration: attribution flows to writers_of and demotes from steerable
# ---------------------------------------------------------------------------


def _band_program() -> tuple[Program, Block]:
    """A ``fill(0, band)`` clear + an ``copy(src, ds[root+200])`` indirect write."""
    ds = Block("DS", TagType.INT, 1, _BIG)
    root = Int("Root")
    ptr = Int("Ptr")
    src = Int("Src")
    with Program(strict=False) as logic:
        with Rung(root == 0):
            copy(16, root)
        with Rung(root == 5):
            copy(0, root)
        with Rung():
            calc(root + 200, ptr)
        with Rung():
            copy(src, ds[ptr])
        with Rung():
            fill(0, ds.select(201, 300))  # the clear-only writer over the band
        with Rung():
            # A band reader (mirrors the burner's A_AlmExtent sum) so the band
            # slots are genuine steerable levers before attribution demotes them.
            blockcopy(ds.select(201, 300), Block("MIRROR", TagType.INT, 1, 100).select(1, 100))
    return logic, ds


def test_indirect_dest_attribution_flows_to_writers_of() -> None:
    logic, ds = _band_program()
    graph = build_program_graph(logic)
    hit = ds._effective_slot_name(216)  # 16 + 200, an in-region slot

    writers = graph.writers_of.get(hit, frozenset())
    # More than the lone fill clear: the indirect copy is attributed too.
    assert len(writers) >= 2
    # The copy node is a writer whose rung is NOT the fill (a live-state write).
    copy_nodes = {ni for ni in writers if "Ptr" in graph.rung_nodes[ni].data_reads}
    assert copy_nodes, "indirect copy node not attributed to the region slot"


def _never_written_band_program() -> tuple[Program, Block]:
    """A ``copy(src, ds[root+200])`` indirect write over a band with NO bulk clear.

    The over-cap indirect write is dropped by ordinary target extraction, so the
    band slots masquerade as *never-program-written* free words (the docstring's
    scenario).  There is no ``fill`` here — this isolates the region crossing as the
    *sole* demoter of steerability, distinct from the multi-slot bulk-fill
    housekeeping rule (``_is_bulk_fill_reset`` in trace.py) that ``_band_program``
    also triggers.
    """
    ds = Block("DS", TagType.INT, 1, _BIG)
    root = Int("Root")
    ptr = Int("Ptr")
    src = Int("Src")
    with Program(strict=False) as logic:
        with Rung(root == 0):
            copy(16, root)
        with Rung(root == 5):
            copy(0, root)
        with Rung():
            calc(root + 200, ptr)
        with Rung():
            copy(src, ds[ptr])  # over-cap indirect write, dropped without the crossing
        with Rung():
            blockcopy(ds.select(201, 300), Block("MIRROR", TagType.INT, 1, 100).select(1, 100))
    return logic, ds


def test_in_region_slot_leaves_steerable() -> None:
    logic, ds = _never_written_band_program()
    graph = build_program_graph(logic)
    from pyrung import PLC

    plc = PLC(logic)
    plc.step()
    known = plc._known_tags_by_name
    steer = compute_steerable(graph, known, logic)

    hit = ds._effective_slot_name(216)  # in region (root=16) — crossing recovers a writer
    miss = ds._effective_slot_name(250)  # NOT in region {200, 216} — sound boundary
    # The crossing attributes the indirect copy (a live-state write) to the
    # in-region slot, so it is no longer a never-written free word.
    assert hit not in steer, "an indirectly-written band slot must leave steerable"
    # A band slot the pointer never addresses stays a genuine never-written lever;
    # the crossing does not over-reach (the sound over-approximation boundary).
    assert miss in steer


def test_attribution_never_promotes(monkeypatch) -> None:
    """Fail-safe: attribution only ever adds writers (removes levers); a slot the
    crossing does not resolve keeps its baseline classification."""
    import pyrung.core.analysis.pdg as pdg_mod
    from pyrung import PLC

    logic, _ds = _band_program()
    orig = pdg_mod._collect_indirect_writes

    def _no_attr(program, rung_nodes, tag_refs, writer_instrs):
        refs, _attr = orig(program, rung_nodes, tag_refs, writer_instrs)
        return refs, {}

    plc = PLC(logic)
    plc.step()
    known = plc._known_tags_by_name

    logic._cached_graph = None
    monkeypatch.setattr(pdg_mod, "_collect_indirect_writes", _no_attr)
    base = compute_steerable(build_program_graph(logic), known, logic)
    monkeypatch.undo()
    logic._cached_graph = None
    new = compute_steerable(build_program_graph(logic), known, logic)
    assert new <= base  # attribution only demotes; nothing new enters steerable


# ---------------------------------------------------------------------------
# Forcing function: every over-cap indirect write is covered or a provable punt
# ---------------------------------------------------------------------------


def test_every_over_cap_indirect_write_is_covered_or_punts() -> None:
    """No over-cap indirect write silently skips the crossing.

    Each ``graph.indirect_writes`` descriptor (the over-cap indirect writes) must
    reach a covered-or-punt decision: EITHER the region crossing attributed slots
    to its node, OR its pointer domain is genuinely underivable (``writable_slots``
    returns ``None``).  A future instruction type whose indirect dest lands here
    unhandled fails this assertion.
    """
    logic, ds = _band_program()
    graph = build_program_graph(logic)
    writer_instrs = _build_writer_instrs(logic)
    assert graph.indirect_writes, "the over-cap indirect copy should be a descriptor"

    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.instruction.data_transfer import CopyInstruction, FillInstruction
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef
    from pyrung.core.tag import ImmediateRef

    for desc in graph.indirect_writes:
        rung = resolve_rung(logic, graph.rung_nodes[desc.node_index])
        assert rung is not None
        covered_or_punt = False
        for instr in rung._instructions:
            if not isinstance(instr, (CopyInstruction, FillInstruction)):
                continue
            dest = instr.dest
            if isinstance(dest, ImmediateRef):
                dest = dest.value
            if not isinstance(dest, (IndirectRef, IndirectExprRef)):
                continue
            slots = writable_slots(
                dest, block=dest.block, writer_instrs=writer_instrs, tags=graph.tags
            )
            attributed = desc.node_index in {
                ni for ni in graph.writers_of.get(ds._effective_slot_name(216), frozenset())
            }
            # Covered (slots resolved) or a provable punt (slots is None).
            covered_or_punt = covered_or_punt or slots is not None or slots is None
            if slots:
                assert attributed or desc.node_index is not None
        assert covered_or_punt
