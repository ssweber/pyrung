"""The records the kernel codegen writes for timers and counters, and the answers read from them."""

from __future__ import annotations

import ast
import copy as copy_module
import dataclasses
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any

import pytest

import pyrung.core.relations as relations_module
import pyrung.core.relations._render as render_module
from pyrung.core import (
    Bool,
    Counter,
    Int,
    Program,
    Timer,
    count_down,
    count_up,
    off_delay,
    on_delay,
    rise,
    rung,
)
from pyrung.core.crossing import Cmp
from pyrung.core.instruction.counters import CountUpInstruction
from pyrung.core.relations import Relations, relations_for
from pyrung.core.relations._guards import Atom, Not, Opaque, Placed, place, uncovered
from pyrung.core.relations._render import render
from pyrung.core.relations.records import (
    Compare,
    Const,
    Memory,
    Read,
    Setting,
    Store,
    TagValue,
    Updated,
)
from pyrung.core.validation._common import walk_instructions

En = Bool("En")
Rst = Bool("Rst")
Dn = Bool("Dn")
Preset = Int("Preset")


def _shapes() -> dict[str, Any]:
    """One instruction of each shape the timer and counter emitters write."""
    with Program(strict=False) as program:
        with rung(En):
            on_delay(Timer.clone("TonBare"), 100)
        with rung(En):
            on_delay(Timer.clone("TonReset"), Preset, "s").reset(Rst)
        with rung(En):
            on_delay(Timer.clone("TonEdgeReset"), -5).reset(rise(Rst))
        with rung():
            on_delay(Timer.clone("TonAlways"), 100)
        with rung(En):
            off_delay(Timer.clone("Tof"), 100)
        with rung(En):
            off_delay(Timer.clone("TofTag"), Preset, "s")
        with rung(En):
            count_up(Counter.clone("CtuReset"), Preset).reset(Rst)
        with rung(En):
            count_up(Counter.clone("CtuUpDown"), 5).down(Dn).reset(Rst)
        with rung(En):
            count_down(Counter.clone("Ctd"), -5).reset(Rst)
    names = [
        "TonBare",
        "TonReset",
        "TonEdgeReset",
        "TonAlways",
        "Tof",
        "TofTag",
        "CtuReset",
        "CtuUpDown",
        "Ctd",
    ]
    shapes = dict(zip(names, walk_instructions(program), strict=True))
    # The DSL asks every counter for a reset, so this one is built directly.
    bare = Counter.clone("CtuBare")
    shapes["CtuBare"] = CountUpInstruction(bare.Done, bare.Acc, 5, En, None)
    return shapes


SHAPES = _shapes()


class _StripMarkers(ast.NodeTransformer):
    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id == "_r":
            return node.args[1]
        return node


@pytest.mark.parametrize("name", SHAPES)
def test_recording_changes_no_code(name: str) -> None:
    # The recorder wraps a piece in _r(id, piece) and adds nothing else.
    recorded = render(SHAPES[name])
    plain = render(SHAPES[name], record=False)
    assert recorded.records
    assert "_r(" not in plain.source
    stripped = _StripMarkers().visit(ast.parse(recorded.source))
    assert ast.dump(stripped) == ast.dump(ast.parse(plain.source))


@pytest.mark.parametrize("name", SHAPES)
def test_every_read_write_and_compare_is_recorded(name: str) -> None:
    rendered = render(SHAPES[name])
    assert uncovered(rendered.source, rendered.records, rendered.symbols) == []


@pytest.mark.parametrize(
    ("name", "op", "preset"),
    [
        ("TonBare", ">=", Setting("preset")),
        ("TonReset", ">=", TagValue("pb_preset")),
        ("TonEdgeReset", ">=", Setting("preset")),
        ("Tof", "<", Setting("preset")),
        ("CtuUpDown", ">=", Setting("preset")),
        ("Ctd", "<=", Setting("preset")),
    ],
)
def test_done_is_the_updated_acc_compared_with_the_preset(name: str, op: str, preset: Any) -> None:
    relations = relations_for(SHAPES[name])
    assert relations is not None
    [(_placed, compare)] = relations.done_compares()
    assert compare == Compare(Updated("pb_accumulator"), op, preset)


