"""Timer, counter and Out-family presets take an int or an INT, DINT or WORD tag.

A REAL preset compared differently per backend (``acc >= 2.5`` interpreted,
``acc >= int(2.5)`` compiled), and CLICK refuses one, so it is rejected when the
instruction is built.
"""

import pytest

from pyrung.core import (
    Bool,
    Char,
    Counter,
    Dint,
    Int,
    Program,
    Real,
    Rung,
    Timer,
    Word,
    count_down,
    count_up,
    debounce,
    flasher,
    off_delay,
    on_delay,
    timed,
)

Run = Bool("PresetRun")
Rst = Bool("PresetRst")
Target = Bool("PresetTarget")
T = Timer.clone("PresetT")
T2 = Timer.clone("PresetT2")
C = Counter.clone("PresetC")


def _build(preset, kind):
    with Program(strict=False):
        with Rung(Run):
            if kind == "on_delay":
                on_delay(T, preset=preset)
            elif kind == "off_delay":
                off_delay(T, preset=preset)
            elif kind == "count_up":
                count_up(C, preset=preset).reset(Rst)
            elif kind == "count_down":
                count_down(C, preset=preset).reset(Rst)
            elif kind == "debounce":
                debounce(Target, on=(T, preset), off=(T2, 10))
            elif kind == "flasher":
                flasher(Target, on=(T, 10), off=(T2, preset))
            elif kind == "timed":
                timed(Target, T, preset=preset)


KINDS = ("on_delay", "off_delay", "count_up", "count_down", "debounce", "flasher", "timed")


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("preset", [Int("PresetInt"), Dint("PresetDint"), Word("PresetWord"), 5])
def test_integer_presets_accepted(kind, preset):
    _build(preset, kind)


@pytest.mark.parametrize("kind", KINDS)
def test_real_preset_tag_rejected(kind):
    with pytest.raises(TypeError, match=r"preset must be INT, DINT, or WORD; got REAL at PresetSp"):
        _build(Real("PresetSp", default=2.5), kind)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("preset", [Bool("PresetBool"), Char("PresetChar")])
def test_non_numeric_preset_tag_rejected(kind, preset):
    with pytest.raises(TypeError, match=r"must be INT, DINT, or WORD"):
        _build(preset, kind)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("preset", [2.5, True])
def test_float_and_bool_literal_presets_rejected(kind, preset):
    with pytest.raises(TypeError, match=r"must be an int or an INT, DINT, or WORD tag"):
        _build(preset, kind)


def test_direct_construction_rejects_real_preset():
    from pyrung.core.instruction import OnDelayInstruction

    with pytest.raises(
        TypeError, match=r"on_delay\(\) preset must be INT, DINT, or WORD; got REAL"
    ):
        OnDelayInstruction(T.Done, T.Acc, Real("PresetSp"), Run)
