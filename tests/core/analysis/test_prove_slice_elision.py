"""Regression coverage for the slice elision strategy.

These tests drive ``_elide_scan_local_stateful_dims`` directly to exercise
slice elision edge cases.
"""

from __future__ import annotations

import pytest

from pyrung.core import (
    Block,
    Bool,
    Int,
    Or,
    Program,
    Rung,
    TagType,
    calc,
    call,
    copy,
    fall,
    latch,
    out,
    reset,
    rise,
    subroutine,
)
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.prove import Counterexample, always
from pyrung.core.analysis.prove.elision import _elide_scan_local_stateful_dims


def _slice_elide(
    program: Program,
    stateful_dims: dict[str, tuple[object, ...]],
    nondeterministic_dims: dict[str, tuple[object, ...]],
    observed_tags: frozenset[str] = frozenset(),
) -> tuple[dict[str, tuple[object, ...]], dict[str, str]]:
    reduced, elided, _details, _subs = _elide_scan_local_stateful_dims(
        program,
        build_program_graph(program),
        stateful_dims,
        nondeterministic_dims,
        observed_tags=observed_tags,
    )
    return reduced, elided


def test_unconditional_write_before_read_is_elided() -> None:
    """A tag written unconditionally before any read is scan-local."""
    tmp = Bool("Tmp")
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung():
            reset(tmp)
        with Rung(tmp):
            out(seen)

    reduced, elided = _slice_elide(logic, {"Tmp": (False, True), "Seen": (False, True)}, {})

    assert "Tmp" in elided
    assert reduced == {}


def test_tag_with_no_readers_is_elided() -> None:
    """An unconditionally-written, never-read tag is scan-local."""
    trig = Bool("Trig", external=True)
    dump = Int("Dump", choices={0: "a", 1: "b"})

    with Program(strict=False) as logic:
        with Rung():
            copy(trig, dump)

    _reduced, elided = _slice_elide(logic, {"Dump": (0, 1)}, {"Trig": (False, True)})

    assert "Dump" in elided


def test_seal_in_flag_is_kept_stateful() -> None:
    """A seal-in coil reads its own entry value, so it is not scan-local."""
    start = Bool("Start", external=True)
    held = Bool("Held")

    with Program(strict=False) as logic:
        with Rung(Or(start, held)):
            out(held)

    reduced, elided = _slice_elide(logic, {"Held": (False, True)}, {"Start": (False, True)})

    assert "Held" not in elided
    assert reduced == {"Held": (False, True)}


def test_oneshot_write_does_not_hide_entry_read() -> None:
    """A oneshot copy goes inert on later scans, exposing an entry read.

    ``copy(src, n2, oneshot=True)`` writes ``N2`` only on first activation;
    afterwards ``Rung(N2)`` observes the scan-entry value, so ``N2`` must stay
    stateful.  The def-use fast path must not short-circuit this.
    """
    src = Int("Src", external=True, min=0, max=3)
    n2 = Int("N2", min=0, max=3)
    flag = Bool("Flag")

    with Program(strict=False) as logic:
        with Rung():
            copy(src, n2, oneshot=True)
        with Rung(n2):
            latch(flag)

    reduced, elided = _slice_elide(logic, {"N2": (0, 1, 2, 3)}, {"Src": (0, 1, 2, 3)})

    assert "N2" not in elided
    assert reduced == {"N2": (0, 1, 2, 3)}


