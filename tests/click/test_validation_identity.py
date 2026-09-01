"""Click validation for logical identities that share hardware."""

from pyrung import Bool, Program, Rung, out
from pyrung.click import ClickBlocks, TagMap
from pyrung.click.validation import CLK_ADDRESS_IDENTITY_CONFLICT, CLK_BANK_UNRESOLVED


def _strict_codes(logic: Program, mapping: TagMap) -> set[str]:
    report = mapping.validate(logic, mode="strict")
    return {finding.code for finding in report.errors}


def test_named_click_bank_slot_resolves_without_tag_map_entry():
    blocks = ClickBlocks()
    blocks.x.slot(1, name="Pedal1", external=True)
    blocks.c.slot(1, name="RecordTopThickness")

    with Program() as logic:
        with Rung(blocks.x[1]):
            out(blocks.c[1])

    codes = _strict_codes(logic, TagMap({}, include_system=False))

    assert CLK_BANK_UNRESOLVED not in codes
    assert CLK_ADDRESS_IDENTITY_CONFLICT not in codes


def test_named_click_bank_slot_conflicts_with_raw_address_identity():
    blocks = ClickBlocks()
    blocks.x.slot(1, name="Pedal1", external=True)
    RawX1 = Bool("X1")

    with Program() as logic:
        with Rung(blocks.x[1], RawX1):
            out(Bool("C1"))

    report = TagMap({}, include_system=False).validate(logic, mode="strict")
    conflicts = [
        finding for finding in report.errors if finding.code == CLK_ADDRESS_IDENTITY_CONFLICT
    ]

    assert len(conflicts) == 1
    assert "X001" in conflicts[0].message
    assert "'Pedal1', 'X1'" in conflicts[0].message


def test_padded_and_unpadded_raw_addresses_conflict():
    RawX1 = Bool("X1")
    RawX001 = Bool("X001")

    with Program() as logic:
        with Rung(RawX1, RawX001):
            out(Bool("C1"))

    codes = _strict_codes(logic, TagMap({}, include_system=False))

    assert CLK_ADDRESS_IDENTITY_CONFLICT in codes


def test_repeated_use_of_one_raw_identity_does_not_conflict():
    RawX1 = Bool("X1")

    with Program() as logic:
        with Rung(RawX1):
            out(Bool("C1"))
        with Rung(RawX1):
            out(Bool("C2"))

    codes = _strict_codes(logic, TagMap({}, include_system=False))

    assert CLK_ADDRESS_IDENTITY_CONFLICT not in codes