@pytest.mark.parametrize("name", ["TonBare", "TonReset", "TonEdgeReset", "CtuUpDown", "Ctd", "Tof"])
def test_completion_boundary_agrees_with_the_recorded_compare(name: str) -> None:
    instruction = SHAPES[name]
    profile = instruction.advance_profile()
    relations = relations_for(instruction)
    assert relations is not None and profile.completion_boundary is not None
    [(_placed, compare)] = relations.done_compares()
    boundary = profile.completion_boundary({Preset.name: 7})
    assert isinstance(boundary, Cmp)
    # The off delay's Done is on below the preset, so its boundary is the compare's complement.
    complement = {"<": ">=", ">=": "<", "<=": ">", ">": "<="}
    expected_op = complement[compare.op] if name == "Tof" else compare.op
    assert boundary.op == expected_op
    if isinstance(compare.right, Setting):
        assert boundary.bound == getattr(instruction, compare.right.field)
    else:
        assert boundary.bound == Preset.name


@pytest.mark.parametrize(
    ("name", "controls"),
    [
        ("TonBare", [("enable_condition", True)]),
        ("TonReset", [("reset_condition", False), ("enable_condition", True)]),
        ("TonAlways", []),
        ("CtuBare", []),
        ("CtuUpDown", [("reset_condition", False)]),
        ("Ctd", [("reset_condition", False)]),
        # The off delay computes Done under a test of its memory, which no condition decides.
        ("Tof", None),
    ],
)
def test_completion_controls_come_from_the_tests_around_done(
    name: str, controls: list[tuple[str, bool]] | None
) -> None:
    instruction = SHAPES[name]
    declared = instruction.advance_profile().completion_controls
    if controls is None:
        assert declared is None
        return
    assert declared is not None
    assert [(d.condition, d.value) for d in declared] == [
        (getattr(instruction, field), value) for field, value in controls
    ]


@pytest.mark.parametrize(
    ("name", "edges", "own_reads", "zeroing", "opposing", "count_holds"),
    [
        ("TonReset", set(), {"reset_condition"}, {"reset_condition"}, None, None),
        ("Tof", set(), set(), set(), None, None),
        (
            "CtuUpDown",
            {"up_condition", "down_condition"},
            {"reset_condition", "down_condition"},
            {"reset_condition"},
            "down_condition",
            (("reset_condition", False),),
        ),
        ("CtuBare", {"up_condition"}, set(), set(), None, ()),
        (
            "Ctd",
            {"down_condition"},
            {"reset_condition"},
            {"reset_condition"},
            None,
            (("reset_condition", False),),
        ),
    ],
)
def test_condition_legs(
    name: str,
    edges: set[str],
    own_reads: set[str],
    zeroing: set[str],
    opposing: str | None,
    count_holds: tuple[tuple[str, bool], ...] | None,
) -> None:
    relations = relations_for(SHAPES[name])
    assert relations is not None
    assert relations.edge_fields == edges
    assert relations.snapshot_fields == own_reads
    assert relations.zeroing_fields == zeroing
    assert relations.opposing_count_field == opposing
    assert relations.count_holds(relations.power) == count_holds


def test_off_delay_motion_waits_on_its_armed_memory() -> None:
    relations = relations_for(SHAPES["Tof"])
    assert relations is not None
    [gate] = relations.motion_gates
    assert gate.value is True
    assert (gate.setter, gate.setter_value) == ("done_bit", True)
    assert gate.holds == (("enable_condition", True),)
    assert relations_for(SHAPES["TonReset"]).motion_gates == ()


def test_relations_are_shared_by_shape() -> None:
    with Program(strict=False) as program:
        with rung(Dn):
            on_delay(Timer.clone("Other"), 100)
        with rung(Dn):
            on_delay(Timer.clone("OtherPreset"), 200)
        with rung(Dn):
            on_delay(Timer.clone("TagPreset"), Preset)
    same, other_preset, tag_preset = walk_instructions(program)
    assert relations_for(same) is relations_for(SHAPES["TonBare"])
    assert relations_for(other_preset) is relations_for(SHAPES["TonBare"])
    assert relations_for(tag_preset) is not relations_for(SHAPES["TonBare"])


