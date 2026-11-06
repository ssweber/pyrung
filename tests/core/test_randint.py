"""randint(lo, hi): a per-scan draw on a hidden input tag, reproduced by replay."""

from __future__ import annotations

import warnings

import pytest

from pyrung import (
    Block,
    Bool,
    Char,
    Counter,
    Dint,
    Int,
    Program,
    Real,
    Rung,
    TagType,
    Timer,
    Word,
    calc,
    call,
    copy,
    count_up,
    latch,
    on_delay,
    out,
    subroutine,
)
from pyrung.core import forloop, randint
from pyrung.core.analysis.prove import Counterexample, Intractable, Proven, always
from pyrung.core.compiled_plc import CompiledPLC
from pyrung.core.expression import RandIntExpr, format_expr, randint_draw, randint_sites, sqrt
from pyrung.core.runner import PLC
from pyrung.core.system_points import system
from pyrung.core.tag import Tag


def _die_program() -> tuple[Program, RandIntExpr, Tag]:
    die = Int("Die")
    draw = randint(1, 6)
    with Program(strict=False) as logic:
        with Rung():
            calc(draw, die)
    return logic, draw, die


def _int_and_word() -> tuple[Program, RandIntExpr, list[str]]:
    roll = randint(100000, 110000)
    with Program(strict=False) as logic:
        with Rung():
            calc(roll, Int("I"))
        with Rung():
            calc(roll, Word("W"))
    name = roll.tag.name
    return logic, roll, [name, f"{name}.cut", f"{name}.ucut", "I", "W"]


def _tag_read_by_a_rung(
    *, with_calc: bool, as_condition: bool
) -> tuple[Program, RandIntExpr, list[str]]:
    roll = randint(70000, 90000)
    with Program(strict=False) as logic:
        if with_calc:
            with Rung():
                calc(roll, Int("Die"))
        if as_condition:
            with Rung(roll.tag > 80000):
                copy(1, Int("High"))
            with Rung(roll.tag <= 80000):
                copy(0, Int("High"))
        else:
            with Rung():
                copy(roll.tag, Dint("Shown"))
    name = roll.tag.name
    return logic, roll, [name, f"{name}.cut", "Die", "Shown", "High"]


def _tag_read_in_a_subroutine() -> tuple[Program, RandIntExpr, list[str]]:
    roll = randint(70000, 90000)
    with Program(strict=False) as logic:
        with Rung():
            calc(roll, Int("Die"))
        with Rung():
            call("show")
        with subroutine("show"):
            with Rung():
                copy(roll.tag, Dint("Shown"))
    name = roll.tag.name
    return logic, roll, [name, f"{name}.cut", "Die", "Shown"]


#: Programs whose draws stepping once took otherwise than the kernel (review of the uncut draw).
_KERNEL_SITE_PROGRAMS = {
    "int_and_hex_word": _int_and_word,
    "calc_and_tag_copy": lambda: _tag_read_by_a_rung(with_calc=True, as_condition=False),
    "tag_copy_alone": lambda: _tag_read_by_a_rung(with_calc=False, as_condition=False),
    "tag_in_a_condition": lambda: _tag_read_by_a_rung(with_calc=False, as_condition=True),
    "tag_in_a_subroutine": _tag_read_in_a_subroutine,
}


def _draws(runner, tag: str, scans: int) -> list[int]:
    values = []
    for _ in range(scans):
        state = runner.step()
        state = state if state is not None else runner.state
        values.append(state.tags[tag])
    return values


