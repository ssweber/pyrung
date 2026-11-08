"""Runners on one program share the compiled code and its parse, and nothing that scans."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from pyrung import PLC, Block, Bool, Int, Program, Rung, TagType, Timer, blockcopy, copy, on_delay
from pyrung.circuitpy.codegen import render_kernel
from pyrung.circuitpy.codegen.context import describe_static_range
from pyrung.core import jit_stats, scan_trace
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.coast_run import coast_until
from pyrung.core.memory_block import BlockRange
from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME
from pyrung.core.tag import Tag

T = Timer.clone("KsT")
Go = Bool("KsGo")
N = Int("KsN")
Bank = Block("KsBank", TagType.INT, 1, 40)
SCANS = 3000


@pytest.fixture(autouse=True)
def _no_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    # A collector PYRUNG_JIT_STATS started would count these runs too.
    monkeypatch.setattr(jit_stats, "ACTIVE", [])


def _program() -> Program:
    """A timer that runs while Go is on, and N set to 5 once it is done."""
    with Program(strict=False) as program:
        with Rung(Go):
            on_delay(T, 10_000, "ms")
        with Rung(T.Done):
            copy(5, N)
    return program


def _runner(program: Program, go: bool) -> PLC:
    plc = PLC(program, dt=0.01)
    plc.patch({Go.name: go})
    plc.step()
    return plc


def test_a_second_runner_reuses_the_code_and_the_parse() -> None:
    program = _program()
    first = _runner(program, True)
    second = _runner(program, True)
    kept = first._compiled_replay_supported_kernel()
    with jit_stats.collect() as stats:
        again = second._compiled_replay_supported_kernel()
    assert kept is not None and again is not None and kept is not again
    assert stats.costs["compile"][0] == 1
    assert "compile: python compile" not in stats.costs
    assert kept.step_fn.__code__ is again.step_fn.__code__
    # Each kernel runs the code in a namespace of its own.
    assert kept.step_fn is not again.step_fn
    assert kept.step_fn.__globals__ is not again.step_fn.__globals__
    assert scan_trace._source_of(kept) is scan_trace._source_of(again)


def test_two_runners_on_one_program_keep_their_own_scans() -> None:
    program = _program()
    timing, idle = _runner(program, True), _runner(program, False)
    twins = _runner(program, True), _runner(program, False)
    with jit_stats.collect() as stats:
        # In turns, so each runner's loop runs between the other's.
        for _ in range(3):
            for plc in (timing, idle):
                coast_until(plc, lambda _s: False, budget=SCANS, predicate_reads=frozenset())
    assert stats.stretched_scans > SCANS
    for twin in twins:
        for _ in range(3 * SCANS):
            twin.step()
    for plc, twin in zip((timing, idle), twins, strict=True):
        assert plc.state.scan_id == twin.state.scan_id
        assert plc.state.tags == twin.state.tags
    assert timing.state.tags[N.name] == 5
    assert idle.state.tags.get(N.name, 0) == 0


def test_the_code_kept_is_bounded() -> None:
    for preset in range(render_kernel.MAX_CACHED_CODE + 2):
        with Program(strict=False) as program:
            with Rung(Go):
                on_delay(T, 1000 + preset, "ms")
        PLC(program, dt=0.01)._compiled_replay_supported_kernel()
    assert len(render_kernel._CODE_BY_SOURCE) == render_kernel.MAX_CACHED_CODE


def _names_item_by_item(program: Program) -> frozenset[str]:
    """The materialized-tag walk as it was, queueing every item it meets."""
    found: set[str] = set()
    visited: set[int] = set()
    queue: list[Any] = [*program.rungs]
    for rungs in program.subroutines.values():
        queue.extend(rungs)
    while queue:
        current = queue.pop()
        if current is None:
            continue
        if isinstance(current, Tag):
            if current.name not in SYSTEM_TAGS_BY_NAME:
                found.add(current.name)
            continue
        if isinstance(current, (str, bytes, bytearray, int, float, bool, Block)):
            continue
        if type(current) is BlockRange:
            continue
        if isinstance(current, BlockRange):
            layout = describe_static_range(current)
            if not layout.uses_backing_storage:
                queue.extend(layout.tags)
            continue
        if id(current) in visited:
            continue
        visited.add(id(current))
        if isinstance(current, Mapping):
            queue.extend(current.keys())
            queue.extend(current.values())
        elif isinstance(current, tuple | list | set | frozenset):
            queue.extend(current)
        elif hasattr(current, "__dict__"):
            queue.extend(vars(current).values())
        elif hasattr(current, "__slots__"):
            for slot in current.__slots__:
                if slot not in {"__weakref__", "__dict__"} and hasattr(current, slot):
                    queue.append(getattr(current, slot))
    return frozenset(found)


def test_the_materialized_tags_are_those_a_walk_of_every_item_finds() -> None:
    with Program(strict=False) as program:
        with Rung(Go):
            blockcopy(Bank.select(1, 20), Bank.select(21, 40))
        with Rung(Bank[3] > 2):
            copy(5, N)
    PLC(program, dt=0.01)
    before = render_kernel._collect_materialized_tag_names(program)
    assert before == _names_item_by_item(program)
    # A rung reaches the program's analysis caches, which hold sets of plain values.
    build_program_graph(program)
    after = render_kernel._collect_materialized_tag_names(program)
    assert after == _names_item_by_item(program)
