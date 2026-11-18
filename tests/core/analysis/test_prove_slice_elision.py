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
    TagType,
    Timer,
    call,
    copy,
    fall,
    latch,
    math,
    off_delay,
    on_delay,
    out,
    reset,
    rise,
    rung,
    search,
    subroutine,
    time_drum,
)
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.prove.elision import _elide_scan_local_stateful_dims
from pyrung.core.analysis.prove.elision.slice import _SliceElision


def _slice_elide(
    program: Program,
    stateful_dims: dict[str, tuple[object, ...]],
    nondeterministic_dims: dict[str, tuple[object, ...]],
    observed_tags: frozenset[str] = frozenset(),
) -> tuple[dict[str, tuple[object, ...]], dict[str, str]]:
    return _elide_scan_local_stateful_dims(
        program,
        build_program_graph(program),
        stateful_dims,
        nondeterministic_dims,
        observed=observed_tags,
    )


def test_unconditional_write_before_read_is_elided() -> None:
    """A tag written unconditionally before any read is scan-local."""
    tmp = Bool("Tmp")
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with rung():
            reset(tmp)
        with rung(tmp):
            out(seen)

    reduced, elided = _slice_elide(logic, {"Tmp": (False, True), "Seen": (False, True)}, {})

    assert "Tmp" in elided
    assert reduced == {}


def test_tag_with_no_readers_is_elided() -> None:
    """An unconditionally-written, never-read tag is scan-local."""
    trig = Bool("Trig", external=True)
    dump = Int("Dump", choices={0: "a", 1: "b"})

    with Program(strict=False) as logic:
        with rung():
            copy(trig, dump)

    _reduced, elided = _slice_elide(logic, {"Dump": (0, 1)}, {"Trig": (False, True)})

    assert "Dump" in elided


def test_seal_in_flag_is_kept_stateful() -> None:
    """A seal-in coil reads its own entry value, so it is not scan-local."""
    start = Bool("Start", external=True)
    held = Bool("Held")

    with Program(strict=False) as logic:
        with rung(Or(start, held)):
            out(held)

    reduced, elided = _slice_elide(logic, {"Held": (False, True)}, {"Start": (False, True)})

    assert "Held" not in elided
    assert reduced == {"Held": (False, True)}


def test_oneshot_write_does_not_hide_entry_read() -> None:
    """A oneshot copy goes inert on later scans, exposing an entry read.

    ``copy(src, n2, oneshot=True)`` writes ``N2`` only on first activation;
    afterwards ``rung(N2)`` observes the scan-entry value, so ``N2`` must stay
    stateful.  The def-use fast path must not short-circuit this.
    """
    src = Int("Src", external=True, min=0, max=3)
    n2 = Int("N2", min=0, max=3)
    flag = Bool("Flag")

    with Program(strict=False) as logic:
        with rung():
            copy(src, n2, oneshot=True)
        with rung(n2):
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
    never reads ``X`` before ``rung(): copy(5, X)`` writes it, and ``X`` would
    look scan-local.  ``rise(B)`` fires from either memory value, so it is
    caught either way.
    """
    b = Bool("B", external=True)
    x = Int("X", min=0, max=5)
    y = Int("Y", min=0, max=5)

    with Program(strict=False) as logic:
        with rung(edge(b)):
            copy(x, y)
        with rung():
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
        with rung():
            copy(1, scratch)
        with rung(scratch == 1):
            out(result)

    with Program(strict=False) as logic:
        with rung(trigger):
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
        with rung(req_a):
            copy(1, pulse)
        with rung(req_b):
            copy(1, pulse)
        with rung(pulse == 1):
            out(seen)

    reduced, elided = _slice_elide(
        logic,
        {"Pulse": (0, 1)},
        {"ReqA": (False, True), "ReqB": (False, True)},
    )

    assert "Pulse" not in elided
    assert reduced == {"Pulse": (0, 1)}


def _indexed_entry_read_program() -> tuple[Program, Bool]:
    """``DS[P]`` reads DS2 before the rung that writes it, but only at P = 2."""
    hit = Bool("Hit")
    k = Int("K", external=True, choices={0: "a", 7: "b"})
    p = Int("P", external=True, choices={0: "zero", 2: "two"})
    n = Int("N")
    ds = Block("DS", TagType.INT, 1, 4)

    with Program(strict=False) as logic:
        with rung():
            copy(ds[p], n)
        with rung():
            copy(k, ds[2])
        with rung(n == 7):
            latch(hit)
    return logic, hit


def test_index_pointer_seeds_the_slice() -> None:
    """The pointer is enumerated, not fixed at its default 0, where DS2 looks unread."""
    logic, _hit = _indexed_entry_read_program()

    reduced, elided = _slice_elide(logic, {"DS2": (0, 7)}, {"K": (0, 7), "P": (0, 2)})

    assert "DS2" not in elided
    assert reduced == {"DS2": (0, 7)}


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
        with rung():
            math(hid * 10 + 9, idx)
        if shape == "read_first":
            with rung():
                copy(ds[idx], echo)
        if shape == "compare_first":
            with rung(~bit, ds[idx] == 7):
                latch(hit)
        with rung(bit):
            copy(7, ds[idx])
        if shape in ("write_then_read", "compare_first"):
            with rung(~bit):
                copy(0, ds[idx])
        if shape != "read_first":
            with rung():
                copy(ds[idx], echo)
        with rung(~bit, echo == 7):
            latch(hit)

    with Program(strict=False) as logic:
        with rung(go1):
            copy(1, hid)
            call(hist)
        with rung(go2):
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


# ---------------------------------------------------------------------------
# Reads an instruction's own state decides
# ---------------------------------------------------------------------------

_PRESET_INPUTS = {"Run": (False, True), "Clear": (False, True), "Slow": (False, True)}


def _preset_box(kind: str, preset: Int) -> None:
    if kind == "drum":
        time_drum(
            outputs=[Bool("A"), Bool("B")],
            presets=[preset, 50],
            unit="ms",
            pattern=[[1, 0], [0, 1]],
            current_step=Int("Step"),
            accumulator=Int("Acc"),
            completion_flag=Bool("Done"),
        ).reset(Bool("Clear", external=True))
    elif kind == "off_delay":
        off_delay(Timer.clone("Purge"), preset)
    else:
        on_delay(Timer.clone("Dwell"), preset)


def _preset_program(kind: str, *, writes_above: bool) -> Program:
    """*kind*'s preset, which a rung of each value of ``Slow`` writes, above it or below it."""
    slow = Bool("Slow", external=True)
    preset = Int("Preset")

    def write() -> None:
        with rung(slow):
            copy(300, preset)
        with rung(~slow):
            copy(100, preset)

    with Program(strict=False) as logic:
        if writes_above:
            write()
        with rung(Bool("Run", external=True)):
            _preset_box(kind, preset)
        if not writes_above:
            write()
    return logic