class TestDraws:
    def test_inclusive_range_and_variation(self):
        logic, _draw, _die = _die_program()
        values = _draws(PLC(logic, dt=0.01), "Die", 300)
        assert set(values) == {1, 2, 3, 4, 5, 6}

    def test_draw_is_a_pure_function_of_site_and_scan(self):
        assert randint_draw(3, 17, 1, 6) == randint_draw(3, 17, 1, 6)
        assert randint_draw(3, 17, 6, 1) == randint_draw(3, 17, 1, 6)
        assert randint_draw(3, 17, 4, 4) == 4

    def test_two_runs_of_one_program_agree(self):
        logic, _draw, _die = _die_program()
        assert _draws(PLC(logic, dt=0.01), "Die", 50) == _draws(PLC(logic, dt=0.01), "Die", 50)

    def test_two_sites_draw_independently(self):
        a, b = Int("A"), Int("B")
        with Program(strict=False) as logic:
            with Rung():
                calc(randint(1, 1000), a)
                calc(randint(1, 1000), b)
        plc = PLC(logic, dt=0.01)
        pairs = [(s.tags["A"], s.tags["B"]) for s in (plc.step() or plc.state for _ in range(20))]
        assert any(x != y for x, y in pairs)

    def test_tag_bounds_are_read_each_scan(self):
        lo, hi, result = Int("Lo", default=10), Int("Hi", default=12), Int("Result")
        with Program(strict=False) as logic:
            with Rung():
                calc(randint(lo, hi), result)
        plc = PLC(logic, dt=0.01)
        assert set(_draws(plc, "Result", 100)) == {10, 11, 12}
        plc.patch({lo: 50, hi: 51})
        plc.step()
        assert plc.state.tags["Result"] in (50, 51)

    def test_reversed_bounds_draw_between_them(self):
        result = Int("Result")
        with Program(strict=False) as logic:
            with Rung():
                calc(randint(6, 1), result)
        assert set(_draws(PLC(logic, dt=0.01), "Result", 300)) == {1, 2, 3, 4, 5, 6}

    def test_compiled_backend_draws_the_same(self):
        logic, _draw, _die = _die_program()
        interpreted = _draws(PLC(logic, dt=0.01), "Die", 50)
        compiled = _draws(CompiledPLC(logic, dt=0.01), "Die", 50)
        assert compiled == interpreted

    def test_format_and_repr(self):
        lo = Int("Lo")
        assert format_expr(randint(1, 6)) == "randint(1, 6)"
        assert repr(randint(lo, 6)) == "randint(Lo, 6)"


class TestOverride:
    def test_patch_overrides_one_scan(self):
        logic, draw, _die = _die_program()
        plc = PLC(logic, dt=0.01)
        natural = _draws(PLC(logic, dt=0.01), "Die", 3)
        plc.patch({draw.tag: 4})
        plc.step()
        assert plc.state.tags["Die"] == 4
        plc.step()
        plc.step()
        assert plc.state.tags["Die"] == natural[2]

    def test_force_pins_every_scan(self):
        logic, draw, _die = _die_program()
        plc = PLC(logic, dt=0.01)
        plc.force(draw.tag, 5)
        assert set(_draws(plc, "Die", 20)) == {5}

    def test_compiled_patch_overrides(self):
        logic, draw, _die = _die_program()
        plc = CompiledPLC(logic, dt=0.01)
        plc.patch({draw.tag: 2})
        assert plc.step().tags["Die"] == 2


