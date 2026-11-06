"""Property test: For/Next with body rungs, Break, Continue and nesting.

A random loop nest (up to three deep) is built into a pyrung program and run
by a plain-Python interpreter of the same tree.  The tree mixes bare body
instructions (implicit always-true rungs) with conditioned body rungs holding
``break_loop()`` / ``continue_loop()``, ``out()`` coils and ``calc`` increments, read
back from tags the body wrote on earlier iterations.  The interpreter and the
compiled kernel must both match the model on every scan, and each other.

Rules the model encodes (hardware-recorded in ``tests/conformance/program_control/test_loop.py``
and ``test_for_count.py``): a For whose rung is false skips its body, so
coils hold; the count is read as unsigned 16-bit and a count of 0 runs no
iteration; Break leaves the innermost loop, Continue skips the rest of the
iteration; a body rung reads its contacts afresh on every iteration.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pyrung.core import (
    PLC,
    Bool,
    CompiledPLC,
    Int,
    Program,
    Rung,
    break_loop,
    continue_loop,
    forloop,
    math,
    out,
)

pytestmark = pytest.mark.hypothesis

SETTINGS = settings(
    max_examples=300,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

REGS = tuple(Int(f"FlR{i}") for i in range(3))
FLAGS = tuple(Bool(f"FlF{i}") for i in range(3))
COUNTS = tuple(Int(f"FlN{i}") for i in range(3))
Enable = Bool("FlEnable")
MAX_DEPTH = 3


# --- the tree -------------------------------------------------------------------


@dataclass
class Loop:
    count: tuple  # ("const", n) | ("tag", i)
    body: list = field(default_factory=list)


# Items: ("inc", reg) | ("out", flag) | ("rung", cond, [leaf]) | Loop
# Leaves: ("inc", reg) | ("out", flag) | ("break",) | ("continue",)
# Conds:  ("idx", level, k) | ("ge", reg, k) | ("flag", f), each with a negate bit.


@st.composite
def _cond(draw, levels: int):
    kind = draw(st.sampled_from(["idx", "ge", "flag"]))
    negate = draw(st.booleans())
    if kind == "idx":
        return (kind, draw(st.integers(0, levels - 1)), draw(st.integers(0, 4)), negate)
    if kind == "ge":
        return (kind, draw(st.integers(0, 2)), draw(st.integers(0, 6)), negate)
    return (kind, draw(st.integers(0, 2)), 0, negate)


@st.composite
def _leaf(draw):
    return draw(
        st.one_of(
            st.tuples(st.just("inc"), st.integers(0, 2)),
            st.tuples(st.just("out"), st.integers(0, 2)),
            st.just(("break",)),
            st.just(("continue",)),
        )
    )


@st.composite
def _body(draw, levels: int, counter: list):
    """A loop body, *levels* loops deep (>= 1); ids come from *counter*."""
    items: list = []
    kinds = ["inc", "out", "rung", "rung"] + (["loop"] if levels < MAX_DEPTH else [])
    for _ in range(draw(st.integers(1, 4))):
        kind = draw(st.sampled_from(kinds))
        if kind == "inc":
            items.append(("inc", draw(st.integers(0, 2))))
        elif kind == "out":
            items.append(("out", draw(st.integers(0, 2))))
        elif kind == "rung":
            cond = draw(_cond(levels))
            items.append(("rung", cond, draw(st.lists(_leaf(), min_size=1, max_size=3))))
        else:
            count = draw(
                st.one_of(
                    st.tuples(st.just("const"), st.integers(1, 5)),
                    st.tuples(st.just("tag"), st.integers(0, 2)),
                )
            )
            loop = Loop(count)
            loop.body = draw(_body(levels + 1, counter))
            items.append(loop)
    return items


@st.composite
def _scenario(draw):
    counter = [0]
    top = Loop(
        draw(
            st.one_of(
                st.tuples(st.just("const"), st.integers(1, 5)),
                st.tuples(st.just("tag"), st.integers(0, 2)),
            )
        ),
    )
    top.body = draw(_body(1, counter))
    scans = draw(
        st.lists(
            st.tuples(st.booleans(), st.tuples(*(st.integers(0, 5) for _ in range(3)))),
            min_size=1,
            max_size=4,
        )
    )
    return top, scans


# --- the model ------------------------------------------------------------------


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


def _wrap16(value: int) -> int:
    return (value + 0x8000) % 0x10000 - 0x8000


class _Model:
    def __init__(self) -> None:
        self.regs = [0, 0, 0]
        self.flags = [False, False, False]
        self.idx: dict[int, int] = {}  # loop index by depth: siblings share a tag
        self.depth = 0
        self.counts = (0, 0, 0)

    def cond(self, cond) -> bool:
        kind, a, b, negate = cond
        if kind == "idx":
            value = self.idx.get(a + 1, 0) == b
        elif kind == "ge":
            value = self.regs[a] >= b
        else:
            value = self.flags[a]
        return value != negate

    def leaf(self, leaf) -> None:
        if leaf[0] == "inc":
            self.regs[leaf[1]] = _wrap16(self.regs[leaf[1]] + 1)
        elif leaf[0] == "out":
            self.flags[leaf[1]] = True
        elif leaf[0] == "break":
            raise _Break
        else:
            raise _Continue

    def items(self, items) -> None:
        for item in items:
            if isinstance(item, Loop):
                self.loop(item)
            elif item[0] == "rung":
                _, cond, leaves = item
                powered = self.cond(cond)
                for leaf in leaves:
                    if leaf[0] == "out":
                        self.flags[leaf[1]] = powered  # a coil follows its rung
                    elif powered:
                        self.leaf(leaf)
            else:
                self.leaf(item)

    def loop(self, loop: Loop) -> None:
        raw = loop.count[1] if loop.count[0] == "const" else self.counts[loop.count[1]]
        self.depth += 1
        for i in range(raw & 0xFFFF):
            self.idx[self.depth] = i
            try:
                self.items(loop.body)
            except _Continue:
                continue
            except _Break:
                break
        self.depth -= 1


# --- the program ----------------------------------------------------------------


def _emit_leaf(leaf) -> None:
    if leaf[0] == "inc":
        math(REGS[leaf[1]] + 1, REGS[leaf[1]])
    elif leaf[0] == "out":
        out(FLAGS[leaf[1]])
    elif leaf[0] == "break":
        break_loop()
    else:
        continue_loop()


def _emit_cond(cond, idx_tags):
    kind, a, b, negate = cond
    if kind == "idx":
        return idx_tags[a] != b if negate else idx_tags[a] == b
    if kind == "ge":
        return REGS[a] < b if negate else REGS[a] >= b
    return ~FLAGS[a] if negate else FLAGS[a]


def _emit_loop(loop: Loop, idx_tags: list, idx_by_depth: dict) -> None:
    count = loop.count[1] if loop.count[0] == "const" else COUNTS[loop.count[1]]
    with forloop(count) as handle:
        idx_tags.append(handle.idx)
        idx_by_depth[len(idx_tags)] = handle.idx.name
        for item in loop.body:
            if isinstance(item, Loop):
                _emit_loop(item, idx_tags, idx_by_depth)
            elif item[0] == "rung":
                with Rung(_emit_cond(item[1], idx_tags)):
                    for leaf in item[2]:
                        _emit_leaf(leaf)
            else:
                _emit_leaf(item)
    idx_tags.pop()


def _build(top: Loop) -> tuple[Program, dict[int, str]]:
    idx_by_depth: dict[int, str] = {}
    with Program(strict=False) as logic:
        with Rung(Enable):
            _emit_loop(top, [], idx_by_depth)
    return logic, idx_by_depth


@given(scenario=_scenario())
@SETTINGS
def test_forloop_body_matches_the_model(scenario):
    top, scans = scenario
    logic, idx_names = _build(top)
    plc = PLC(logic)
    kernel = CompiledPLC(logic)
    model = _Model()
    for enabled, counts in scans:
        patch = {"FlEnable": enabled, **{f"FlN{i}": v for i, v in enumerate(counts)}}
        plc.patch(patch)
        kernel.patch(patch)
        plc.step()
        kernel.step()
        if enabled:  # a false For skips its body: coils and registers hold
            model.counts = counts
            model.loop(top)
        for state in (plc.current_state, kernel.current_state):
            tags = state.tags
            assert [tags.get(r.name, 0) for r in REGS] == model.regs
            assert [tags.get(f.name, False) for f in FLAGS] == model.flags
            for depth, name in idx_names.items():
                assert tags.get(name, 0) == model.idx.get(depth, 0)
        assert dict(plc.current_state.tags) == dict(kernel.current_state.tags)
        assert dict(plc.current_state.memory) == dict(kernel.current_state.memory)
