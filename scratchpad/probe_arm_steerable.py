"""Probe the boundaries of _arm_fully_steerable: which OR-arm shapes collapse
(PILOT picks the steerable arm) vs still surface as an ambiguous choice.

Tests the helper directly on synthetic Atom/And/Or trees so we can see exactly
where it still 'balks for silly reasons'.
"""

from __future__ import annotations

from pyrung.core.analysis.simplified import And, Atom, Or
from pyrung.core.analysis.pilot.trace import _arm_fully_steerable

STEER = frozenset({"Manual", "DiverterBtn", "BtnA", "BtnB", "Size", "Auto"})
SELF = "DiverterCmd"


def xic(t):
    return Atom(tag=t, form="xic", operand=None)


def gt(t, op):
    return Atom(tag=t, form="gt", operand=op)


def eq(t, op):
    return Atom(tag=t, form="eq", operand=op)


cases = {
    "bare input atom            Manual": xic("Manual"),
    "And of inputs             And(Manual,DiverterBtn)": And(terms=(xic("Manual"), xic("DiverterBtn"))),
    "nested And of inputs      And(Manual,And(BtnA,BtnB))": And(terms=(xic("Manual"), And(terms=(xic("BtnA"), xic("BtnB"))))),
    "internal arm              And(State==2,IsLarge,Auto)": And(terms=(eq("State", 2), xic("IsLarge"), xic("Auto"))),
    "internal coil atom        ProdMode": xic("ProdMode"),
    "self-seal arm             DiverterCmd": xic(SELF),
    "--- likely 'silly balks' below ---": None,
    "nested OR of inputs       And(Manual,Or(BtnA,BtnB))": And(terms=(xic("Manual"), Or(terms=(xic("BtnA"), xic("BtnB"))))),
    "threshold on steer input  And(Manual,Size>100)": And(terms=(xic("Manual"), gt("Size", 100))),
    "bare threshold            Size>100": gt("Size", 100),
}

print(f"{'shape':52s} steerable?")
print("-" * 66)
for label, expr in cases.items():
    if expr is None:
        print(label)
        continue
    print(f"{label:52s} {_arm_fully_steerable(expr, SELF, STEER)}")
