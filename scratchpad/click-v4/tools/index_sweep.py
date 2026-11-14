"""Indexed-access sweep: prover verdict vs brute force for block[ptr], block[ptr + k], block[k - ptr].

Run: uv run --no-sync python scratchpad/click-v4/tools/index_sweep.py
On 3d3901e every case but d was a wrong Proven; after the affine fix b, e and j remain
(unbounded pointer reads, BUILD-PLAN open queue). k is malformed (Ptr's default 0 is below min=3).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from adv import check
from pyrung.core import Block, Bool, Int, Program, Rung, TagType, calc, copy, fill, latch, out

def a_bounded_offset():
    Start, Hit = Bool("Start"), Bool("Hit")
    Ptr = Int("Ptr", min=0, max=1)
    DS = Block("DS", TagType.INT, 1, 4)
    N = Int("N")
    with Program(strict=False) as logic:
        with Rung(Start):
            fill(7, DS.select(3, 4))
        with Rung():
            copy(DS[Ptr + 3], N)
        with Rung(N == 7):
            latch(Hit)
    return logic, Hit, {"Start": [False, True], "Ptr": [0, 1]}

def b_unbounded_shift():
    Start, Hit = Bool("Start"), Bool("Hit")
    Ptr = Int("Ptr")
    DS = Block("DS", TagType.INT, 1, 4)
    N = Int("N")
    with Program(strict=False) as logic:
        with Rung(Start):
            copy(7, DS[1])
        with Rung():
            copy(DS[Ptr + 3], N)
        with Rung(N == 7):
            latch(Hit)
    return logic, Hit, {"Start": [False, True], "Ptr": [-2, 0, 1]}

def c_negative_scale():
    Start, Hit = Bool("Start"), Bool("Hit")
    Ptr = Int("Ptr", min=0, max=1)
    DS = Block("DS", TagType.INT, 1, 4)
    N = Int("N")
    with Program(strict=False) as logic:
        with Rung(Start):
            fill(7, DS.select(3, 4))
        with Rung():
            copy(DS[4 - Ptr], N)
        with Rung(N == 7):
            latch(Hit)
    return logic, Hit, {"Start": [False, True], "Ptr": [0, 1]}

def d_indirect_write_offset():
    Start, Hit = Bool("Start"), Bool("Hit")
    Ptr = Int("Ptr", min=0, max=1)
    DS = Block("DS", TagType.INT, 1, 4)
    N = Int("N")
    with Program(strict=False) as logic:
        with Rung(Start):
            copy(7, DS[Ptr + 3])
        with Rung(DS[4] == 7):
            latch(Hit)
    return logic, Hit, {"Start": [False, True], "Ptr": [0, 1]}


def e_combinational_pointer():
    Start, Hit = Bool("Start"), Bool("Hit")
    In = Int("In", min=0, max=1)
    Ptr = Int("Ptr")
    Q = Int("Q", choices={1: "one"})
    N = Int("N")
    DS = Block("DS", TagType.INT, 1, 4)
    with Program(strict=False) as logic:
        with Rung(Start):
            fill(7, DS.select(3, 4))
        with Rung():
            copy(DS[Q], N)
        with Rung():
            calc(In + 3, Ptr)
        with Rung():
            copy(DS[Ptr], N)
        with Rung(N == 7):
            latch(Hit)
    return logic, Hit, {"Start": [False, True], "In": [0, 1]}

def f_two_offsets():
    Start, Hit = Bool("Start"), Bool("Hit")
    Ptr = Int("Ptr", min=1, max=2)
    N, M = Int("N"), Int("M")
    DS = Block("DS", TagType.INT, 1, 4)
    with Program(strict=False) as logic:
        with Rung(Start):
            fill(7, DS.select(3, 4))
        with Rung():
            copy(DS[Ptr], N)
        with Rung():
            copy(DS[Ptr + 2], M)
        with Rung(M == 7):
            latch(Hit)
    return logic, Hit, {"Start": [False, True], "Ptr": [1, 2]}

def g_nonaffine():
    Start, Hit = Bool("Start"), Bool("Hit")
    Ptr = Int("Ptr", min=1, max=2)
    N = Int("N")
    DS = Block("DS", TagType.INT, 1, 4)
    with Program(strict=False) as logic:
        with Rung(Start):
            fill(7, DS.select(3, 4))
        with Rung():
            copy(DS[Ptr * 2], N)
        with Rung(N == 7):
            latch(Hit)
    return logic, Hit, {"Start": [False, True], "Ptr": [1, 2]}


def h_plain_pointer():
    Start, Hit = Bool("Start"), Bool("Hit")
    Ptr = Int("Ptr", min=3, max=4)
    N = Int("N")
    DS = Block("DS", TagType.INT, 1, 6)
    with Program(strict=False) as logic:
        with Rung(Start):
            fill(7, DS.select(3, 4))
        with Rung():
            copy(DS[Ptr], N)
        with Rung(N == 7):
            latch(Hit)
    return logic, Hit, {"Start": [False, True], "Ptr": [3, 4]}

def i_plain_pointer_written():
    Start, Hit = Bool("Start"), Bool("Hit")
    Ptr = Int("Ptr", min=3, max=4)
    N = Int("N")
    DS = Block("DS", TagType.INT, 1, 6)
    with Program(strict=False) as logic:
        with Rung(Start):
            copy(7, DS[4])
        with Rung():
            copy(DS[Ptr], N)
        with Rung(N == 7):
            latch(Hit)
    return logic, Hit, {"Start": [False, True], "Ptr": [3, 4]}


def j_plain_unbounded():
    Start, Hit = Bool("Start"), Bool("Hit")
    Ptr = Int("Ptr")
    N = Int("N")
    DS = Block("DS", TagType.INT, 1, 4)
    with Program(strict=False) as logic:
        with Rung(Start):
            copy(7, DS[1])
        with Rung():
            copy(DS[Ptr], N)
        with Rung(N == 7):
            latch(Hit)
    return logic, Hit, {"Start": [False, True], "Ptr": [0, 1]}

def k_combinational_bounded():
    Start, Hit = Bool("Start"), Bool("Hit")
    In = Int("In", min=0, max=1)
    Ptr = Int("Ptr", min=3, max=4)
    N = Int("N")
    DS = Block("DS", TagType.INT, 1, 4)
    with Program(strict=False) as logic:
        with Rung(Start):
            fill(7, DS.select(3, 4))
        with Rung():
            calc(In + 3, Ptr)
        with Rung():
            copy(DS[Ptr], N)
        with Rung(N == 7):
            latch(Hit)
    return logic, Hit, {"Start": [False, True], "In": [0, 1]}


if __name__ == "__main__":
    for name, build in list(globals().items()):
        if len(name) > 2 and name[1] == "_" and callable(build) and name[0] in "abcdefghijk":
            check(name, build, depth=6)