class TestBoundsAsCLICKReadsThem:
    @pytest.mark.parametrize("factory", [PLC, CompiledPLC])
    def test_equal_constants_are_a_math_error(self, factory):
        die = Int("Die", default=7)
        with Program(strict=False) as logic:
            with Rung():
                calc(randint(4, 4), die)
        state = factory(logic, dt=0.01).step()
        assert state.tags["Die"] == 0
        assert state.tags["fault.math_operation_error"] is True
        assert state.tags["fault.out_of_range"] is False

    def test_bounds_that_can_be_equal_declare_the_math_error(self):
        lo = Int("Lo", default=4)
        with Program(strict=False) as logic:
            with Rung():
                calc(randint(4, 4), Int("A"))
                calc(randint(4, 5), Int("B"))
                calc(randint(lo, lo), Int("C"))
        faults = [instr._fault_writes for instr in logic.rungs[0]._instructions]
        assert ["fault.math_operation_error" in writes for writes in faults] == [
            True,
            False,
            True,
        ]

    @pytest.mark.parametrize("factory", [PLC, CompiledPLC])
    def test_equal_tag_bounds_are_a_math_error(self, factory):
        """Two tags holding one value: 0 with SC46, as equal constants (CLICK, 2026-11-05)."""
        lo, hi = Dint("Lo", default=7), Dint("Hi", default=7)
        die = Int("Die", default=9)
        with Program(strict=False) as logic:
            with Rung():
                calc(randint(lo, hi), die)
        state = factory(logic, dt=0.01).step()
        assert state.tags["Die"] == 0
        assert state.tags["fault.math_operation_error"] is True
        assert state.tags["fault.out_of_range"] is False

    @pytest.mark.parametrize("factory", [PLC, CompiledPLC])
    @pytest.mark.parametrize("tags", [False, True], ids=["constants", "tags"])
    def test_a_math_error_zeroes_only_the_randint(self, factory, tags):
        """randint(7, 7) + 5 is 5 with SC46: the rest of the formula runs."""
        lo, hi = Dint("Lo", default=7), Dint("Hi", default=7)
        die = Int("Die", default=9)
        with Program(strict=False) as logic:
            with Rung():
                calc((randint(lo, hi) if tags else randint(7, 7)) + 5, die)
        state = factory(logic, dt=0.01).step()
        assert state.tags["Die"] == 5
        assert state.tags["fault.math_operation_error"] is True

    @pytest.mark.parametrize("factory", [PLC, CompiledPLC])
    def test_real_tag_bounds_in_a_formula_read_zero_without_a_flag(self, factory):
        r = Real("R", default=3.0)
        die = Int("Die", default=9)
        with Program(strict=False) as logic:
            with Rung():
                calc(randint(r, r) + 5, die)
        state = factory(logic, dt=0.01).step()
        assert state.tags["Die"] == 5
        assert state.tags["fault.math_operation_error"] is False

    @pytest.mark.parametrize(
        "bounds,lowest,highest",
        [
            ((70000, 70002), 4464, 4466),
            ((70002, 70000), 4464, 4466),
        ],
    )
    def test_constants_cut_into_an_int(self, bounds, lowest, highest):
        die = Int("Die")
        with Program(strict=False) as logic:
            with Rung():
                calc(randint(*bounds), die)
        values = _draws(PLC(logic, dt=0.01), "Die", 2000)
        assert (min(values), max(values)) == (lowest, highest)

    def test_bounds_each_cut_on_their_own(self):
        """A pair that crosses 65536 is cut bound by bound, not as a range."""
        roll = randint(65000, 66000)
        die = Int("Die")
        with Program(strict=False) as logic:
            with Rung():
                calc(roll, die)
        assert (roll.cut_tag.min, roll.cut_tag.max) == (-536, 464)
        values = _draws(PLC(logic, dt=0.01), "Die", 2000)
        assert -536 <= min(values) < -500
        assert 400 < max(values) <= 464

    def test_bounds_that_cut_to_one_value_are_a_math_error(self):
        die = Int("Die", default=7)
        with Program(strict=False) as logic:
            with Rung():
                calc(randint(70000, 4464), die)
        state = PLC(logic, dt=0.01).step()
        assert state.tags["Die"] == 0
        assert state.tags["fault.math_operation_error"] is True

    def test_a_tag_bound_keeps_its_width(self):
        hi = Dint("Hi", default=70000)
        die = Dint("Die")
        with Program(strict=False) as logic:
            with Rung():
                calc(randint(hi, 70001), die)
        state = PLC(logic, dt=0.01).step()
        assert state.tags["Die"] in (70000, 70001)
        assert state.tags["fault.math_operation_error"] is False

    @pytest.mark.parametrize("factory", [PLC, CompiledPLC])
    def test_a_cut_site_still_answers_to_its_tag(self, factory):
        roll = randint(70000, 70002)
        die = Int("Die")
        with Program(strict=False) as logic:
            with Rung():
                calc(roll, die)
        plc = factory(logic, dt=0.01)
        plc.patch({roll.cut_tag: 4465})
        assert plc.step().tags["Die"] == 4465

    def test_a_cut_site_draws_inside_its_declared_range(self):
        roll = randint(70000, 70002)
        die = Int("Die")
        with Program(strict=False) as logic:
            with Rung():
                calc(roll, die)
        assert (roll.cut_tag.min, roll.cut_tag.max) == (4464, 4466)
        assert (roll.tag.min, roll.tag.max) == (70000, 70002)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            _draws(PLC(logic, dt=0.01), "Die", 20)

    def test_a_cut_site_exports_as_written(self):
        roll = randint(70000, 70002)
        with Program(strict=False) as logic:
            with Rung():
                calc(roll / 2, Int("Die"))
        instr = logic.rungs[0]._instructions[0]
        assert format_expr(instr.source_expression.left) == "randint(70000, 70002)"
        assert format_expr(instr.expression.left) == "randint(4464, 4466)"

    @pytest.mark.parametrize("factory", [PLC, CompiledPLC])
    @pytest.mark.parametrize("narrow_first", [True, False], ids=["narrow_first", "wide_first"])
    def test_one_expression_has_one_draw_per_width(self, factory, narrow_first):
        roll = randint(70000, 70002)
        narrow, wide = Int("Narrow"), Dint("Wide")
        with Program(strict=False) as logic:
            with Rung():
                for dest in (narrow, wide) if narrow_first else (wide, narrow):
                    calc(roll, dest)
        plc = factory(logic, dt=0.01)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            for _ in range(30):
                state = plc.step()
                assert 4464 <= state.tags["Narrow"] <= 4466
                assert 70000 <= state.tags["Wide"] <= 70002

    def test_two_calcs_that_cut_read_the_one_cut_draw(self):
        roll = randint(70000, 70002)
        first, second = Int("First"), Int("Second")
        with Program(strict=False) as logic:
            with Rung():
                calc(roll, first)
                calc(roll, second)
        plc = PLC(logic, dt=0.01)
        for _ in range(30):
            state = plc.step()
            assert state.tags["First"] == state.tags["Second"]

    def test_a_later_program_cannot_reach_an_earlier_ones_draws(self):
        roll = randint(70000, 70002)
        wide = Dint("Wide")
        with Program(strict=False) as first:
            with Rung():
                calc(roll, wide)
        running = PLC(first, dt=0.01)
        before = _draws(PLC(first, dt=0.01), "Wide", 20)

        with Program(strict=False) as second:
            with Rung():
                calc(roll, Int("Narrow"))

        assert (roll.tag.min, roll.tag.max) == (70000, 70002)
        assert _draws(running, "Wide", 20) == before
        narrow = _draws(PLC(second, dt=0.01), "Narrow", 20)
        assert all(4464 <= value <= 4466 for value in narrow)

    def test_an_earlier_program_that_cuts_keeps_its_draws(self):
        roll = randint(70000, 70002)
        with Program(strict=False) as first:
            with Rung():
                calc(roll, Int("Narrow"))
        before = _draws(PLC(first, dt=0.01), "Narrow", 20)

        with Program(strict=False) as second:
            with Rung():
                calc(roll, Dint("Wide"))

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert _draws(PLC(first, dt=0.01), "Narrow", 20) == before
            wide = _draws(PLC(second, dt=0.01), "Wide", 20)
        assert all(70000 <= value <= 70002 for value in wide)

    def test_both_widths_keep_what_each_calc_exports(self):
        roll = randint(70000, 70002)
        with Program(strict=False) as logic:
            with Rung():
                calc(roll, Int("Narrow"))
                calc(roll, Dint("Wide"))
        narrow, wide = logic.rungs[0]._instructions
        assert format_expr(narrow.source_expression) == "randint(70000, 70002)"
        assert format_expr(narrow.expression) == "randint(4464, 4466)"
        assert format_expr(wide.expression) == "randint(70000, 70002)"

    def test_a_site_with_nothing_to_cut_has_the_one_tag(self):
        roll = randint(70000, 70002)
        assert roll.cut_tag.name != roll.tag.name
        small = randint(1, 6)
        assert small.cut_tag is small.tag

    @pytest.mark.parametrize("factory", [PLC, CompiledPLC])
    @pytest.mark.parametrize(
        "dests, drawn",
        [
            pytest.param((Int,), ("cut",), id="int"),
            pytest.param((Dint,), ("uncut",), id="dint"),
            pytest.param((Int, Dint), ("cut", "uncut"), id="both"),
        ],
    )
    def test_only_the_draws_a_math_runs_are_drawn(self, factory, dests, drawn):
        """One draw per RAND a Math runs, as on a CLICK: each its own site's, as if alone."""
        roll = randint(70000, 90000)
        with Program(strict=False) as logic:
            with Rung():
                for index, dest in enumerate(dests):
                    calc(roll, dest(f"Die{index}"))
        sites = {
            "uncut": (roll.tag.name, roll.site, 70000, 90000),
            "cut": (f"{roll.tag.name}.cut", ~roll.site, 4464, 24464),
        }
        plc = factory(logic, dt=0.01)
        for _ in range(20):
            state = plc.step()
            for which, (name, site, lo, hi) in sites.items():
                held = state.tags.get(name, roll.tag.default if which == "uncut" else lo)
                if which in drawn:
                    # Drawn as the scan runs, before its id moves on.
                    assert held == randint_draw(site, state.scan_id - 1, lo, hi)
                else:
                    assert held == (roll.tag.default if which == "uncut" else lo)

    @pytest.mark.parametrize("factory", [PLC, CompiledPLC])
    def test_a_signed_and_an_unsigned_cut_of_one_draw_both_draw(self, factory):
        """An Int's cut and a hex Word's share a site number, and each draws over its own cut."""
        roll = randint(100000, 110000)
        with Program(strict=False) as logic:
            with Rung():
                calc(roll, Int("I"))
            with Rung():
                calc(roll, Word("W"))
        assert {roll.cut_tag.name, roll.ucut_tag.name} == {
            f"{roll.tag.name}.cut",
            f"{roll.tag.name}.ucut",
        }
        plc = factory(logic, dt=0.01)
        for _ in range(20):
            state = plc.step()
            scan = state.scan_id - 1
            assert state.tags["I"] == randint_draw(~roll.site, scan, -31072, -21072)
            assert state.tags["W"] == randint_draw(~roll.site, scan, 34464, 44464)

    @pytest.mark.parametrize("program", sorted(_KERNEL_SITE_PROGRAMS))
    def test_stepping_draws_the_sites_the_kernel_draws(self, program):
        """Stepping draws by name the sites the kernel reaches, so a long run is stepping."""
        # *roll* is held as a script holds it: a site is known while its draw is alive, and a
        # rung reading ``roll.tag`` alone does not keep it so.
        logic, roll, watched = _KERNEL_SITE_PROGRAMS[program]()
        stepped = PLC(logic, dt=0.01)
        compiled = CompiledPLC(logic, dt=0.01)
        drawn = [site.tag.name for site in stepped._randint_site_list()]
        assert drawn == [site.tag.name for site in compiled._randint_sites]
        assert roll.tag.name in drawn or program == "int_and_hex_word"
        sites = randint_sites(watched)
        defaults = {site.tag.name: site.tag.default for site in sites}
        for _ in range(50):
            ours, theirs = stepped.step(), compiled.step()
            assert ours.scan_id == theirs.scan_id
            for name in watched:
                assert ours.tags.get(name, defaults.get(name)) == theirs.tags.get(
                    name, defaults.get(name)
                ), name
        long_run = PLC(logic, dt=0.01)
        long_run.run_for(30.0)
        by_step = PLC(logic, dt=0.01)
        for _ in range(long_run.state.scan_id):
            by_step.step()
        assert long_run.state.scan_id == by_step.state.scan_id
        assert long_run.state.tags == by_step.state.tags

    @pytest.mark.parametrize("factory", [PLC, CompiledPLC])
    def test_a_patch_or_force_of_a_draw_no_math_runs_moves_nothing(self, factory):
        """The uncut tag of an Int's cut draw holds what is put there, as a tag nothing writes."""
        roll = randint(70000, 90000)
        with Program(strict=False) as logic:
            with Rung():
                calc(roll, Int("Die"))
        natural = _draws(factory(logic, dt=0.01), "Die", 9)
        plc = factory(logic, dt=0.01)
        dice = []
        held = []
        plc.patch({roll.tag: 80000})
        for _ in range(3):
            state = plc.step()
            dice.append(state.tags["Die"])
            held.append(state.tags[roll.tag.name])
        plc.force(roll.tag, 85000)
        for _ in range(3):
            state = plc.step()
            dice.append(state.tags["Die"])
            held.append(state.tags[roll.tag.name])
        plc.unforce(roll.tag)
        for _ in range(3):
            state = plc.step()
            dice.append(state.tags["Die"])
            held.append(state.tags[roll.tag.name])
        assert dice == natural
        assert held == [80000] * 3 + [85000] * 6


