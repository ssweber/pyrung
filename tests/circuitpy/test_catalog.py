"""Tests for the P1AM module catalog."""

import pytest

from pyrung.circuitpy.catalog import (
    MODULE_CATALOG,
    ChannelGroup,
    ModuleDirection,
)
from pyrung.core.tag import TagType

# ---------------------------------------------------------------------------
# Catalog completeness
# ---------------------------------------------------------------------------

EXPECTED_DISCRETE_INPUT = {
    "P1-08ND-TTL",
    "P1-08ND3",
    "P1-08NA",
    "P1-08SIM",
    "P1-08NE3",
    "P1-16ND3",
    "P1-16NE3",
}

EXPECTED_DISCRETE_OUTPUT = {
    "P1-04TRS",
    "P1-08TA",
    "P1-08TRS",
    "P1-16TR",
    "P1-08TD-TTL",
    "P1-08TD1",
    "P1-08TD2",
    "P1-15TD1",
    "P1-15TD2",
}

EXPECTED_COMBO_DISCRETE = {
    "P1-16CDR",
    "P1-15CDD1",
    "P1-15CDD2",
}

EXPECTED_ANALOG_INPUT = {
    "P1-04AD",
    "P1-04AD-1",
    "P1-04AD-2",
    "P1-04RTD",
    "P1-04THM",
    "P1-04NTC",
    "P1-04ADL-1",
    "P1-04ADL-2",
    "P1-08ADL-1",
    "P1-08ADL-2",
}

EXPECTED_ANALOG_OUTPUT = {
    "P1-04DAL-1",
    "P1-04DAL-2",
    "P1-08DAL-1",
    "P1-08DAL-2",
}

EXPECTED_COMBO_ANALOG = {
    "P1-4ADL2DAL-1",
    "P1-4ADL2DAL-2",
}

ALL_EXPECTED = (
    EXPECTED_DISCRETE_INPUT
    | EXPECTED_DISCRETE_OUTPUT
    | EXPECTED_COMBO_DISCRETE
    | EXPECTED_ANALOG_INPUT
    | EXPECTED_ANALOG_OUTPUT
    | EXPECTED_COMBO_ANALOG
)


def test_catalog_contains_all_expected_modules():
    assert ALL_EXPECTED == set(MODULE_CATALOG.keys())


def test_catalog_has_no_duplicate_keys():
    # dict keys are unique by construction, but verify count matches
    assert len(MODULE_CATALOG) == len(ALL_EXPECTED)


# ---------------------------------------------------------------------------
# ModuleSpec properties
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("part", sorted(EXPECTED_DISCRETE_INPUT))
def test_discrete_input_modules(part: str):
    spec = MODULE_CATALOG[part]
    assert spec.part_number == part
    assert spec.direction is ModuleDirection.INPUT
    assert not spec.is_combo
    assert len(spec.groups) == 1
    assert spec.groups[0].direction is ModuleDirection.INPUT
    assert spec.groups[0].tag_type is TagType.BOOL
    assert spec.groups[0].count > 0
    assert spec.input_group is not None
    assert spec.output_group is None


@pytest.mark.parametrize("part", sorted(EXPECTED_DISCRETE_OUTPUT))
def test_discrete_output_modules(part: str):
    spec = MODULE_CATALOG[part]
    assert spec.direction is ModuleDirection.OUTPUT
    assert not spec.is_combo
    assert spec.groups[0].direction is ModuleDirection.OUTPUT
    assert spec.groups[0].tag_type is TagType.BOOL
    assert spec.input_group is None
    assert spec.output_group is not None


@pytest.mark.parametrize("part", sorted(EXPECTED_COMBO_DISCRETE))
def test_combo_discrete_modules(part: str):
    spec = MODULE_CATALOG[part]
    assert spec.direction is ModuleDirection.COMBO
    assert spec.is_combo
    assert len(spec.groups) == 2
    assert spec.input_group is not None
    assert spec.output_group is not None
    assert spec.input_group.tag_type is TagType.BOOL
    assert spec.output_group.tag_type is TagType.BOOL


