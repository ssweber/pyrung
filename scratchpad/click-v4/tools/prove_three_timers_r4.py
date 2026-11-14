"""Three timers: b must be pressed while T1 is mid-run (an intermediate offset)."""
import sys
sys.path.insert(0, __import__("os").path.dirname(__file__))
from adv import check
from pyrung.core import Bool, Program, Rung, Timer, latch, on_delay, out
B2 = (False, True)
def three(tag, p1, p2, p3):
    def build():
        a = Bool(tag+"A", external=True); b = Bool(tag+"B", external=True)
        l1 = Bool(tag+"L1"); l2 = Bool(tag+"L2"); x = Bool(tag+"X"); y = Bool(tag+"Y"); h = Bool(tag+"H")
        t1 = Timer.clone(tag+"T1"); t2 = Timer.clone(tag+"T2"); t3 = Timer.clone(tag+"T3")
        with Program(strict=False) as p:
            with Rung(a): latch(l1)
            with Rung(b): latch(l2)
            with Rung(l1): on_delay(t1, preset=p1)
            with Rung(l1): on_delay(t3, preset=p3)
            with Rung(l2): on_delay(t2, preset=p2)
            with Rung(t1.Done, ~t2.Done): latch(x)
            with Rung(t2.Done, ~t3.Done): latch(y)
            with Rung(x, y): out(h)
        return p, h, {tag+"A": B2, tag+"B": B2}
    return build
i = 0
for p1, p2, p3 in ((100, 50, 120), (60, 30, 80), (80, 40, 100)):
    i += 1
    check(f"three {p1}/{p2}/{p3}", three(f"R{i}_", p1, p2, p3), depth=16)
