"""The DAP leaves out the tags pyrung adds itself, and only those."""

from __future__ import annotations

import pytest

from pyrung.core import PLC, Bool, Int, Program, Rung, copy
from pyrung.core.harness import Harness
from pyrung.core.physical import Physical
from pyrung.dap.internal_tags import is_internal_tag_name, visible_tag_names, visible_tags

#: Every prefix pyrung generates (synthesis.py, harness.py).
GENERATED = [
    "__on1__Light",
    "__off12__Light",
    "__set1__Speed",
    "__cpl_ond__Fb",
    "__cpl_on__Fb",
    "__cpl_on_Ts__Fb",
    "__cpl_off_Td__Fb",
    "__pls_on_Tm__Fb",
    "__pls_off_Th__Fb",
    "__cpl_off__Fb",
    "__cpl_armed__Fb",
    "__pls_ond__Fb",
    "__pls_on__Fb",
    "__pls_offd__Fb",
    "__pls_off__Fb",
]

#: User tags that only look like them.
USER = [
    "Light",
    "__Mine",
    "__a__b",
    "__spare__1",
    "__init__",
    "__on__Light",
    "__onA__Light",
    "__set__Speed",
    "__frz0__Light",
    "__cpl_x__Fb",
    "__pls_offx__Fb",
    "__cpl_on__",
    "x__on1__Light",
]


@pytest.mark.parametrize("name", GENERATED)
def test_generated_names_are_internal(name: str):
    assert is_internal_tag_name(name)


@pytest.mark.parametrize("name", USER)
def test_user_names_are_not_internal(name: str):
    assert not is_internal_tag_name(name)


def test_visible_helpers_keep_order_and_values():
    names = ["B", "__on1__B", "__init__", "__cpl_on__Fb", "A"]
    assert visible_tag_names(names) == ["B", "__init__", "A"]
    assert visible_tags({name: index for index, name in enumerate(names)}) == {
        "B": 0,
        "__init__": 2,
        "A": 4,
    }


def test_matches_what_a_harness_coupling_adds():
    # Ground the prefixes in the real generator, not just the list above.
    enable = Bool("Enable", external=True)
    fb = Bool("Fb", physical=Physical("Switch", on_delay="200ms", off_delay="100ms"), link="Enable")
    stage = Int("Stage")
    with Program() as prog:
        with Rung(enable, fb):
            copy(1, stage)
    plc = PLC(prog, dt=0.1)
    Harness(plc).install()
    plc.patch({"Enable": True})
    plc.run(cycles=3)
    tags = plc.current_state.tags
    added = [name for name in tags if name.startswith("__")]
    assert added
    assert all(is_internal_tag_name(name) for name in added), added
    assert visible_tag_names(tags) == [name for name in tags if not name.startswith("__")]
