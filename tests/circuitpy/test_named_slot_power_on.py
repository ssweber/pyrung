"""Generated code powers a block's slots on as stepping does.

A tag holding a slot lives in the block's array, with no scalar line of its own, so
the array carries its default.  A pointer reads the same cell.  The interpreter is
the reference; the wire and the retentive save read the array too.
"""

from __future__ import annotations

import struct
from typing import Any

import pytest
from pyclickplc.modbus import plc_to_modbus

from pyrung.circuitpy import P1AM, ModbusServerConfig, generate_circuitpy
from pyrung.click import ClickBlocks, TagMap
from pyrung.core import PLC, Block, Bool, Int, Program, Real, Rung, TagType, copy, out
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
