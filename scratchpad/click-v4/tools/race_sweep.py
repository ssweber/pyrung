"""Timer races vs brute force: stop points must find every reachable window."""
import sys

sys.path.insert(0, __import__("os").path.dirname(__file__))
from adv import check  # noqa: E402

from pyrung.core import Bool, Counter, Program, Rung, Timer, count_up, latch, on_delay, out  # noqa: E402

B2 = (False, True)
n = [0]


def three(p1, p2, p3, unit="Tms", retentive=False):
    def build():
        n[0] += 1
        tag = f"S{n[0]}_"
        a = Bool(tag + "A", external=True)
        b = Bool(tag + "B", external=True)
        l1, l2, x, y, h = (Bool(tag + s) for s in ("L1", "L2", "X", "Y", "H"))
        t1, t2, t3 = (Timer.clone(tag + s) for s in ("T1", "T2", "T3"))
        with Program(strict=False) as p:
            with Rung(a):
                latch(l1)
            with Rung(b):
                latch(l2)
            with Rung(l1):
                on_delay(t1, preset=p1, unit=unit)
            with Rung(l1):
                on_delay(t3, preset=p3, unit=unit)
            with Rung(l2):
                if retentive:
                    on_delay(t2, preset=p2, unit=unit).reset(a)
                else:
                    on_delay(t2, preset=p2, unit=unit)
            with Rung(t1.Done, ~t2.Done):
                latch(x)
            with Rung(t2.Done, ~t3.Done):
                latch(y)
            with Rung(x, y):
                out(h)
        return p, h, {tag + "A": B2, tag + "B": B2}

    return build


def counter_race(p1, p2, p3):
    """A level counter (one count a scan) started by B races two timers."""

    def build():
        n[0] += 1
        tag = f"C{n[0]}_"
        a = Bool(tag + "A", external=True)
        b = Bool(tag + "B", external=True)
        l1, l2, x, y, h = (Bool(tag + s) for s in ("L1", "L2", "X", "Y", "H"))
        t1, t3 = Timer.clone(tag + "T1"), Timer.clone(tag + "T3")
        c = Counter.clone(tag + "C")
        with Program(strict=False) as p:
            with Rung(a):
                latch(l1)
            with Rung(b):
                latch(l2)
            with Rung(l1):
                on_delay(t1, preset=p1)
            with Rung(l1):
                on_delay(t3, preset=p3)
            with Rung(l2):
                count_up(c, preset=p2).reset(a)
            with Rung(t1.Done, ~c.Done):
                latch(x)
            with Rung(c.Done, ~t3.Done):
                latch(y)
            with Rung(x, y):
                out(h)
        return p, h, {tag + "A": B2, tag + "B": B2}

    return build


cases = [
    ("three 100/50/120", three(100, 50, 120), 16),
    ("three 60/30/80", three(60, 30, 80), 16),
    ("three 80/40/100", three(80, 40, 100), 16),
    ("three 100/50/110 (1-scan window)", three(100, 50, 110), 16),
    ("three 100/50/100 (no window)", three(100, 50, 100), 16),
    ("three 100/90/120", three(100, 90, 120), 18),
    ("three 70/20/80", three(70, 20, 80), 14),
    ("three retentive T2", three(100, 50, 120, retentive=True), 16),
    ("counter race 10/5/12", counter_race(100, 5, 120), 16),
    ("counter race 10/5/11", counter_race(100, 5, 110), 16),
]
flags = []
for name, build, depth in cases:
    _v, _gt, flag = check(name, build, depth=depth)
    flags.append((name, flag))
bad = [f for f in flags if f[1] != "ok"]
print("BAD:", bad if bad else "none")