class TestBoundsAsWritten:
    """Only a bare ``Real`` tag as a bound reads 0; every other bound still draws."""

    @pytest.mark.parametrize("factory", [PLC, CompiledPLC])
    def test_a_real_block_element_reads_zero(self, factory):
        df = Block("DF", TagType.REAL, 1, 5)
        die = Int("Die", default=7)
        with Program(strict=False) as logic:
            with Rung():
                calc(randint(0, df[2]) + 3, die)
        state = factory(logic, dt=0.01).step()
        assert state.tags["Die"] == 3
        assert state.tags["fault.out_of_range"] is False

    @pytest.mark.parametrize("factory", [PLC, CompiledPLC])
    @pytest.mark.parametrize(
        "make, low, high",
        [
            pytest.param(lambda n: randint(n / 2, n / 2 + 1), 8, 9, id="division"),
            pytest.param(lambda n: randint(n**2, n**2 + 1), 256, 257, id="power"),
            pytest.param(lambda n: randint(sqrt(n), sqrt(n) + 1), 4, 5, id="sqrt"),
        ],
    )
    def test_a_computed_bound_draws(self, factory, make, low, high):
        n = Int("N", default=16)
        die = Int("Die")
        with Program(strict=False) as logic:
            with Rung():
                calc(make(n), die)
        plc = factory(logic, dt=0.01)
        values = {plc.step().tags["Die"] for _ in range(40)}
        assert values == {low, high}

    @pytest.mark.parametrize("factory", [PLC, CompiledPLC])
    def test_a_real_inside_a_computed_bound_draws(self, factory):
        r = Real("R", default=3.5)
        die = Int("Die")
        with Program(strict=False) as logic:
            with Rung():
                calc(randint(r + 0, 5), die)
        plc = factory(logic, dt=0.01)
        assert {plc.step().tags["Die"] for _ in range(40)} == {3, 4, 5}

    @pytest.mark.parametrize("factory", [PLC, CompiledPLC])
    def test_a_float_literal_bound_draws(self, factory):
        die = Int("Die")
        with Program(strict=False) as logic:
            with Rung():
                calc(randint(2.5, 4.5), die)
        plc = factory(logic, dt=0.01)
        assert {plc.step().tags["Die"] for _ in range(40)} == {2, 3, 4}

    @pytest.mark.parametrize("factory", [PLC, CompiledPLC])
    def test_equal_float_literals_are_a_math_error(self, factory):
        """Each bound is read as an integer: 2 and 2 are equal, 0 with SC46."""
        die = Int("Die", default=7)
        with Program(strict=False) as logic:
            with Rung():
                calc(randint(2.5, 2.5), die)
        state = factory(logic, dt=0.01).step()
        assert state.tags["Die"] == 0
        assert state.tags["fault.math_operation_error"] is True

    @pytest.mark.parametrize("factory", [PLC, CompiledPLC])
    def test_a_char_bound_draws(self, factory):
        lo = Char("Lo", default="3")
        die = Int("Die")
        with Program(strict=False) as logic:
            with Rung():
                calc(randint(lo, 5), die)
        plc = factory(logic, dt=0.01)
        assert {plc.step().tags["Die"] for _ in range(40)} == {3, 4, 5}

    @pytest.mark.parametrize("factory", [PLC, CompiledPLC])
    @pytest.mark.parametrize(
        "make",
        [
            pytest.param(lambda: randint(7, 7), id="equal_constants"),
            pytest.param(lambda: randint(Real("Lo", default=1.0), 5), id="real_tag"),
        ],
    )
    def test_a_site_with_no_draw_ignores_patch_and_force(self, factory, make):
        roll = make()
        die = Int("Die", default=9)
        with Program(strict=False) as logic:
            with Rung():
                calc(roll, die)
        plc = factory(logic, dt=0.01)
        plc.patch({roll.tag: 6})
        assert plc.step().tags["Die"] == 0
        plc.force(roll.tag, 6)
        assert plc.step().tags["Die"] == 0