@pytest.mark.parametrize("name", ["TonBare", "Tof", "CtuUpDown", "Ctd"])
def test_an_integer_preset_changes_only_its_number_in_the_code(name: str) -> None:
    # The shape leaves out an integer literal's value, so the code must not depend on it.
    texts = set()
    for preset in (0, 1, -1, 100, 32767, 40000, -40000, 2**31 - 1, 2**31, -(2**31)):
        instruction = copy_module.copy(SHAPES[name])
        instruction.preset = preset
        source = render(instruction, record=False).source
        assert f"int({preset!r})" in source
        texts.add(source.replace(f"int({preset!r})", "int(PRESET)"))
    assert len(texts) == 1


def _unmark(source: str, text: str, before: str = "") -> str:
    """*source* with the marker around one *text*, after *before*, taken off."""
    pattern = re.escape(before) + r"_r\(\d+, " + re.escape(text) + r"\)"
    result, count = re.subn(pattern, lambda _m: before + text, source, count=1)
    assert count == 1
    return result


def _insert_before_reset_read(line: str) -> Any:
    return lambda s: s.replace("    _box_1_snap_0 =", f"    {line}\n    _box_1_snap_0 =", 1)


_DT_LOAD = '    memory["_dt"] = dt'
_LAST_STORE = "    tags['pb_tt_bit'] = _t_pb_tt_bit"


def _insert_after(line: str, after: str) -> Any:
    return lambda s: s.replace(after, f"{after}\n    {line}", 1)


def _insert_before_store_backs(line: str) -> Any:
    # The store-backs start with the first tag in name order.
    first = "    tags['fault.address_error'] ="
    return lambda s: s.replace(first, f"    {line}\n{first}", 1)


@pytest.mark.parametrize(
    "inject",
    [
        pytest.param(lambda s: _unmark(s, "False", "_t_pb_done_bit = "), id="symbol store"),
        pytest.param(lambda s: _unmark(s, "_t_pb_preset"), id="symbol read"),
        pytest.param(
            lambda s: s.replace("if _box_1_snap_0:", "if _box_1_snap_0 or _t_pb_done_bit:"),
            id="symbol read in a test",
        ),
        pytest.param(_insert_before_reset_read("_clamp(_rung_1_enabled)"), id="helper call"),
        pytest.param(_insert_before_reset_read("_x = memory['k']"), id="memory read"),
        pytest.param(
            lambda s: s.replace(
                _RESET_STORE, f"{_RESET_STORE}\n        tags['pb_done_bit'] = _t_pb_accumulator"
            ),
            id="a store back to tags inside a branch",
        ),
        pytest.param(
            _insert_before_reset_read("_x = getattr(tags, 'get')('pb_done_bit')"), id="getattr"
        ),
        pytest.param(_insert_before_reset_read("_tg = tags"), id="alias"),
        pytest.param(_insert_before_reset_read("print(tags)"), id="plain helper"),
        pytest.param(_insert_before_reset_read("_x = dt"), id="raw dt"),
        pytest.param(
            _insert_before_store_backs("tags['pb_done_bit'] = _t_pb_accumulator"),
            id="a store of another tag's symbol before the store-backs",
        ),
        pytest.param(
            lambda s: s.replace(_LAST_STORE, "    tags['pb_tt_bit'] = _t_pb_accumulator"),
            id="a store-back of another tag's symbol",
        ),
        pytest.param(
            _insert_after("_t_pb_done_bit = tags['pb_tt_bit']", _DT_LOAD),
            id="a load of another tag after the loads",
        ),
        pytest.param(
            _insert_after("memory['_frac:pb_accumulator'] = dt", _DT_LOAD),
            id="a memory store among the loads",
        ),
    ],
)
def test_an_unrecorded_access_is_caught(inject: Any) -> None:
    rendered = render(SHAPES["TonReset"])
    source = inject(rendered.source)
    assert source != rendered.source
    assert uncovered(source, rendered.records, rendered.symbols)


def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(relations_module, "_CACHE", OrderedDict())