@pytest.mark.parametrize("edge", [rise, fall])
def test_edge_contact_memory_exposes_entry_read(edge) -> None:
    """An edge contact turns on the bit it saw last scan, not only this one.

    ``fall(B)`` is True only when the contact's memory (``_edge:e1``) holds
    True and ``B`` is False, so the slice must try both memory values: with
    the memory left at its default False, ``fall(B)`` never fires, the copy
    never reads ``X`` before ``Rung(): copy(5, X)`` writes it, and ``X`` would
    look scan-local.  ``rise(B)`` fires from either memory value, so it is
    caught either way.
    """
    b = Bool("B", external=True)
    x = Int("X", min=0, max=5)
    y = Int("Y", min=0, max=5)

    with Program(strict=False) as logic:
        with Rung(edge(b)):
            copy(x, y)
        with Rung():
            copy(5, x)

    _reduced, elided = _slice_elide(logic, {"X": (0, 5), "Y": (0, 5)}, {"B": (False, True)})

    assert "X" not in elided


def test_subroutine_scratch_is_elided() -> None:
    """A scratch tag written then read within one subroutine is scan-local.

    The conditionally-called subroutine writes ``Scratch`` unconditionally
    before reading it; when the subroutine is skipped the tag is neither
    written nor read.  Caught by the subroutine fast path.
    """
    trigger = Bool("Trigger", external=True)
    scratch = Int("Scratch", choices={0: "idle", 1: "go"})
    result = Bool("Result")

    @subroutine("worker", strict=False)
    def worker() -> None:
        with Rung():
            copy(1, scratch)
        with Rung(scratch == 1):
            out(result)

    with Program(strict=False) as logic:
        with Rung(trigger):
            call(worker)

    _reduced, elided = _slice_elide(logic, {"Scratch": (0, 1)}, {"Trigger": (False, True)})

    assert "Scratch" in elided


def test_all_conditional_writers_can_all_skip_keeps_tag() -> None:
    """When every writer is gated and all gates can be false, entry is read."""
    req_a = Bool("ReqA", external=True)
    req_b = Bool("ReqB", external=True)
    pulse = Int("Pulse", choices={0: "no", 1: "yes"})
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung(req_a):
            copy(1, pulse)
        with Rung(req_b):
            copy(1, pulse)
        with Rung(pulse == 1):
            out(seen)

    reduced, elided = _slice_elide(
        logic,
        {"Pulse": (0, 1)},
        {"ReqA": (False, True), "ReqB": (False, True)},
    )

    assert "Pulse" not in elided
    assert reduced == {"Pulse": (0, 1)}


def test_strategy_toggle_dispatches_both_paths() -> None:
    """Both strategies honor the shared return contract."""
    tmp = Bool("Tmp")
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung():
            reset(tmp)
        with Rung(tmp):
            out(seen)

    graph = build_program_graph(logic)
    dims = {"Tmp": (False, True), "Seen": (False, True)}

    result = _elide_scan_local_stateful_dims(logic, graph, dims, {})
    assert len(result) == 4
    reduced, elided, details, subs = result
    assert isinstance(reduced, dict)
    assert isinstance(elided, dict)
    assert isinstance(details, dict)
    assert isinstance(subs, dict)


def _indexed_entry_read_program() -> tuple[Program, Bool]:
    """``DS[P]`` reads DS2 before the rung that writes it, but only at P = 2."""
    hit = Bool("Hit")
    k = Int("K", external=True, choices={0: "a", 7: "b"})
    p = Int("P", external=True, choices={0: "zero", 2: "two"})
    n = Int("N")
    ds = Block("DS", TagType.INT, 1, 4)

    with Program(strict=False) as logic:
        with Rung():
            copy(ds[p], n)
        with Rung():
            copy(k, ds[2])
        with Rung(n == 7):
            latch(hit)
    return logic, hit


def test_index_pointer_seeds_the_slice() -> None:
    """The pointer is enumerated, not fixed at its default 0, where DS2 looks unread."""
    logic, _hit = _indexed_entry_read_program()

    reduced, elided = _slice_elide(logic, {"DS2": (0, 7)}, {"K": (0, 7), "P": (0, 2)})

    assert "DS2" not in elided
    assert reduced == {"DS2": (0, 7)}


def test_index_entry_read_is_found() -> None:
    logic, hit = _indexed_entry_read_program()

    assert isinstance(always(logic, ~hit), Counterexample)