class TestReplay:
    def test_fork_draws_what_the_parent_draws(self):
        logic, _draw, _die = _die_program()
        plc = PLC(logic, dt=0.01)
        _draws(plc, "Die", 5)
        child = plc.fork()
        assert _draws(child, "Die", 10) == _draws(plc, "Die", 10)

    def test_history_rebuilds_draws_and_patches(self):
        logic, draw, _die = _die_program()
        plc = PLC(logic, dt=0.01, history=3)
        values = _draws(plc, "Die", 10)
        plc.patch({draw.tag: 6})
        values += _draws(plc, "Die", 10)
        for offset, value in enumerate(values, start=1):
            assert plc.history.at(offset).tags["Die"] == value

    def test_fold_steps_scan_by_scan(self):
        die, six = Int("Die"), Bool("Six")
        timer = Timer.clone("T")
        sixes = Counter.clone("C")
        with Program(strict=False) as logic:
            with Rung():
                on_delay(timer, 5000)
            with Rung():
                calc(randint(1, 6), die)
            with Rung(die == 6):
                count_up(sixes, 100_000).reset(six)

        folded = PLC(logic, dt=0.01)
        folded.run_for(10.0)
        stepped = PLC(logic, dt=0.01)
        stepped.run_for(10.0, jit=False)
        assert folded.state.tags == stepped.state.tags
        assert folded.state.scan_id == stepped.state.scan_id


