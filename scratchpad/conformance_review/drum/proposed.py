"""Scratch: proposed drum cases, run in-process under pyrung (default) or the
CLICK-like model (--patch, from model_a.py)."""
import sys
from pyrung.conformance import Group, case, run
from pyrung.core import Or, Rung, copy, event_drum, out
from tests.conformance.memory import c
from tests.conformance.scans import count_scans, done, scans_for
from tests.conformance.test_drum import at_scan, first_on_from, last_on, prove_pause_holds_and_drops_jog, prove_reset_jump_jog_order

if "--patch" in sys.argv:
    import importlib.util, pathlib
    sys.argv.remove("--patch")
    src = pathlib.Path(__file__).with_name("model_a.py").read_text().split("if \"--patch\"")[0]
    ns = {}; exec(src, ns)
    from pyrung.core.instruction import drums as D
    D.EventDrumInstruction.execute = ns["execute"]

def prove_reset_no_stale_outputs(slot):
    # An event on scan 2 takes the drum to step 2 of 3 (not the last);
    # reset (C1956, also on while Cmd == 0) on scans 5 and 6.
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 2):
        out(c[1953])
    with Rung(Or(slot.Cmd == 0, slot.Scratch == 5, slot.Scratch == 6)):
        out(c[1956])
    with Rung(slot.Cmd != 0):
        event_drum(
            outputs=[c[1951], c[1952]],
            events=[c[1953], c[1954], c[1954]],
            pattern=[[1, 0], [0, 1], [1, 1]],
            current_step=slot.Result1,
            completion_flag=c[1955],
        ).reset(c[1956])
    at_scan(slot, 5, slot.Result1, slot.Result4)
    first_on_from(slot, c[1951], 3, slot.Result2)
    last_on(slot, c[1952], slot.Result3)
    done(slot)

def prove_completion_turns_outputs_off(slot):
    # Events on scans 2 and 3 complete the 2-step drum; jump to step 1
    # (C806) on scan 6.
    count_scans(slot)
    with Rung(slot.Cmd != 0):
        copy(1, slot.Result4)
    with Rung(slot.Cmd != 0, slot.Scratch == 2):
        out(c[803])
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        out(c[804])
    with Rung(slot.Cmd != 0, slot.Scratch == 6):
        out(c[806])
    with Rung(slot.Cmd != 0):
        event_drum(
            outputs=[c[801], c[802]],
            events=[c[803], c[804]],
            pattern=[[1, 0], [0, 1]],
            current_step=slot.Result1,
            completion_flag=c[805],
        ).reset(slot.Cmd == 0).jump(c[806], step=slot.Result4)
    last_on(slot, c[802], slot.Result2)
    first_on_from(slot, c[801], 3, slot.Result3)
    done(slot)

S = scans_for()
g = Group("drum", [
    case("reset", ladder=prove_reset_no_stale_outputs, expect={"Result1": 0, "Result2": 0, "Result3": 0, "Result4": 0}, scans=S),
    case("completion", ladder=prove_completion_turns_outputs_off, expect={"Result1": 0, "Result2": 0, "Result3": 0}, scans=S),
    case("order", ladder=prove_reset_jump_jog_order, expect={"Result1": 3, "Result2": 1, "Result3": 1}, scans=S),
    case("pause", ladder=prove_pause_holds_and_drops_jog, expect={"Result1": 1, "Result2": 3, "Result3": 4, "Result4": 6}, scans=S),
])
for r in run(g):
    print(r.case.id, r.passed, r.actual, r.note)