def _historian_program(shape: str) -> tuple[Program, Bool]:
    """A slot per id, reached through ``Idx = Hid * 10 + 9`` inside one subroutine.

    ``"write_then_read"`` writes the slot on both branches of ``Bit`` and reads
    it back; ``"read_first"`` reads it before writing; ``"compare_first"``
    compares it in a rung condition before writing it on both branches;
    ``"write_some"`` writes it only under ``Bit`` and reads it on every call.
    ``Hit`` latches when a call with ``Bit`` off sees 7.
    """
    go1, go2 = Bool("Go1", external=True), Bool("Go2", external=True)
    bit, hit = Bool("Bit", external=True), Bool("Hit")
    hid, idx, echo = Int("Hid"), Int("Idx"), Int("Echo")
    ds = Block("DS", TagType.INT, 1, 40)

    @subroutine("hist", strict=False)
    def hist() -> None:
        with Rung():
            calc(hid * 10 + 9, idx)
        if shape == "read_first":
            with Rung():
                copy(ds[idx], echo)
        if shape == "compare_first":
            with Rung(~bit, ds[idx] == 7):
                latch(hit)
        with Rung(bit):
            copy(7, ds[idx])
        if shape in ("write_then_read", "compare_first"):
            with Rung(~bit):
                copy(0, ds[idx])
        if shape != "read_first":
            with Rung():
                copy(ds[idx], echo)
        with Rung(~bit, echo == 7):
            latch(hit)

    with Program(strict=False) as logic:
        with Rung(go1):
            copy(1, hid)
            call(hist)
        with Rung(go2):
            copy(2, hid)
            call(hist)
    return logic, hit


_HISTORIAN_DIMS = {"DS19": (0, 7), "Hid": (0, 1, 2)}
_HISTORIAN_INPUTS = {"Bit": (False, True), "Go1": (False, True), "Go2": (False, True)}


def test_slot_written_then_read_in_each_call_is_elided() -> None:
    """The call sites' conditions don't matter: each call writes DS19 before reading it."""
    logic, _hit = _historian_program("write_then_read")

    _reduced, elided = _slice_elide(logic, dict(_HISTORIAN_DIMS), dict(_HISTORIAN_INPUTS))

    assert "DS19" in elided


def test_slot_read_before_its_write_is_kept() -> None:
    logic, _hit = _historian_program("read_first")

    _reduced, elided = _slice_elide(logic, dict(_HISTORIAN_DIMS), dict(_HISTORIAN_INPUTS))

    assert "DS19" not in elided


def test_slot_compared_before_its_write_is_kept() -> None:
    """A rung condition's read counts: running the body alone must see it too."""
    logic, hit = _historian_program("compare_first")

    _reduced, elided = _slice_elide(logic, dict(_HISTORIAN_DIMS), dict(_HISTORIAN_INPUTS))

    assert "DS19" not in elided
    assert isinstance(always(logic, ~hit), Counterexample)


def test_slot_a_call_reads_without_writing_is_kept() -> None:
    logic, _hit = _historian_program("write_some")

    _reduced, elided = _slice_elide(logic, dict(_HISTORIAN_DIMS), dict(_HISTORIAN_INPUTS))

    assert "DS19" not in elided


def test_observed_slot_is_kept() -> None:
    """A projection would see what DS19 kept from an earlier call."""
    logic, _hit = _historian_program("write_then_read")

    _reduced, elided = _slice_elide(
        logic, dict(_HISTORIAN_DIMS), dict(_HISTORIAN_INPUTS), frozenset({"DS19"})
    )

    assert "DS19" not in elided


def test_slot_kept_from_an_earlier_call_is_found() -> None:
    """A call with Bit on stores 7; a later one with Bit off echoes it."""
    logic, hit = _historian_program("write_some")

    assert isinstance(always(logic, ~hit), Counterexample)