class TestBuild:
    def test_randint_inside_forloop_is_rejected(self):
        result = Int("Result")
        with pytest.raises(ValueError, match="forloop"):
            with Program(strict=False):
                with Rung():
                    with forloop(3):
                        calc(randint(1, 6), result)

    def test_call_inside_forloop_does_not_see_a_randint_elsewhere(self):
        from pyrung.core import call, subroutine

        result = Int("Result")
        with Program(strict=False):
            with subroutine("Other"):
                with Rung():
                    calc(randint(1, 6), result)
            with subroutine("Work"):
                with Rung():
                    calc(result + 1, result)
            with Rung():
                with forloop(3):
                    call("Work")

    def test_randint_in_a_called_subroutine_inside_forloop_is_rejected(self):
        from pyrung.core import call, subroutine

        result = Int("Result")
        with pytest.raises(ValueError, match="forloop"):
            with Program(strict=False):
                with subroutine("Work"):
                    with Rung():
                        calc(randint(1, 6), result)
                with subroutine("Outer"):
                    with Rung():
                        call("Work")
                with Rung():
                    with forloop(3):
                        call("Outer")

    def test_hidden_tag_is_an_external_input_over_the_bounds(self):
        tag = randint(1, 6).tag
        assert tag.external
        assert (tag.min, tag.max, tag.default) == (1, 6, 1)
        bounded = randint(Int("Lo", min=2, max=4), 9).tag
        assert (bounded.min, bounded.max) == (2, 9)
        assert randint(Int("Lo"), 9).tag.min is None