@pytest.mark.parametrize("kind", ["drum", "off_delay"])
def test_preset_read_on_some_scans_is_kept(kind: str) -> None:
    """The box reads its preset only on scans its memory picks, which the check's scans never set.

    A drum reads it while its step times, and an off-delay while it times.  The rungs below
    write it every scan, so the box reads the value of the scan before.
    """
    logic = _preset_program(kind, writes_above=False)

    _reduced, elided = _slice_elide(logic, {"Preset": (0, 100, 300)}, dict(_PRESET_INPUTS))

    assert "Preset" not in elided


def test_drum_preset_set_in_its_subroutine_is_kept() -> None:
    """A call that writes no preset leaves the one the drum reads on its next timing scan."""
    preset = Int("Preset")

    @subroutine("mixer", strict=False)
    def mixer() -> None:
        with rung(Bool("Slow", external=True)):
            copy(300, preset)
        with rung(Bool("Run", external=True)):
            _preset_box("drum", preset)

    with Program(strict=False) as logic:
        with rung():
            call(mixer)

    _reduced, elided = _slice_elide(logic, {"Preset": (0, 300)}, dict(_PRESET_INPUTS))

    assert "Preset" not in elided


@pytest.mark.parametrize("kind", ["drum", "off_delay", "on_delay"])
def test_preset_written_above_each_scan_is_elided(kind: str) -> None:
    """A preset that a rung above the box writes on every scan holds that scan's value."""
    logic = _preset_program(kind, writes_above=True)

    _reduced, elided = _slice_elide(logic, {"Preset": (0, 100, 300)}, dict(_PRESET_INPUTS))

    assert "Preset" in elided


def test_drum_preset_written_first_each_scan_is_elided() -> None:
    """A rung with no condition above the drum writes its preset, so def-use chains elide it."""
    preset = Int("Preset")

    with Program(strict=False) as logic:
        with rung():
            copy(Int("HmiPreset", external=True, min=0, max=500), preset)
        with rung(Bool("Run", external=True)):
            _preset_box("drum", preset)

    dims = {"Preset": (0, 100, 500)}
    inputs = {**_PRESET_INPUTS, "HmiPreset": (0, 100, 500)}
    analyzer = _SliceElision(logic, build_program_graph(logic), dims, inputs)

    assert analyzer.candidate_elidable("Preset") == (True, "fast_path")


def test_search_table_filled_above_each_scan_is_elided() -> None:
    """A search reads the elements before its first match, and each scan fills them first."""
    table = Block("Tbl", TagType.INT, 1, 4)
    inputs: dict[str, tuple[object, ...]] = {"Look": (False, True)}

    with Program(strict=False) as logic:
        for i in range(1, 5):
            sel = Bool(f"Sel{i}", external=True)
            inputs[sel.name] = (False, True)
            with rung(sel):
                copy(i * 10, table[i])
            with rung(~sel):
                copy(0, table[i])
        with rung(Bool("Look", external=True)):
            search(table.select(1, 4) == 30, result=Int("At"), found=Bool("Hit"))

    dims = {f"Tbl{i}": (0, i * 10) for i in range(1, 5)}
    _reduced, elided = _slice_elide(logic, dims, inputs)

    assert set(dims) <= set(elided)


def test_continuous_search_result_is_kept() -> None:
    """A continuous search reads its last result to resume after it, before it writes one."""
    bins = Block("Bin", TagType.INT, 1, 3)

    with Program(strict=False) as logic:
        with rung():
            search(bins.select(1, 3) == 5, result=Int("At"), found=Bool("Hit"), continuous=True)
        with rung(Int("At") == 2):
            out(Bool("AtTwo"))

    _reduced, elided = _slice_elide(logic, {"At": (-1, 0, 1, 2, 3)}, {})

    assert "At" not in elided
