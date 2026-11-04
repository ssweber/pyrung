"""Property test: CLICK v4 programs survive the CSV round trip with their behaviour.

A random program of v4 instructions (Out variants, For/Next with body rungs,
Break and Continue, range aggregates, ``randint``) is built on real CLICK
addresses, exported to the ladder CSV, read back by the codegen and
exec'd.  The two programs are then run on the same input schedule and must hold
the same tags every scan.  ``randint`` sites draw from a hash of their site
number, which differs between two programs, so those destinations are only
checked against their bounds.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings

from pyrung.click import ClickBlocks, TagMap, ladder_to_pyrung, pyrung_to_ladder
from pyrung.core import (
    PLC,
    And,
    Or,
    Program,
    Rung,
    Timer,
    break_loop,
    calc,
    continue_loop,
    debounce,
    flasher,
    forloop,
    randint,
    timed,
    toggle,
)
from tests.click.helpers import exec_with_source
from tests.core.test_v4_agreement_hypothesis import Spec, _spec

pytestmark = pytest.mark.hypothesis

SETTINGS = settings(
    max_examples=200,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

LEVEL = ds[100]


def _cond(cond):
    kind, a, b = cond
    if kind == "in":
        return ~x[a + 1] if b else x[a + 1]
    if kind == "and":
        return And(x[a + 1], ~x[b + 1])
    if kind == "or":
        return Or(x[a + 1], x[b + 1])
    return LEVEL > a if kind == "gt" else LEVEL < a


def _emit(instruction: tuple, k: int, timers: list, rands: list) -> None:
    kind = instruction[0]

    def timer(side: str) -> Timer:
        n = 2 * k + (1 if side == "b" else 0)
        clone = Timer.clone(f"T{n}", nicknames={"Done": f"T{n}", "Acc": f"TD{n}"})
        timers.append((clone, n))
        return clone

    target = c[k]
    if kind == "toggle":
        toggle(target)
    elif kind == "debounce":
        _, on, off, unit = instruction
        debounce(target, on=(timer("a"), on), off=(timer("b"), off), unit=unit)
    elif kind == "flasher":
        _, on, off, unit = instruction
        flasher(target, on=(timer("a"), on), off=(timer("b"), off), unit=unit)
    elif kind == "timed":
        _, preset, unit, maintained = instruction
        timed(target, timer("a"), preset, unit, maintained=maintained)
    elif kind == "for":
        _, count, leaves = instruction
        total = ds[200 + k]
        with forloop(count):
            for leaf, at in leaves:
                if leaf == "inc":
                    calc(total + 1, total)
                else:
                    with Rung(total >= at):
                        break_loop() if leaf == "break" else continue_loop()
    elif kind == "agg":
        _, agg, lo, hi = instruction
        calc(getattr(ds.select(100 + lo, 100 + hi), agg)(), ds[300 + k])
    elif kind == "rand":
        _, lo, hi = instruction
        calc(randint(lo, hi), ds[400 + k])
        rands.append((f"DS{400 + k}", min(lo, hi), max(lo, hi)))
    else:  # randagg
        calc(randint(1, 6) + ds.select(101, 106).sum(), ds[500 + k])


def _build(spec: Spec) -> tuple[Program, TagMap, list]:
    k, timers, rands = 0, [], []
    with Program(strict=False) as logic:
        for cond, instructions in spec.rungs:
            # A For is the only instruction of its rung (ladder v1 shape).
            plain = []
            for instruction in instructions:
                k += 1
                if instruction[0] == "for":
                    with Rung(_cond(cond)):
                        _emit(instruction, k, timers, rands)
                else:
                    plain.append((instruction, k))
            if plain:
                with Rung(_cond(cond)):
                    for instruction, at in plain:
                        _emit(instruction, at, timers, rands)
    entries = []
    for clone, n in timers:
        entries += [clone.Done.map_to(t[n]), clone.Acc.map_to(td[n])]
    return logic, TagMap(entries), rands


def _randagg_names(spec: Spec) -> set[str]:
    names, k = set(), 0
    for _, instructions in spec.rungs:
        for instruction in instructions:
            k += 1
            if instruction[0] == "randagg":
                names.add(f"DS{500 + k}")
    return names


@given(spec=_spec())
@SETTINGS
def test_v4_programs_behave_the_same_after_export_and_import(spec):
    logic, mapping, rands = _build(spec)
    code = ladder_to_pyrung(pyrung_to_ladder(logic, mapping))
    namespace: dict[str, object] = {}
    exec_with_source(code, namespace)
    again = namespace["logic"]

    ran: set[tuple[str, str]] = set()  # (side, dest) that has held a draw
    skip = {name for name, _, _ in rands} | _randagg_names(spec)
    dt = spec.dt
    left, right = PLC(logic, dt=dt), PLC(again, dt=dt)
    for inputs, level, rg, scans in spec.stretches:
        patch = {f"X{i + 1:03d}": v for i, v in enumerate(inputs)}
        patch["DS100"] = level
        patch.update({f"DS{100 + i}": v for i, v in enumerate(rg, start=1)})
        left.patch(patch)
        right.patch(patch)
        for _ in range(scans):
            left.step()
            right.step()
            a, b = dict(left.current_state.tags), dict(right.current_state.tags)
            for name, lo, hi in rands:
                # A destination is 0 until its rung first runs (calc then holds
                # the last draw), so 0 out of bounds means "not run yet".  Once
                # nonzero it must stay in bounds, and both sides must agree on
                # whether the rung has run.
                if not lo <= 0 <= hi:
                    assert (a[name] == 0) == (b[name] == 0), (name, a[name], b[name], code)
                for side, tags in (("a", a), ("b", b)):
                    if tags[name] != 0:
                        ran.add((side, name))
                        assert lo <= tags[name] <= hi, (name, side, tags[name], lo, hi)
                    elif (side, name) in ran:
                        assert lo <= 0 <= hi, (name, side, "went back to 0", lo, hi)
            for name in skip:
                a.pop(name, None)
                b.pop(name, None)
            # Hidden randint draws are named by site, which differs by program.
            a = {n: v for n, v in a.items() if not n.startswith("_rand.")}
            b = {n: v for n, v in b.items() if not n.startswith("_rand.")}
            # The import declares only the tags the logic names; the rest read 0.
            for name in a.keys() | b.keys():
                assert a.get(name, 0) == b.get(name, 0), (name, code)
