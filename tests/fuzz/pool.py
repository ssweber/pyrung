"""TagPool dataclass and Hypothesis strategy for generating tag pools."""

from __future__ import annotations

from dataclasses import dataclass, field

import hypothesis.strategies as st
from hypothesis import assume

from pyrung.core import Block, Bool, Counter, Dint, Int, Real, Tag, TagType, Timer, Word


@dataclass
class TagPool:
    bool_inputs: list[Tag] = field(default_factory=list)
    bool_internal: list[Tag] = field(default_factory=list)
    int_tags: list[Tag] = field(default_factory=list)
    dint_tags: list[Tag] = field(default_factory=list)
    real_tags: list[Tag] = field(default_factory=list)
    word_tags: list[Tag] = field(default_factory=list)
    timers: list = field(default_factory=list)
    counters: list = field(default_factory=list)
    int_block: Block | None = None
    bool_block: Block | None = None

    def all_bool(self) -> list[Tag]:
        return self.bool_inputs + self.bool_internal

    def writable_bool(self) -> list[Tag]:
        return self.bool_internal

    def all_numeric(self) -> list[Tag]:
        return self.int_tags + self.dint_tags + self.real_tags + self.word_tags

    def writable_numeric(self) -> list[Tag]:
        return self.all_numeric()

    def all_conditions(self) -> list:
        return self.all_bool() + [t.Done for t in self.timers] + [c.Done for c in self.counters]

    def input_names(self) -> list[str]:
        return [t.name for t in self.bool_inputs]


@st.composite
def tag_pools(draw: st.DrawFn) -> TagPool:
    n_inputs = draw(st.integers(1, 4))
    n_internal = draw(st.integers(0, 3))
    n_int = draw(st.integers(0, 3))
    n_dint = draw(st.integers(0, 2))
    n_real = draw(st.integers(0, 1))
    n_word = draw(st.integers(0, 1))
    n_timer = draw(st.integers(0, 2))
    n_counter = draw(st.integers(0, 2))
    has_block = draw(st.booleans())
    has_bool_block = draw(st.booleans())

    bool_inputs = [Bool(f"In{i}", external=True) for i in range(n_inputs)]
    bool_internal = [Bool(f"B{i}") for i in range(n_internal)]

    int_tags = []
    for i in range(n_int):
        flavor = draw(st.floats(0, 1))
        if flavor < 0.3:
            lo = draw(st.integers(-50, 0))
            hi = draw(st.integers(1, 50))
            int_tags.append(Int(f"N{i}", min=lo, max=hi))
        elif flavor < 0.5:
            choices = {0: "off", 1: "on", 2: "auto"}
            int_tags.append(Int(f"N{i}", choices=choices))
        else:
            int_tags.append(Int(f"N{i}"))

    dint_tags = [Dint(f"D{i}") for i in range(n_dint)]
    real_tags = [Real(f"R{i}") for i in range(n_real)]
    word_tags = [Word(f"W{i}") for i in range(n_word)]
    timers = [Timer.clone(f"T{i}") for i in range(n_timer)]
    counters = [Counter.clone(f"C{i}") for i in range(n_counter)]

    int_block = None
    if has_block:
        size = draw(st.integers(3, 8))
        int_block = Block("DS", TagType.INT, 1, size)

    bool_block = None
    if has_bool_block:
        bool_block = Block("CB", TagType.BOOL, 1, 8)

    pool = TagPool(
        bool_inputs=bool_inputs,
        bool_internal=bool_internal,
        int_tags=int_tags,
        dint_tags=dint_tags,
        real_tags=real_tags,
        word_tags=word_tags,
        timers=timers,
        counters=counters,
        int_block=int_block,
        bool_block=bool_block,
    )
    assume(len(pool.all_conditions()) > 0)
    return pool
