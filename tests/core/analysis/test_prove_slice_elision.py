"""Regression coverage for the slice elision strategy.

These tests drive ``_elide_scan_local_stateful_dims`` directly to exercise
slice elision edge cases.
"""

from __future__ import annotations

from pyrung.core import (
    Bool,
    Int,
    Or,
    Program,
    Rung,
    call,
    copy,
    latch,
    out,
    reset,
    subroutine,
)
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.prove.elision import _elide_scan_local_stateful_dims


def _slice_elide(
    program: Program,
    stateful_dims: dict[str, tuple[object, ...]],
    nondeterministic_dims: dict[str, tuple[object, ...]],
) -> tuple[dict[str, tuple[object, ...]], dict[str, str]]:
    reduced, elided, _details, _subs = _elide_scan_local_stateful_dims(
        program,
        build_program_graph(program),
        stateful_dims,
        nondeterministic_dims,
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