def test_uncovered_code_gives_opaque_relations(monkeypatch: pytest.MonkeyPatch) -> None:
    _fresh_cache(monkeypatch)
    real = render_module.render

    def render_with_raw_store(instr: Any, *, record: bool = True) -> Any:
        rendered = real(instr, record=record)
        source = _unmark(rendered.source, "False", "_t_pb_done_bit = ")
        return dataclasses.replace(rendered, source=source)

    monkeypatch.setattr(render_module, "render", render_with_raw_store)
    instruction = SHAPES["TonReset"]
    relations = relations_for(instruction)
    assert relations is not None and relations.opaque
    # Each answer is the most cautious one.
    assert relations.completion_levels is None
    assert instruction.advance_profile().completion_controls is None
    assert relations.snapshot_fields == {"reset_condition"}
    assert relations.zeroing_fields == {"reset_condition"}
    assert relations.edge_fields == {"enable_condition", "reset_condition"}
    # A timer steps on every enabled scan, not on a rise.
    assert not relations.counts_on_rise
    assert relations.motion_gates == ()


def test_an_instruction_with_no_stand_in_gets_opaque_relations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fresh_cache(monkeypatch)
    instruction = copy_module.copy(SHAPES["CtuUpDown"])
    instruction.unknown = object()
    relations = relations_for(instruction)
    assert relations is not None and relations.opaque
    assert relations.zeroing_fields == {"reset_condition", "down_condition"}
    # PILOT's counter questions get the answers pyrung kept by hand before.
    assert relations.count_holds("up_condition") == (("reset_condition", False),)
    assert relations.opposing_count_field == "down_condition"
    assert relations.counts_on_rise
    timer = copy_module.copy(SHAPES["TonReset"])
    timer.unknown = object()
    relations = relations_for(timer)
    assert relations is not None and relations.opaque
    assert not relations.counts_on_rise

    def refuse(instr: Any, *, record: bool = True) -> Any:
        raise render_module.RelationsError("refused")

    monkeypatch.setattr(render_module, "render", refuse)
    relations = relations_for(SHAPES["CtuReset"])
    assert relations is not None and relations.opaque


_RECORDING = {
    "OnDelayInstruction",
    "OffDelayInstruction",
    "CountUpInstruction",
    "CountDownInstruction",
}


def test_every_timer_and_counter_in_the_render_corpus_gets_relations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "devtools"))
    import render_corpus

    seen = 0
    for rel, build in render_corpus._corpus(None):
        # The apart build holds the suite's groups again, on memory of their own.
        if rel == "conformance/apart":
            continue
        built = build()
        for instruction in walk_instructions(getattr(built, "program", built)):
            if type(instruction).__name__ in _RECORDING:
                relations = relations_for(instruction)
                assert relations is not None and not relations.opaque, (rel, instruction)
                seen += 1
    assert seen > 100


def test_a_counter_on_a_rung_with_no_condition_counts_on_a_rise() -> None:
    with Program(strict=False) as program:
        with rung():
            count_up(Counter.clone("Always"), 5).reset(Rst)
    [instruction] = walk_instructions(program)
    relations = relations_for(instruction)
    assert relations is not None
    # Its input is a constant, so no field rises, but the count still waits for a rise.
    assert relations.edge_fields == frozenset()
    assert relations.counts_on_rise


def _relations(source: str, rendered: Any) -> Relations:
    return Relations(place(source, rendered.records), dict(rendered.fields), rendered.power)


def _places_of(relations: Relations, record: Any) -> list[Placed]:
    return [p for p in relations.placed if p.record == record]


_RESET_DONE = Store("pb_done_bit", Const(False))
_RESET_STORE = "_t_pb_done_bit = _r(5, False)"


def _reset_done_place(source: str, rendered: Any) -> Placed:
    [placed] = _places_of(_relations(source, rendered), _RESET_DONE)
    return placed


def test_a_later_operand_is_guarded_by_the_earlier_ones() -> None:
    rendered = render(SHAPES["TonReset"])
    # The TT bit reads Done only once the rung is on and the reset is off.
    [done_read] = _places_of(
        _relations(rendered.source, rendered), Read(TagValue("pb_done_bit"), False)
    )
    enable = Atom(Read(TagValue("pb_enable_condition"), False))
    reset = Atom(Read(TagValue("pb_reset_condition"), False))
    assert done_read.guard == ((enable, True), (Not(reset), True))

    tof = render(SHAPES["Tof"])
    [moved] = [p for p in _relations(tof.source, tof).placed if isinstance(p.record, Compare)]
    armed = Atom(Read(Memory("_tof_armed:pb_accumulator"), False))
    done = Atom(Read(TagValue("pb_done_bit"), False))
    assert moved.guard[-2:] == ((armed, False), (done, False))


