"""Generated code powers a block's slots on as stepping does.

A tag holding a slot lives in the block's array, with no scalar line of its own, so
the array carries its default.  A pointer reads the same cell.  The interpreter is
the reference; the wire and the retentive save read the array too.
"""

from __future__ import annotations

import builtins
import enum
import json
import struct
from typing import Any

import pytest
from pyclickplc.modbus import plc_to_modbus

from pyrung.circuitpy import P1AM, ModbusServerConfig, RunStopConfig, generate_circuitpy
from pyrung.click import ClickBlocks, TagMap
from pyrung.core import PLC, Block, Bool, Dint, Int, Program, Real, Rung, TagType, copy, out
from tests.circuitpy.test_codegen import _run_single_scan_source
from tests.core.test_indirect_expr_compare import _StubBase


def _generate(program: Program, tag_map: TagMap | None = None) -> Any:
    hw = P1AM()
    hw.slot(1, "P1-08SIM")
    server = {} if tag_map is None else {"modbus_server": ModbusServerConfig(ip="192.168.1.200")}
    return generate_circuitpy(
        program, hw, target_scan_ms=10.0, runstop=None, tag_map=tag_map, **server
    )


def _run(result: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return _run_single_scan_source(
        result.code, monkeypatch, _StubBase(), runtime_source=result.runtime
    )


def _read_by_name_and_pointer(name: Any, slots: Block, addr: int, by_type: Any = Int) -> Program:
    index = Int("Jx", default=addr)
    with Program(strict=False) as program:
        with Rung():
            copy(name, by_type("By"))
        with Rung():
            copy(slots[index], by_type("Seen"))
    return program


def _tag_map_to(retentive: bool) -> Program:
    slots = Block("Cb", TagType.INT, 1, 8)
    name = Int("Cq", default=9, retentive=retentive)
    name.map_to(slots[3])
    return _read_by_name_and_pointer(name, slots, 3)


def _real() -> Program:
    slots = Block("Fl", TagType.REAL, 1, 8)
    name = Real("Rt", default=1.5)
    name.map_to(slots[3])
    return _read_by_name_and_pointer(name, slots, 3, Real)


def _real_on_an_int_block() -> Program:
    slots = Block("Cb", TagType.INT, 1, 8)
    name = Real("Rt", default=1.5)
    name.map_to(slots[3])
    return _read_by_name_and_pointer(name, slots, 3, Real)


def _same_spelling() -> Program:
    slots = Block("Cb", TagType.INT, 1, 8)
    return _read_by_name_and_pointer(Int("Cb3", default=7), slots, 3)


def _name_on_a_slot_with_its_own_default() -> Program:
    """The name wins, even with no default of its own: it is what the engines seed."""
    slots = Block("Cb", TagType.INT, 1, 8)
    slots.slot(3, name="Nm", default=5)
    return _read_by_name_and_pointer(Int("Nm"), slots, 3)


def _slot_named_with_a_default() -> Program:
    slots = Block("Cb", TagType.INT, 1, 8)
    slots.slot(3, name="Nm", default=5)
    return _read_by_name_and_pointer(slots[3], slots, 3)


def _block_map_slot_default() -> Program:
    bank = Block("Dz", TagType.INT, 1, 8)
    logical = Block("Lg", TagType.INT, 1, 4)
    logical.slot(2, name="Lim", default=44)
    logical.map_to(bank.select(3, 6))
    return _read_by_name_and_pointer(logical[2], bank, 4)


def _slot_default_read_only_through_the_pointer() -> Program:
    slots = Block("Cb", TagType.INT, 1, 8)
    slots.slot(3, default=5)
    with Program(strict=False) as program:
        with Rung():
            copy(slots[Int("Jx", default=3)], Int("Seen"))
    return program


def _slot_range_default() -> Program:
    slots = Block("Cb", TagType.INT, 1, 8)
    slots.slot(2, 6, default=5)
    with Program(strict=False) as program:
        with Rung():
            copy(slots[Int("Jx", default=6)], Int("Seen"))
    return program


def _default_factory() -> Program:
    slots = Block("Cb", TagType.INT, 1, 8, default_factory=lambda addr: addr * 10)
    with Program(strict=False) as program:
        with Rung():
            copy(slots[Int("Jx", default=3)], Int("Seen"))
    return program


def _sparse_block() -> Program:
    slots = Block("Sp", TagType.INT, 1, 200, valid_ranges=((1, 8), (101, 108)))
    slots.slot(102, default=6)
    with Program(strict=False) as program:
        with Rung():
            copy(slots[Int("Jx", default=102)], Int("Seen"))
    return program


def _bool_contact() -> Program:
    slots = Block("Fg", TagType.BOOL, 1, 8)
    name = Bool("En", default=True)
    name.map_to(slots[3])
    with Program(strict=False) as program:
        with Rung(name):
            out(Bool("By"))
        with Rung():
            copy(slots[Int("Jx", default=3)], Bool("Seen"))
    return program


def _comparison_on_a_setpoint() -> Program:
    slots = Block("Cb", TagType.INT, 1, 8)
    setpoint = Int("Setpoint", default=100)
    setpoint.map_to(slots[3])
    with Program(strict=False) as program:
        with Rung(Int("Pv", default=50) < setpoint):
            out(Bool("By"))
        with Rung():
            copy(slots[Int("Jx", default=3)], Int("Seen"))
    return program


PROGRAMS = [
    pytest.param(lambda: _tag_map_to(True), id="int-retentive"),
    pytest.param(lambda: _tag_map_to(False), id="int-non-retentive"),
    pytest.param(_real, id="real"),
    pytest.param(_real_on_an_int_block, id="real-on-int-block"),
    pytest.param(_same_spelling, id="same-spelling"),
    pytest.param(_name_on_a_slot_with_its_own_default, id="name-over-slot-default"),
    pytest.param(_slot_named_with_a_default, id="slot-name-and-default"),
    pytest.param(_block_map_slot_default, id="block-map-slot-default"),
    pytest.param(_slot_default_read_only_through_the_pointer, id="pointer-only"),
    pytest.param(_slot_range_default, id="slot-range"),
    pytest.param(_default_factory, id="default-factory"),
    pytest.param(_sparse_block, id="sparse-block"),
    pytest.param(_bool_contact, id="bool-contact"),
    pytest.param(_comparison_on_a_setpoint, id="comparison"),
]


@pytest.mark.parametrize("build", PROGRAMS)
def test_generated_first_scan_matches_the_interpreter(
    build: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = PLC(build(), dt=0.010)
    runner.step()
    namespace = _run(_generate(build()), monkeypatch)
    watched = [name for name in ("By", "Seen") if f"_t_{name}" in namespace]
    assert watched
    assert {name: namespace[f"_t_{name}"] for name in watched} == {
        name: runner.current_state.tags[name] for name in watched
    }


def test_a_big_block_seeds_only_what_differs() -> None:
    def build(*defaults: tuple[int, int]) -> str:
        slots = Block("Bg", TagType.INT, 1, 4500)
        for addr, value in defaults:
            slots.slot(addr, default=value)
        with Program(strict=False) as program:
            with Rung():
                copy(slots[Int("Jx", default=31)], Int("Seen"))
        return _generate(program).code

    plain = build()
    seeded = build((31, 9), (4000, 8))
    assert [line for line in plain.splitlines() if line.startswith("_b_Bg")] == [
        "_b_Bg = [0] * 4500"
    ]
    assert [line for line in seeded.splitlines() if line.startswith("_b_Bg")] == [
        "_b_Bg = [0] * 4500",
        "_b_Bg[30] = 9",
        "_b_Bg[3999] = 8",
    ]
    assert len(seeded) - len(plain) < 100


def test_a_run_of_equal_defaults_is_one_loop() -> None:
    assert "for _i in range(1, 6):\n    _b_Cb[_i] = 5\n" in _generate(_slot_range_default()).code


class _Sink:
    def __enter__(self) -> _Sink:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _saved_values(namespace: dict[str, Any]) -> dict[str, Any]:
    """What ``save_memory`` writes, with the card and the files stubbed out."""
    saved: dict[str, Any] = {}

    class _Json:
        @staticmethod
        def dump(payload: dict[str, Any], _file: object) -> None:
            saved.update(payload["values"])

    class _Os:
        @staticmethod
        def rename(_old: str, _new: str) -> None:
            return None

        @staticmethod
        def remove(_path: str) -> None:
            return None

    namespace.update(
        _sd_available=True,
        json=_Json,
        os=_Os,
        open=lambda *_args, **_kwargs: _Sink(),
    )
    namespace["save_memory"]()
    return saved


def test_a_retentive_name_at_its_default_is_not_saved(monkeypatch: pytest.MonkeyPatch) -> None:
    namespace = _run(_generate(_tag_map_to(True)), monkeypatch)
    assert namespace["_RET_DEFAULTS"]["Cq"] == 9
    assert namespace["_b_Cb"][2] == 9
    assert "Cq" not in _saved_values(namespace)

    namespace["_b_Cb"][2] = 0
    assert _saved_values(namespace)["Cq"] == {"type": "INT", "value": 0}


def _mbap(pdu: bytes) -> bytes:
    return struct.pack(">HHHB", 1, 0, len(pdu) + 1, 1) + pdu


def _wire_read(namespace: dict[str, Any], bank: str, index: int) -> bytes:
    """The response PDU of a one-register read."""
    addr, _ = plc_to_modbus(bank, index)
    return namespace["_mb_handle"](_mbap(struct.pack(">BHH", 3, addr, 1)), 12)[7:]


def test_the_wire_reads_a_mapped_default(monkeypatch: pytest.MonkeyPatch) -> None:
    blocks = ClickBlocks()
    name = Int("Cq", default=9)
    program = _read_by_name_and_pointer(name, blocks.ds, 31)
    result = _generate(program, TagMap({name: blocks.ds[31]}, include_system=False))
    namespace = _run(result, monkeypatch)
    assert namespace["_t_By"] == 9
    assert _wire_read(namespace, "DS", 31) == struct.pack(">BBH", 3, 2, 9)


def test_the_wire_serves_a_name_whatever_was_indexed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A name on a raw register with no TagMap row, in a bank the program keeps an array of."""
    blocks = ClickBlocks()
    name = Int("Cp", default=6)
    name.map_to(blocks.ds[30])
    by = Int("By")
    program = _read_by_name_and_pointer(name, blocks.ds, 30)
    tag_map = TagMap({by: blocks.ds[100]}, include_system=False)

    before = _generate(program, tag_map)
    assert blocks.ds[30] is name
    after = _generate(program, tag_map)
    assert before.code == after.code
    assert before.runtime == after.runtime

    namespace = _run(before, monkeypatch)
    assert _wire_read(namespace, "DS", 30) == struct.pack(">BBH", 3, 2, 6)


def test_the_wire_refuses_a_cell_no_register_can_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not a crash in ``_mb_handle``: the program keeps running, and the next read works."""
    for indexed_first in (False, True):
        blocks = ClickBlocks()
        name = Dint("Cp", default=100000)
        name.map_to(blocks.ds[30])
        if indexed_first:
            assert blocks.ds[30] is name
        program = _read_by_name_and_pointer(name, blocks.ds, 30, Dint)
        tag_map = TagMap({Int("Fill", default=4): blocks.ds[100]}, include_system=False)
        namespace = _run(_generate(program, tag_map), monkeypatch)
        assert namespace["_b_DS"][29] == 100000
        assert _wire_read(namespace, "DS", 30) == bytes([0x83, 2])
        namespace["_b_DS"][29] = 5
        assert _wire_read(namespace, "DS", 30) == struct.pack(">BBH", 3, 2, 5)


def test_the_wire_refuses_a_slot_default_no_register_can_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocks = ClickBlocks()
    blocks.ds.slot(30, default=99999)
    by = Int("By")
    with Program(strict=False) as program:
        with Rung():
            copy(blocks.ds[Int("Jx", default=31)], by)
    tag_map = TagMap({by: blocks.ds[100]}, include_system=False)
    namespace = _run(_generate(program, tag_map), monkeypatch)
    assert _wire_read(namespace, "DS", 30) == bytes([0x83, 2])


def test_a_wire_write_beside_a_cell_no_register_can_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    """A one-register write to a DD reads the other half of the cell first."""
    blocks = ClickBlocks()
    name = Real("Rz", default=5e9)
    name.map_to(blocks.dd[30])
    by = Dint("By")
    program = _read_by_name_and_pointer(name, blocks.dd, 30, Real)
    tag_map = TagMap({by: blocks.dd[100]}, include_system=False)
    namespace = _run(_generate(program, tag_map), monkeypatch)
    addr, _ = plc_to_modbus("DD", 30)
    response = namespace["_mb_handle"](_mbap(struct.pack(">BHH", 6, addr, 1)), 12)
    assert response[7:] == bytes([0x86, 2])
    assert namespace["_b_DD"][29] == 5e9


class _Color(enum.IntEnum):
    RED = 1
    BLUE = 2


def _read_slot_through_a_pointer(slots: Block, addr: int) -> Program:
    with Program(strict=False) as program:
        with Rung():
            copy(slots[Int("Jx", default=addr)], Int("Seen"))
    return program


def test_an_enum_default_is_seeded_as_its_value(monkeypatch: pytest.MonkeyPatch) -> None:
    def from_slot() -> Program:
        slots = Block("Cb", TagType.INT, 1, 8)
        slots.slot(3, default=_Color.BLUE)
        return _read_slot_through_a_pointer(slots, 3)

    def from_factory() -> Program:
        slots = Block("Cb", TagType.INT, 1, 8, default_factory=lambda _addr: _Color.BLUE)
        return _read_slot_through_a_pointer(slots, 3)

    for build in (from_slot, from_factory):
        runner = PLC(build(), dt=0.010)
        runner.step()
        namespace = _run(_generate(build()), monkeypatch)
        assert namespace["_t_Seen"] == runner.current_state.tags["Seen"] == 2


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_a_default_no_literal_can_spell_stays_at_zero(
    value: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only reachable on a slot no scan reaches: the interpreter's tag would refuse it."""
    slots = Block("Fl", TagType.REAL, 1, 8)
    slots.slot(5, default=value)
    namespace = _run(_generate(_read_slot_through_a_pointer(slots, 3)), monkeypatch)
    assert namespace["_b_Fl"][4] == 0.0


def _counting_factory() -> tuple[Block, dict[str, int]]:
    draws = {"count": 0}

    def counter(_addr: int) -> int:
        draws["count"] += 1
        return draws["count"]

    return Block("Cb", TagType.INT, 1, 8, default_factory=counter), draws


def test_a_counting_factory_seeds_each_slot_its_answer_in_address_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The factory is asked once per slot, lowest address first, whichever slot is read first."""
    slots, draws = _counting_factory()
    program = _read_slot_through_a_pointer(slots, 3)
    first = _generate(program)
    assert draws["count"] == 8

    runner = PLC(program, dt=0.010)
    runner.step()
    assert slots[6].default == 6
    assert _generate(program).code == first.code
    assert draws["count"] == 8

    namespace = _run(first, monkeypatch)
    assert namespace["_t_Seen"] == runner.current_state.tags["Seen"] == 3
    assert namespace["_b_Cb"] == [1, 2, 3, 4, 5, 6, 7, 8]


def test_an_export_after_a_run_is_the_export_before_it(monkeypatch: pytest.MonkeyPatch) -> None:
    def build() -> tuple[Program, dict[str, int]]:
        slots, draws = _counting_factory()
        return _read_slot_through_a_pointer(slots, 3), draws

    program, _draws = build()
    before = _generate(program).code

    program, _draws = build()
    runner = PLC(program, dt=0.010)
    runner.patch({"Jx": 7})
    runner.step()
    after = _generate(program)
    assert after.code == before

    program, _draws = build()
    stepped = PLC(program, dt=0.010)
    stepped.step()
    assert _run(after, monkeypatch)["_t_Seen"] == stepped.current_state.tags["Seen"] == 3


def test_a_partial_factory_seeds_what_it_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The interpreter raises on reaching a slot the factory raises for, so that one is free."""

    def build() -> Program:
        slots = Block("Cb", TagType.INT, 1, 8, default_factory=lambda addr: {3: 30}[addr])
        return _read_slot_through_a_pointer(slots, 3)

    runner = PLC(build(), dt=0.010)
    runner.step()
    namespace = _run(_generate(build()), monkeypatch)
    assert namespace["_t_Seen"] == runner.current_state.tags["Seen"] == 30
    assert namespace["_b_Cb"] == [0, 0, 30, 0, 0, 0, 0, 0]


def test_a_factory_that_answers_once_seeds_only_the_first_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = iter([7])

    def once(_addr: int) -> int:
        return next(calls)

    slots = Block("Cb", TagType.INT, 1, 8, default_factory=once)
    namespace = _run(_generate(_read_slot_through_a_pointer(slots, 3)), monkeypatch)
    assert namespace["_b_Cb"] == [7, 0, 0, 0, 0, 0, 0, 0]
    assert slots[1].default == 7


def test_a_pure_factory_generates_the_same_code_whatever_was_indexed() -> None:
    slots = Block("Cb", TagType.INT, 1, 8, default_factory=lambda addr: addr * 10)
    program = _read_slot_through_a_pointer(slots, 3)
    before = _generate(program).code
    assert slots[3].default == 30
    assert _generate(program).code == before


# -- A restart: the array powers on, then the card's values load over it.


def _attach_card(namespace: dict[str, Any], card: dict[str, Any]) -> None:
    class _Json:
        @staticmethod
        def dump(payload: dict[str, Any], _file: object) -> None:
            card["payload"] = json.loads(json.dumps(payload))

        @staticmethod
        def load(_file: object) -> dict[str, Any]:
            return card["payload"]

    class _Os:
        @staticmethod
        def rename(_old: str, _new: str) -> None:
            return None

        @staticmethod
        def remove(_path: str) -> None:
            return None

    namespace.update(
        _sd_available=True,
        json=_Json,
        os=_Os,
        open=lambda *_args, **_kwargs: _Sink(),
    )


@pytest.mark.parametrize("written, expected", [(0, 0), (4, 4), (9, 9)])
def test_a_retentive_name_loads_over_its_seed_after_a_restart(
    written: int, expected: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    card: dict[str, Any] = {}
    namespace = _run(_generate(_tag_map_to(True)), monkeypatch)
    namespace["_b_Cb"][2] = written
    _attach_card(namespace, card)
    namespace["save_memory"]()

    restarted = _run(_generate(_tag_map_to(True)), monkeypatch)
    assert restarted["_b_Cb"][2] == 9
    _attach_card(restarted, card)
    restarted["load_memory"]()
    assert restarted["_b_Cb"][2] == expected


# -- STOP->RUN, driven through ``board.SWITCH``.  Scan 2 writes 7 through the pointer.
# The switch's debounce takes a scan, so _RUN_STOP_RUN is in STOP for scans 5 and 6.

_RUN_STOP_RUN = [True, True, True, False, False, True, True, True]
_BOOT_IN_STOP = [False, False, True, True, True]


def _scan_with_switch(
    program: Program,
    switch: list[bool],
    monkeypatch: pytest.MonkeyPatch,
    *,
    hw: P1AM | None = None,
    stub: Any = None,
) -> dict[str, Any]:
    """Run the generated code with ``board.SWITCH`` reading ``switch``, one entry per scan."""
    if hw is None:
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
    result = generate_circuitpy(
        program,
        hw,
        target_scan_ms=10.0,
        runstop=RunStopConfig(debounce_ms=0, run_when_high=True),
    )
    marker = "    scan_start_ns = time.monotonic_ns()"
    assert marker in result.code
    code = result.code.replace(marker, f"    __scan_hook(globals())\n{marker}", 1)
    scans = {"count": 0}

    def hook(namespace: dict[str, Any]) -> None:
        scans["count"] += 1
        if "_t_Tr" in namespace:
            namespace["_t_Tr"] = scans["count"] == 2
        namespace["_board_switch_io"].value = switch[scans["count"] - 1]
        if stub is not None:
            stub.scan = scans["count"]

    monkeypatch.setattr(builtins, "__scan_hook", hook, raising=False)
    return _run_single_scan_source(
        code,
        monkeypatch,
        _StubBase() if stub is None else stub,
        runtime_source=result.runtime,
        scans=len(switch),
    )


def _interpreter_after_stop_run(program: Program) -> Any:
    runner = PLC(program, dt=0.010)
    runner.step()
    runner.patch({"Tr": True})
    runner.step()
    runner.step()
    runner.stop()
    runner.start()
    runner.step()
    return runner.current_state.tags["Seen"]


def _written_through_a_pointer(slots: Block, addr: int, *rungs: Any) -> Program:
    index = Int("Jx", default=addr, retentive=True)
    with Program(strict=False) as program:
        with Rung(Bool("Tr", retentive=False)):
            copy(7, slots[index])
        with Rung():
            copy(slots[index], Int("Seen", retentive=False))
        for source, target in rungs:
            with Rung():
                copy(source, target)
    return program


def _pointer_only_slot_default() -> Program:
    slots = Block("Cb", TagType.INT, 1, 8)
    slots.slot(3, default=5, retentive=False)
    return _written_through_a_pointer(slots, 3)


def _pointer_only_slot_zero() -> Program:
    slots = Block("Cb", TagType.INT, 1, 8)
    slots.slot(3, default=0, retentive=False)
    return _written_through_a_pointer(slots, 3)


def _pointer_only_no_default() -> Program:
    return _written_through_a_pointer(Block("Cb", TagType.INT, 1, 8), 3)


def _pointer_only_on_a_retentive_block() -> Program:
    slots = Block("Cb", TagType.INT, 1, 8, retentive=True)
    slots.slot(3, default=5, retentive=False)
    return _written_through_a_pointer(slots, 3)


def _pointer_only_factory() -> Program:
    slots = Block("Cb", TagType.INT, 1, 8, default_factory=lambda addr: addr * 10)
    return _written_through_a_pointer(slots, 3)


def _named_slot() -> Program:
    slots = Block("Cb", TagType.INT, 1, 8)
    name = Int("Cq", default=9, retentive=False)
    name.map_to(slots[3])
    return _written_through_a_pointer(slots, 3, (name, Int("By", retentive=False)))


def _named_retentive_slot() -> Program:
    slots = Block("Cb", TagType.INT, 1, 8)
    name = Int("Cq", default=9, retentive=True)
    name.map_to(slots[3])
    return _written_through_a_pointer(slots, 3, (name, Int("By", retentive=False)))


def _block_map_pointer_only() -> Program:
    bank = Block("Dz", TagType.INT, 1, 8)
    logical = Block("Lg", TagType.INT, 1, 4)
    logical.slot(2, default=44, retentive=False)
    logical.map_to(bank.select(3, 6))
    return _written_through_a_pointer(bank, 4)


STOP_RUN_PROGRAMS = [
    pytest.param(_pointer_only_slot_default, id="pointer-only-default"),
    pytest.param(_pointer_only_slot_zero, id="pointer-only-zero"),
    pytest.param(_pointer_only_no_default, id="pointer-only-none"),
    pytest.param(_pointer_only_on_a_retentive_block, id="non-retentive-on-retentive-block"),
    pytest.param(_pointer_only_factory, id="factory"),
    pytest.param(_named_slot, id="named"),
    pytest.param(_named_retentive_slot, id="named-retentive"),
    pytest.param(_block_map_pointer_only, id="block-map"),
]


@pytest.mark.parametrize("build", STOP_RUN_PROGRAMS)
def test_stop_to_run_matches_the_interpreter(build: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    namespace = _scan_with_switch(build(), _RUN_STOP_RUN, monkeypatch)
    assert namespace["_t_Seen"] == _interpreter_after_stop_run(build())


@pytest.mark.parametrize("default", [5, 0])
def test_a_retentive_slot_keeps_its_value_across_stop_to_run(
    default: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reached only by a pointer.  The interpreter loses this one, which is its own bug."""
    slots = Block("Cb", TagType.INT, 1, 8)
    slots.slot(3, default=default, retentive=True)
    namespace = _scan_with_switch(_written_through_a_pointer(slots, 3), _RUN_STOP_RUN, monkeypatch)
    assert namespace["_t_Seen"] == 7
    assert namespace["_b_Cb"][2] == 7


def test_an_input_block_keeps_this_scans_read_at_stop_to_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read comes before the reset, so a reset would blank the first RUN scan."""
    hw = P1AM()
    inputs = hw.slot(1, "P1-08SIM")
    index = Int("Jx", default=2, retentive=True)
    with Program(strict=False) as program:
        with Rung():
            copy(inputs[index], Bool("Seen", retentive=False))

    class _Reads(_StubBase):
        def readDiscrete(self, *_args: object, **_kwargs: object) -> int:
            return 0b10

    namespace = _scan_with_switch(program, _RUN_STOP_RUN, monkeypatch, hw=hw, stub=_Reads())
    assert namespace["_t_Seen"] is True


class _RecordsWrites(_StubBase):
    def __init__(self) -> None:
        self.scan = 0
        self.masks: dict[int, int] = {}

    def readDiscrete(self, *_args: object, **_kwargs: object) -> int:
        return 0

    def writeDiscrete(self, mask: int, _slot: int) -> None:
        self.masks[self.scan] = mask


def _output_slot_program(*, retentive: bool = False, default: bool = True) -> tuple[P1AM, Program]:
    hw = P1AM()
    outputs = hw.slot(1, "P1-08TRS")
    outputs.slot(2, default=default, retentive=retentive)
    index = Int("Jx", default=2, retentive=True)
    with Program(strict=False) as program:
        with Rung(Bool("Tr", retentive=False)):
            copy(not default, outputs[index])
        with Rung():
            copy(outputs[index], Bool("Seen", retentive=False))
    return hw, program


def _output_masks(switch: list[bool], monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> list[int]:
    hw, program = _output_slot_program(**kwargs)
    stub = _RecordsWrites()
    _scan_with_switch(program, switch, monkeypatch, hw=hw, stub=stub)
    return [stub.masks[scan] for scan in sorted(stub.masks)]


def test_a_pointer_only_output_energises_again_after_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off in STOP, and back at its power-on True at RUN; scan 2 wrote it False."""
    assert _output_masks(_RUN_STOP_RUN, monkeypatch) == [2, 0, 0, 0, 0, 0, 2, 2]


def test_a_pointer_only_output_energises_after_booting_in_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _output_masks(_BOOT_IN_STOP, monkeypatch) == [0, 0, 0, 2, 2]


def test_a_retentive_output_keeps_its_value_across_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scan 2 writes it True; STOP turns the output off, and RUN finds the True again."""
    masks = _output_masks(_RUN_STOP_RUN, monkeypatch, retentive=True, default=False)
    assert masks == [0, 2, 2, 2, 0, 0, 2, 2]


def _reset_body(program: Program) -> list[str]:
    hw = P1AM()
    hw.slot(1, "P1-08SIM")
    code = generate_circuitpy(
        program, hw, target_scan_ms=10.0, runstop=RunStopConfig(debounce_ms=0)
    ).code
    body = code.split("def _reset_for_run_transition():", 1)[1].split("\ndef ", 1)[0]
    return [line.strip() for line in body.splitlines() if "_b_Bg" in line or "_i in" in line]


def test_the_reset_of_a_big_block_is_small() -> None:
    plain = Block("Bg", TagType.INT, 1, 65535)
    assert _reset_body(_read_slot_through_a_pointer(plain, 31)) == [
        "for _i in range(0, 65535):",
        "_b_Bg[_i] = 0",
    ]

    seeded = Block("Bg", TagType.INT, 1, 65535)
    seeded.slot(31, default=9)
    seeded.slot(40000, retentive=True, default=8)
    assert _reset_body(_read_slot_through_a_pointer(seeded, 31)) == [
        "for _i in range(0, 39999):",
        "_b_Bg[_i] = 0",
        "for _i in range(40000, 65535):",
        "_b_Bg[_i] = 0",
        "_b_Bg[30] = 9",
    ]

    retentive = Block("Bg", TagType.INT, 1, 65535, retentive=True)
    retentive.slot(31, default=9, retentive=False)
    assert _reset_body(_read_slot_through_a_pointer(retentive, 31)) == [
        "_b_Bg[30] = 0",
        "_b_Bg[30] = 9",
    ]
