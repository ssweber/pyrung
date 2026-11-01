"""The live trace shows each region of a rung as the scan left it."""

from __future__ import annotations

from pyrung.core import PLC, Bool, Program, Rung, branch, call, out, subroutine
from pyrung.dap.adapter import _settled_rung_event


def _settled_regions(plc: PLC) -> list[tuple[str, int | None, str]]:
    for _ in plc.debug.scan_steps_debug():
        pass
    event = _settled_rung_event(plc.debug.rung_trace(0).events)
    assert event.trace is not None
    return [
        (region.kind, region.source.source_line, region.enabled_state)
        for region in event.trace.regions
    ]


def test_branches_sharing_a_source_line_keep_their_own_state() -> None:
    """Two branches built by one helper share a source span; each keeps its
    own solved state, and a branch's own branches are shown too."""
    go, a, b, c = Bool("Go"), Bool("A"), Bool("B"), Bool("C")
    y1, y2, y3 = Bool("Y1"), Bool("Y2"), Bool("Y3")

    def leg(condition: Bool, coil: Bool) -> None:
        with branch(condition):
            out(coil)

    with Program(strict=False) as prog:
        with Rung(go):
            leg(a, y1)
            leg(b, y2)
            with branch(c):
                with branch(y1):
                    out(y3)

    rung = prog.rungs[0]
    first_leg, second_leg, outer = rung._branches
    assert first_leg.source_line == second_leg.source_line
    plc = PLC(prog)
    plc.patch({"Go": True, "A": True, "B": False, "C": True})
    assert _settled_regions(plc) == [
        ("rung", rung.source_line, "enabled"),
        ("branch", first_leg.source_line, "enabled"),
        ("branch", second_leg.source_line, "disabled_local"),
        ("branch", outer.source_line, "enabled"),
        ("branch", outer._branches[0].source_line, "enabled"),
    ]


def test_unpowered_rung_shows_its_branches_off() -> None:
    go, a, y = Bool("Go"), Bool("A"), Bool("Y")
    with Program(strict=False) as prog:
        with Rung(go):
            with branch(a):
                out(y)

    rung = prog.rungs[0]
    row = rung._branches[0]
    plc = PLC(prog)
    plc.patch({"Go": False, "A": True})
    assert _settled_regions(plc) == [
        ("rung", rung.source_line, "disabled_local"),
        ("branch", row.source_line, "disabled_parent"),
    ]


def test_a_called_subroutine_adds_no_regions_to_the_caller() -> None:
    go, a, y, z = Bool("Go"), Bool("A"), Bool("Y"), Bool("Z")
    with Program(strict=False) as prog:
        with Rung(go):
            call("Sub")
            with branch(a):
                out(y)
        with subroutine("Sub"):
            with Rung(a):
                with branch(go):
                    out(z)

    rung = prog.rungs[0]
    row = rung._branches[0]
    plc = PLC(prog)
    plc.patch({"Go": True, "A": True})
    assert _settled_regions(plc) == [
        ("rung", rung.source_line, "enabled"),
        ("branch", row.source_line, "enabled"),
    ]
