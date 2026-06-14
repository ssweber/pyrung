"""Bank slot identity: one tag per hardware register.

``map_to`` stamps slot identity onto the singleton Click banks at call time,
and codegen emits nicknamed scalars as bank slot config + block references,
so direct references and indirect reads (``ds[expr]``) resolve to the same
state key — in simulation and export alike.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pyclickplc
from pyclickplc.addresses import AddressRecord, get_addr_key
from pyclickplc.banks import DataType

from pyrung import PLC
from pyrung.click import TagMap, c, ds, ladder_to_pyrung, pyrung_to_ladder, reset_banks, x
from pyrung.core import Bool, Int, Program, rung
from pyrung.core.program import copy
from tests.click.helpers import exec_with_source

# ---------------------------------------------------------------------------
# map_to stamps slot identity immediately (no TagMap required)
# ---------------------------------------------------------------------------


def test_map_to_stamps_name_and_metadata_immediately():
    cfg = Int(
        "JumpCfg",
        comment="jump target",
        choices={0: "Idle", 4: "Resetting"},
    )
    cfg.map_to(ds[165])

    slot = ds.slot(165)
    assert slot.name == "JumpCfg"
    assert slot.comment == "jump target"
    assert slot.choices == {0: "Idle", 4: "Resetting"}


def test_map_to_stamps_range_metadata_immediately():
    pressure = Int("PressureSetpoint", min=0, max=100, uom="psi")
    pressure.map_to(ds[168])

    slot = ds.slot(168)
    assert slot.name == "PressureSetpoint"
    assert slot.min == 0
    assert slot.max == 100
    assert slot.uom == "psi"


def test_map_to_stamps_default_on_non_retentive_slot():
    run = Bool("RunCmd", default=True)
    run.map_to(c[100])
    slot = c.slot(100)
    assert slot.name == "RunCmd"
    assert slot.default is True


def test_map_to_skips_default_on_retentive_slot():
    # DS is retentive by bank default: the initial value is dead data on
    # retentive registers, so map_to must not stamp it.
    cfg = Int("RetCfg", default=42)
    cfg.map_to(ds[166])
    slot = ds.slot(166)
    assert slot.name == "RetCfg"
    assert slot.default == 0


def test_map_to_stamping_is_idempotent():
    cfg = Int("IdemCfg", comment="same twice")
    cfg.map_to(ds[167])
    cfg.map_to(ds[167])  # identical re-application is a no-op
    assert ds.slot(167).name == "IdemCfg"


# ---------------------------------------------------------------------------
# Simulation == export semantics: indirect reads see the semantic identity
# ---------------------------------------------------------------------------


def _run_indirect_copy(build_map: bool) -> object:
    """Program whose ONLY access to c[50] is indirect; returns Dest value."""
    reset_banks()
    flag = Bool("SealFlag", default=True)
    flag.map_to(c[50])
    idx = Int("Idx", default=50)
    dest = Bool("Dest")

    with Program() as logic:
        with rung():
            copy(c[idx], dest)

    if build_map:
        TagMap({flag: c[50], idx: ds[1], dest: c[60]}, include_system=False)

    plc = PLC(logic)
    plc.step()
    return plc.state.tags.get("Dest")


def test_indirect_read_resolves_semantic_default_without_tagmap():
    # Simulate-first: map_to alone aliases the slot; the indirect read sees
    # the semantic tag's default (True), not the raw slot default (False).
    assert _run_indirect_copy(build_map=False) is True


def test_simulation_matches_export_semantics():
    # Behavior must be identical with and without TagMap construction.
    assert _run_indirect_copy(build_map=False) == _run_indirect_copy(build_map=True)


# ---------------------------------------------------------------------------
# Universal codegen emission: indirect-only nicknamed address with initial value
# ---------------------------------------------------------------------------


def _write_nickname_csv(path: Path, records: dict[int, AddressRecord]) -> Path:
    pyclickplc.write_csv(path, records)
    return path


def test_codegen_emits_slot_for_indirect_only_nicknamed_address(tmp_path: Path):
    """The headline case: a config register whose ONLY access is indirect.

    The nicknamed address never appears as a static operand, so slot config
    must come from the injected-mapped-tags path — and the executed artifact
    must model the configured value end-to-end.
    """
    reset_banks()
    CfgIdx = Int("CfgIdx")
    Dest = Int("Dest")

    with Program() as logic:
        with rung():
            copy(ds[CfgIdx], Dest)

    mapping = TagMap({CfgIdx: ds[200], Dest: ds[300]}, include_system=False)
    bundle = pyrung_to_ladder(logic, mapping)
    csv_dir = tmp_path / "csv_out"
    bundle.write(csv_dir)

    nick_path = _write_nickname_csv(
        tmp_path / "nicknames.csv",
        {
            get_addr_key("DS", 165): AddressRecord(
                memory_type="DS",
                address=165,
                nickname="sm__JUMPRESETTING2IDLE",
                comment="",
                initial_value="4",
                retentive=False,
                data_type=DataType.INT,
            ),
            get_addr_key("DS", 200): AddressRecord(
                memory_type="DS",
                address=200,
                nickname="CfgIdx",
                comment="",
                initial_value="165",
                retentive=False,
                data_type=DataType.INT,
            ),
            get_addr_key("DS", 300): AddressRecord(
                memory_type="DS",
                address=300,
                nickname="Dest",
                comment="",
                initial_value="0",
                retentive=False,
                data_type=DataType.INT,
            ),
        },
    )

    code = ladder_to_pyrung(csv_dir / "main.csv", nickname_csv=nick_path)

    # Tag-centric: standalone declaration + TagMap entry
    assert 'sm__JUMPRESETTING2IDLE = Int("sm__JUMPRESETTING2IDLE", default=4)' in code
    assert "sm__JUMPRESETTING2IDLE.map_to(ds[165])" in code or "sm__JUMPRESETTING2IDLE: ds[165]" in code
    assert "sm__JUMPRESETTING2IDLE = ds[165]" not in code
    assert 'ds.slot(165, name="sm__JUMPRESETTING2IDLE"' not in code

    # Executed artifact: the indirect read models the configured jump target
    ns: dict = {}
    exec_with_source(code, ns)
    plc = PLC(ns["logic"])
    plc.step()
    assert plc.state.tags.get("Dest") == 4


def test_codegen_structure_owned_addresses_keep_structure_emission(tmp_path: Path):
    """Structure-owned slots must not get scalar slot config (no double claim)."""
    reset_banks()
    Enable = Bool("Enable")
    Ch_id = Int("Channel1_id")

    with Program() as logic:
        with rung(Enable):
            copy(Ch_id, Ch_id)

    mapping = TagMap({Enable: x[1], Ch_id: ds[101]}, include_system=False)
    bundle = pyrung_to_ladder(logic, mapping)
    csv_dir = tmp_path / "csv_out"
    bundle.write(csv_dir)

    nick_path = _write_nickname_csv(
        tmp_path / "nicknames.csv",
        {
            get_addr_key("DS", 101): AddressRecord(
                memory_type="DS",
                address=101,
                nickname="Channel1_id",
                comment="<Channel:named_array(1,2)>",
                initial_value="0",
                retentive=False,
                data_type=DataType.INT,
            ),
            get_addr_key("DS", 102): AddressRecord(
                memory_type="DS",
                address=102,
                nickname="Channel1_val",
                comment="</Channel:named_array(1,2)>",
                initial_value="0",
                retentive=False,
                data_type=DataType.INT,
            ),
            get_addr_key("X", 1): AddressRecord(
                memory_type="X",
                address=1,
                nickname="Enable",
                comment="",
                initial_value="0",
                retentive=False,
                data_type=DataType.BIT,
            ),
        },
    )

    code = ladder_to_pyrung(csv_dir / "main.csv", nickname_csv=nick_path)
    assert "class Channel:" in code
    assert "ds.slot(101" not in code
    assert "ds.slot(102" not in code


# ---------------------------------------------------------------------------
# Nickname CSV round-trip: bank-slot identity survives without TagMap entries
# ---------------------------------------------------------------------------


def test_to_nickname_file_emits_bank_slot_scalars(tmp_path: Path):
    reset_banks()
    ds.slot(42, name="CfgWord", default=7)
    tm = TagMap({}, include_system=False)

    path = tmp_path / "nicknames.csv"
    tm.to_nickname_file(path)

    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    ds42 = next(row for row in rows if row and row[0] == "DS42")
    assert "CfgWord" in ds42
    assert "7" in ds42
