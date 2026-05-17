"""Reproducer: reachability cross-check — simulation state not in BFS set."""

from pyrung.core import (
    PLC, And, Block, Bool, Char, Counter, Dint, Int, Or, Program, Real, Rung,
    TagType, Timer, Word, blockcopy, branch, calc, call, copy, count_down, count_up,
    event_drum, fall, fill, forloop, latch, lro, lsh, off_delay, on_delay, out, pack_bits,
    pack_text, pack_words, receive, reset, return_early, rise, rro, rsh, search, shift,
    subroutine, time_drum,
    to_ascii, to_binary, to_text, to_value, unpack_to_bits, unpack_to_words,
)
from pyrung.core.analysis.prove import Intractable, reachable_states


def test_reproducer():
    In0 = Bool("In0", external=True)
    In1 = Bool("In1", external=True)
    B0 = Bool("B0")
    B1 = Bool("B1")
    N0 = Int("N0")
    D0 = Dint("D0")
    D1 = Dint("D1")
    W0 = Word("W0")
    Ch0 = Char("Ch0")
    T0 = Timer.clone("T0")
    T1 = Timer.clone("T1")
    C0 = Counter.clone("C0")
    C1 = Counter.clone("C1")
    DS = Block("DS", TagType.INT, 1, 5)
    CH = Block("CH", TagType.CHAR, 1, 6)

    with Program(strict=False) as logic:
        with Rung(~B0):
            out(B0, oneshot=True)
        with Rung(In0):
            count_up(C0, 10).down(B0).reset(B1)
        with Rung(In0):
            count_up(C1, 5).reset(C1.Done)
        with Rung(C1.Done):
            out(B0)

    projection = ['B0', 'B1', 'C0_Done', 'C1_Done']
    bfs_result = reachable_states(logic, project=projection,
                                  max_states=10_000, depth_budget=20)
    assert not isinstance(bfs_result, Intractable)

    plc = PLC(logic, dt=0.010)
    plc.patch({'In0': True, 'In1': False})
    plc.step()
    plc.patch({'In0': True, 'In1': False})
    plc.step()
    plc.patch({'In0': True, 'In1': False})
    plc.step()
    plc.patch({'In0': True, 'In1': True})
    plc.step()
    plc.patch({'In0': False, 'In1': False})
    plc.step()
    plc.patch({'In0': True, 'In1': True})
    plc.step()
    plc.patch({'In0': True, 'In1': False})
    plc.step()
    plc.patch({'In0': False, 'In1': False})
    plc.step()
    plc.patch({'In0': True, 'In1': True})
    plc.step()
    plc.patch({'In0': True, 'In1': True})
    plc.step()
    plc.patch({'In0': True, 'In1': True})
    plc.step()
    plc.patch({'In0': True, 'In1': True})
    plc.step()
    plc.patch({'In0': True, 'In1': True})
    plc.step()
    plc.patch({'In0': False, 'In1': True})
    plc.step()
    plc.patch({'In0': False, 'In1': False})
    plc.step()
    plc.patch({'In0': False, 'In1': True})
    plc.step()
    plc.patch({'In0': False, 'In1': False})
    plc.step()
    plc.patch({'In0': True, 'In1': True})
    plc.step()
    plc.patch({'In0': True, 'In1': True})
    plc.step()

    # Expected BFS set size: 2
    # Simulated state at scan 18: {'B1': False, 'C1_Done': False, 'B0': False, 'C0_Done': True}
    tags = plc.current_state.tags
    state = frozenset((name, tags[name]) for name in ['B0', 'B1', 'C0_Done', 'C1_Done'])
    assert state in bfs_result, (
        f"Simulation state not in BFS set: {dict(state)}"
    )