@pytest.mark.parametrize("part", sorted(EXPECTED_ANALOG_INPUT))
def test_analog_input_modules(part: str):
    spec = MODULE_CATALOG[part]
    assert spec.direction is ModuleDirection.INPUT
    assert not spec.is_combo
    assert spec.groups[0].tag_type is TagType.INT


@pytest.mark.parametrize("part", sorted(EXPECTED_ANALOG_OUTPUT))
def test_analog_output_modules(part: str):
    spec = MODULE_CATALOG[part]
    assert spec.direction is ModuleDirection.OUTPUT
    assert not spec.is_combo
    assert spec.groups[0].tag_type is TagType.INT


@pytest.mark.parametrize("part", sorted(EXPECTED_COMBO_ANALOG))
def test_combo_analog_modules(part: str):
    spec = MODULE_CATALOG[part]
    assert spec.direction is ModuleDirection.COMBO
    assert spec.is_combo
    assert spec.input_group is not None
    assert spec.output_group is not None
    assert spec.input_group.tag_type is TagType.INT
    assert spec.output_group.tag_type is TagType.INT


# ---------------------------------------------------------------------------
# Channel counts
# ---------------------------------------------------------------------------


def test_8ch_discrete_input_channel_count():
    for part in ("P1-08SIM", "P1-08ND3", "P1-08NA", "P1-08NE3", "P1-08ND-TTL"):
        assert MODULE_CATALOG[part].groups[0].count == 8


def test_16ch_discrete_input_channel_count():
    for part in ("P1-16ND3", "P1-16NE3"):
        assert MODULE_CATALOG[part].groups[0].count == 16


def test_4ch_relay_output():
    assert MODULE_CATALOG["P1-04TRS"].groups[0].count == 4


def test_15ch_output_modules():
    for part in ("P1-15TD1", "P1-15TD2"):
        assert MODULE_CATALOG[part].groups[0].count == 15


def test_combo_p1_16cdr_channel_split():
    spec = MODULE_CATALOG["P1-16CDR"]
    assert spec.input_group is not None
    assert spec.output_group is not None
    assert spec.input_group.count == 8
    assert spec.output_group.count == 8


def test_combo_p1_15cdd_channel_split():
    for part in ("P1-15CDD1", "P1-15CDD2"):
        spec = MODULE_CATALOG[part]
        assert spec.input_group is not None
        assert spec.output_group is not None
        assert spec.input_group.count == 8
        assert spec.output_group.count == 7


def test_combo_analog_channel_split():
    for part in ("P1-4ADL2DAL-1", "P1-4ADL2DAL-2"):
        spec = MODULE_CATALOG[part]
        assert spec.input_group is not None
        assert spec.output_group is not None
        assert spec.input_group.count == 4
        assert spec.output_group.count == 2


# ---------------------------------------------------------------------------
# Data integrity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("part", sorted(MODULE_CATALOG.keys()))
def test_every_entry_has_positive_channel_counts(part: str):
    spec = MODULE_CATALOG[part]
    for group in spec.groups:
        assert group.count > 0, f"{part}: group has non-positive count"


@pytest.mark.parametrize("part", sorted(MODULE_CATALOG.keys()))
def test_every_entry_part_number_matches_key(part: str):
    assert MODULE_CATALOG[part].part_number == part


@pytest.mark.parametrize("part", sorted(MODULE_CATALOG.keys()))
def test_every_entry_has_description(part: str):
    assert MODULE_CATALOG[part].description


def test_module_spec_is_frozen():
    spec = MODULE_CATALOG["P1-08SIM"]
    with pytest.raises(AttributeError):
        spec.part_number = "changed"  # type: ignore[misc]


def test_channel_group_is_frozen():
    group = ChannelGroup(ModuleDirection.INPUT, 8, TagType.BOOL)
    with pytest.raises(AttributeError):
        group.count = 99  # type: ignore[misc]
