"""randint(lo, hi): a per-scan draw on a hidden input tag, reproduced by replay."""

from __future__ import annotations

import pytest

from pyrung import Bool, Counter, Int, Program, Rung, Timer, calc, count_up, latch, on_delay
from pyrung.core import forloop, randint
from pyrung.core.analysis.prove import Counterexample, Intractable, Proven, always
from pyrung.core.compiled_plc import CompiledPLC
from pyrung.core.expression import RandIntExpr, format_expr, randint_draw
from pyrung.core.runner import PLC
from pyrung.core.tag import Tag


def _die_program() -> tuple[Program, RandIntExpr, Tag]:
    die = Int("Die")
    draw = randint(1, 6)
    with Program(strict=False) as logic:
        with Rung():
            calc(draw, die)
    return logic, draw, die


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
        plc.patch({lo: 50, hi: 50})
        plc.step()
        assert plc.state.tags["Result"] == 50

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
        stepped.run_for(10.0, fold=False)
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
