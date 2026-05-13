"""TagPool dataclass and Hypothesis strategy for generating tag pools."""

from __future__ import annotations

from dataclasses import dataclass, field

import hypothesis.strategies as st
from hypothesis import assume

from pyrung.core import Block, Bool, Char, Counter, Dint, Int, Real, Tag, TagType, Timer, Word


@dataclass
class TagPool:
    bool_inputs: list[Tag] = field(default_factory=list)
    bool_internal: list[Tag] = field(default_factory=list)
    int_inputs: list[Tag] = field(default_factory=list)
    int_tags: list[Tag] = field(default_factory=list)
    dint_tags: list[Tag] = field(default_factory=list)
    real_tags: list[Tag] = field(default_factory=list)
    word_tags: list[Tag] = field(default_factory=list)
    char_tags: list[Tag] = field(default_factory=list)
    timers: list = field(default_factory=list)
    counters: list = field(default_factory=list)
    int_block: Block | None = None
    bool_block: Block | None = None
    char_block: Block | None = None

    def all_bool(self) -> list[Tag]:
        return self.bool_inputs + self.bool_internal

    def writable_bool(self) -> list[Tag]:
        return self.bool_internal

    def all_numeric(self) -> list[Tag]:
        return self.int_inputs + self.int_tags + self.dint_tags + self.real_tags + self.word_tags

    def all_char(self) -> list[Tag]:
        return self.char_tags

    def writable_numeric(self) -> list[Tag]:
        return self.int_tags + self.dint_tags + self.real_tags + self.word_tags

    def all_conditions(self) -> list:
        return self.all_bool() + [t.Done for t in self.timers] + [c.Done for c in self.counters]

    def input_names(self) -> list[str]:
        return [t.name for t in self.bool_inputs] + [t.name for t in self.int_inputs]

    def input_strategy_map(self) -> dict[str, str]:
        """Return {name: "bool"|"int"} for each external input tag."""
        m: dict[str, str] = {}
        for t in self.bool_inputs:
            m[t.name] = "bool"
        for t in self.int_inputs:
            m[t.name] = "int"
        return m

    def int_input_domain(self, name: str) -> list[int]:
        """Return the finite domain for an external Int input."""
        for t in self.int_inputs:
            if t.name == name:
                if t.choices is not None:
                    return sorted(t.choices.keys())  # ty: ignore[invalid-return-type]
                if t.min is not None and t.max is not None:
                    return list(range(t.min, t.max + 1))
        return [0]


@st.composite
def tag_pools(draw: st.DrawFn) -> TagPool:
    n_inputs = draw(st.integers(1, 4))
    n_internal = draw(st.integers(0, 3))
    n_int_inputs = draw(st.integers(0, 2))
    n_int = draw(st.integers(0, 3))
    n_dint = draw(st.integers(0, 2))
    n_real = draw(st.integers(0, 1))
    n_word = draw(st.integers(0, 1))
    n_char = draw(st.integers(0, 2))
    n_timer = draw(st.integers(0, 2))
    n_counter = draw(st.integers(0, 2))
    has_block = draw(st.booleans())
    has_bool_block = draw(st.booleans())

    bool_inputs = [Bool(f"In{i}", external=True) for i in range(n_inputs)]
    bool_internal = [Bool(f"B{i}") for i in range(n_internal)]

    int_inputs = []
    for i in range(n_int_inputs):
        flavor = draw(st.floats(0, 1))
        if flavor < 0.6:
            choices = draw(
                st.sampled_from(
                    [
                        {0: "Off", 1: "On", 2: "Auto"},
                        {0: "Idle", 1: "Run", 2: "Done"},
                        {1: "A", 2: "B"},
                    ]
                )
            )
            int_inputs.append(Int(f"ExtN{i}", external=True, choices=choices))
        else:
            lo = draw(st.sampled_from([0, -10]))
            hi = draw(st.sampled_from([10, 50, 100]))
            int_inputs.append(Int(f"ExtN{i}", external=True, min=lo, max=hi))

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
    char_tags = [Char(f"Ch{i}") for i in range(n_char)]
    timers = [Timer.clone(f"T{i}") for i in range(n_timer)]
    counters = [Counter.clone(f"C{i}") for i in range(n_counter)]

    int_block = None
    if has_block:
        size = draw(st.integers(3, 8))
        int_block = Block("DS", TagType.INT, 1, size)

    bool_block = None
    if has_bool_block:
        bool_block = Block("CB", TagType.BOOL, 1, 8)

    char_block = None
    if n_char > 0 and draw(st.booleans()):
        char_block = Block("CH", TagType.CHAR, 1, draw(st.integers(3, 8)))

    pool = TagPool(
        bool_inputs=bool_inputs,
        bool_internal=bool_internal,
        int_inputs=int_inputs,
        int_tags=int_tags,
        dint_tags=dint_tags,
        real_tags=real_tags,
        word_tags=word_tags,
        char_tags=char_tags,
        timers=timers,
        counters=counters,
        int_block=int_block,
        bool_block=bool_block,
        char_block=char_block,
    )
    assume(len(pool.all_conditions()) > 0)
    return pool