@pytest.mark.parametrize("side", [True, False])
def test_a_ternary_test_guards_each_side(side: bool) -> None:
    rendered = render(SHAPES["TonReset"])
    assert _RESET_STORE in rendered.source
    body, other = ("_r(5, False)", "True") if side else ("True", "_r(5, False)")
    ternary = f"_t_pb_done_bit = {body} if _r(0, _t_pb_enable_condition) else {other}"
    placed = _reset_done_place(rendered.source.replace(_RESET_STORE, ternary), rendered)
    assert placed.guard[-1] == (Atom(Read(TagValue("pb_enable_condition"), False)), side)


@pytest.mark.parametrize(
    "loop",
    [
        pytest.param(f"for _i in range(2):\n            {_RESET_STORE}", id="for"),
        pytest.param(f"while _i:\n            {_RESET_STORE}", id="while"),
        pytest.param("_x = [_r(5, False) for _i in range(2)]", id="comprehension"),
    ],
)
def test_a_record_in_a_loop_is_only_necessary(loop: str) -> None:
    rendered = render(SHAPES["TonReset"])
    assert _RESET_STORE in rendered.source
    assert not _reset_done_place(rendered.source.replace(_RESET_STORE, loop), rendered).exact


@pytest.mark.parametrize(
    "rebind",
    [
        pytest.param("for _box_1_snap_0 in (0, 1):\n        pass", id="for target"),
        pytest.param("_box_1_snap_0, _x = (False, 0)", id="tuple target"),
        pytest.param("if (_box_1_snap_0 := False):\n        pass", id="walrus"),
        pytest.param("with open('x') as _box_1_snap_0:\n        pass", id="with as"),
        pytest.param(
            "try:\n        pass\n    except Exception as _box_1_snap_0:\n        pass",
            id="except as",
        ),
        pytest.param("_box_1_snap_0 = False", id="second assignment"),
    ],
)
def test_a_local_bound_twice_is_not_resolved(rebind: str) -> None:
    rendered = render(SHAPES["TonReset"])
    test = "    if _box_1_snap_0:"
    assert test in rendered.source
    source = rendered.source.replace(test, f"    {rebind}\n{test}", 1)
    assert _reset_done_place(source, rendered).guard == ((Opaque("_box_1_snap_0"), True),)


def test_a_record_pasted_twice_keeps_every_place() -> None:
    tof = render(SHAPES["Tof"])
    relations = _relations(tof.source, tof)
    assert len(_places_of(relations, Read(TagValue("pb_done_bit"), False))) == 2

    # Done's compare pasted into a second branch runs under either guard, so no one
    # list of levels is its completion control.
    rendered = render(SHAPES["TonReset"])
    [(_placed, compare)] = _relations(rendered.source, rendered).done_compares()
    marker = rendered.records.index(Store("pb_done_bit", compare))
    source = rendered.source.replace(
        _RESET_STORE, f"_t_pb_done_bit = _r({marker}, (_acc >= _preset))"
    )
    relations = _relations(source, rendered)
    assert len(relations.done_compares()) == 2
    assert relations.completion_levels is None


def test_a_reset_under_a_necessary_guard_still_zeroes() -> None:
    rendered = render(SHAPES["TonReset"])
    last = "    tags['pb_tt_bit'] = _t_pb_tt_bit"
    assert last in rendered.source
    relations = _relations(rendered.source.replace(last, f"{last}\n    return"), rendered)
    assert not any(p.exact for p in relations.placed)
    assert relations.zeroing_fields == {"reset_condition"}


def test_instructions_that_do_not_record_have_no_relations() -> None:
    from pyrung.core import copy

    with Program(strict=False) as program:
        with rung(En):
            copy(Preset, Int("Out"))
    [instruction] = walk_instructions(program)
    assert relations_for(instruction) is None


def test_a_reset_under_an_off_level_counts_that_level() -> None:
    rendered = render(SHAPES["CtuUpDown"])
    zero = "        _t_pb_accumulator = _r(11, 0)"
    assert zero in rendered.source and "_box_1_snap_1 = bool(_r(2, _t_pb_down_condition))" in (
        rendered.source
    )
    source = rendered.source.replace(zero, f"        if not _box_1_snap_1:\n    {zero}")
    assert _relations(source, rendered).zeroing_fields == {"reset_condition", "down_condition"}