class TestProver:
    def test_draw_reaches_every_value(self):
        die, six = Int("Die"), Bool("Six")
        with Program(strict=False) as logic:
            with Rung():
                calc(randint(1, 6), die)
            with Rung(die == 6):
                latch(six)
        verdict = always(logic, ~six)
        assert isinstance(verdict, Counterexample)
        plc = PLC(logic, dt=0.01)
        for step in verdict.trace:
            plc.patch(step.inputs)
            for _ in range(step.scans):
                plc.step()
        assert plc.state.tags["Six"] is True

    def test_draw_stays_in_range(self):
        die = Int("Die")
        with Program(strict=False) as logic:
            with Rung():
                calc(randint(1, 6), die)
        assert isinstance(always(logic, die <= 6), Proven)

    def test_a_cut_draw_stays_in_its_cut_range(self):
        die = Int("Die", default=4464)
        with Program(strict=False) as logic:
            with Rung():
                calc(randint(70000, 70002), die)
        assert isinstance(always(logic, die <= 4466), Proven)
        assert isinstance(always(logic, die >= 4464), Proven)

    def test_a_real_bound_reads_zero(self):
        die = Int("Die")
        lo = Real("Lo", min=1.0, max=3.0, default=1.0)
        with Program(strict=False) as logic:
            with Rung():
                calc(randint(lo, 5), die)
        assert isinstance(always(logic, die == 0), Proven)

    @pytest.mark.parametrize(
        "make, writes_it",
        [
            pytest.param(lambda n: randint(0, sqrt(n)), True, id="sqrt-bound"),
            pytest.param(lambda n: randint(sqrt(n), 5), True, id="sqrt-low-bound"),
            pytest.param(lambda n: randint(0, n**0.5), True, id="fractional-power-bound"),
            pytest.param(lambda n: randint(7, 7), True, id="equal-constants"),
            pytest.param(lambda n: randint(0, n), True, id="tag-bound"),
            pytest.param(lambda n: randint(0, n / 2), True, id="division-bound"),
            pytest.param(lambda n: randint(0, 5), False, id="constants"),
            pytest.param(lambda n: randint(Real("R"), sqrt(n)), False, id="real-tag-reads-0"),
        ],
    )
    def test_the_math_error_flag_is_written_where_a_bound_can_hit_one(self, make, writes_it):
        with Program(strict=False) as logic:
            with Rung():
                calc(make(Int("N")), Dint("D"))
        instr = logic.rungs[0]._instructions[0]
        assert ("fault.math_operation_error" in instr._fault_writes) is writes_it

    def test_a_domain_error_in_a_bound_is_no_proof_the_flag_stays_off(self):
        """N = -1 sets SC46, so ``always(~B)`` is false and must not be Proven."""
        n = Int("N", external=True, min=-1, max=1)
        d, b = Dint("D"), Bool("B")

        def build():
            with Program(strict=False) as logic:
                with Rung():
                    calc(randint(0, sqrt(n)), d)
                with Rung(system.fault.math_operation_error):
                    out(b)
            return logic

        plc = PLC(build())
        plc.patch({n: -1})
        plc.step()
        plc.step()
        assert plc.state.tags["B"] is True
        assert not isinstance(always(build(), ~b), Proven)

    def test_equal_constants_are_intractable_naming_the_math_error(self):
        die = Int("Die")
        with Program(strict=False) as logic:
            with Rung():
                calc(randint(7, 7), die)
        verdict = always(logic, die == 0)
        assert isinstance(verdict, Intractable)
        hint = verdict.hints[0]
        assert "randint(7, 7)" in hint
        assert "equal constants" in hint
        assert "tag bounds" not in hint

    def test_undeclared_tag_bound_is_intractable_naming_the_draw(self):
        die, six, lo = Int("Die"), Bool("Six"), Int("Lo")
        with Program(strict=False) as logic:
            with Rung():
                calc(randint(lo, 6), die)
            with Rung(die == 6):
                latch(six)
        verdict = always(logic, ~six)
        assert isinstance(verdict, Intractable)
        assert any("randint(Lo, 6)" in hint for hint in verdict.hints)

    def test_declared_tag_bound_gives_a_domain(self):
        die, six, lo = Int("Die"), Bool("Six"), Int("Lo", min=1, max=3)
        with Program(strict=False) as logic:
            with Rung():
                calc(randint(lo, 6), die)
            with Rung(die == 6):
                latch(six)
        assert isinstance(always(logic, ~six), Counterexample)
