"""Regression coverage for the passes that build how()'s static facts."""

from __future__ import annotations

import pytest

from pyrung.core import (
    Block,
    Bool,
    Int,
    Program,
    TagType,
    call,
    copy,
    fill,
    math,
    out,
    return_early,
    rung,
    subroutine,
)
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.prove import (
    Intractable,
    _build_prior_facts,
)
from pyrung.core.analysis.prove.elision import (
    _elide_scan_local_stateful_dims,
)
from pyrung.core.analysis.prove.passes import (
    _pass_build_graph,
    _pass_classify_dimensions,
    _PassContext,
)


def _discovery_program() -> Program:
    step = Int("Step", external=True, choices={0: "Idle", 1: "Fill", 2: "Dump"})
    threshold_in = Int("ThresholdIn", external=True, choices={0: "Low", 1: "High"})
    stored_step = Int("StoredStep")
    stored_threshold = Int("StoredThreshold")
    dump_mode = Bool("DumpMode")

    with Program(strict=False) as logic:
        with rung():
            copy(step, stored_step)
        with rung():
            copy(threshold_in, stored_threshold)
        with rung(stored_step > stored_threshold):
            out(dump_mode)

    return logic


class TestPassReuse:
    def test_classification_threads_atom_index_through_absorption_helpers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctx = _PassContext(program=_discovery_program())
        _pass_build_graph(ctx)

        def fail(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("absorption helpers should reuse the classification atom index")

        monkeypatch.setattr("pyrung.core.analysis.prove.absorb._build_atom_index", fail)

        _pass_classify_dimensions(ctx)

        assert ctx.stateful_dims is not None


class TestIndividualPasses:
    def test_pass_build_graph_populates_context(self) -> None:
        sensor = Bool("Sensor", external=True)
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with rung(sensor):
                out(alarm)

        ctx = _PassContext(program=logic)
        _pass_build_graph(ctx)

        assert ctx.graph is not None
        assert ctx.all_exprs is not None
        assert ctx.all_exprs


def _elide_stateful_dims(
    program: Program,
    stateful_dims: dict[str, tuple[object, ...]],
    nondeterministic_dims: dict[str, tuple[object, ...]],
) -> dict[str, tuple[object, ...]]:
    reduced, _ = _elide_scan_local_stateful_dims(
        program,
        build_program_graph(program),
        stateful_dims,
        nondeterministic_dims,
    )
    return reduced


class TestScanLocalStateElision:
    def test_elides_indirect_pointer_scratch_from_init_backed_table(self) -> None:
        init_done = Bool("InitDone")
        selector = Int("Selector", external=True, choices={1: "A", 2: "B"})
        idx = Int("Idx")
        tmp = Int("Tmp")
        table = Block("Table", TagType.INT, 1, 2)

        with Program(strict=False) as logic:
            with rung(~init_done):
                copy(10, table[1])
                copy(20, table[2])
                copy(1, init_done)
            with rung():
                math(selector, idx)
            with rung():
                copy(table[idx], tmp)

        reduced = _elide_stateful_dims(
            logic,
            {"Idx": (1, 2), "Tmp": (10, 20)},
            {"Selector": (1, 2)},
        )

        assert reduced == {}

    def test_elides_reset_before_self_read_counter(self) -> None:
        idx = Int("Idx")
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with rung():
                copy(0, idx)
            with rung():
                math(idx + 1, idx)
            with rung(idx == 1):
                out(flag)

        reduced = _elide_stateful_dims(logic, {"Idx": (0, 1, 2)}, {})

        assert reduced == {}

    @pytest.mark.xfail(reason="sliced elision cannot prove return_early pulse patterns scan-local")
    def test_elides_canonical_return_early_pulse_flag(self) -> None:
        req = Bool("Req", external=True)
        pulse = Int("Pulse", choices={0: "No", 1: "Yes"})

        @subroutine("worker", strict=False)
        def worker():
            with rung(req):
                copy(1, pulse)
            with rung(pulse == 1):
                copy(0, pulse)
                return_early()
            with rung():
                copy(0, pulse)

        with Program(strict=False) as logic:
            with rung():
                call(worker)

        reduced = _elide_stateful_dims(logic, {"Pulse": (0, 1)}, {"Req": (False, True)})

        assert reduced == {}

    @pytest.mark.xfail(
        reason="sliced elision cannot prove multi-writer pulse+reset patterns scan-local"
    )
    def test_elides_canonical_branch_reset_flag(self) -> None:
        req_a = Bool("ReqA", external=True)
        req_b = Bool("ReqB", external=True)
        pulse = Int("Pulse", choices={0: "No", 1: "Yes"})
        seen = Int("Seen", choices={0: "No", 1: "Yes"})

        with Program(strict=False) as logic:
            with rung():
                copy(0, seen)
            with rung(req_a):
                copy(1, pulse)
            with rung(req_b):
                copy(1, pulse)
            with rung(pulse == 1):
                copy(1, seen)
                copy(0, pulse)

        reduced = _elide_stateful_dims(
            logic,
            {"Pulse": (0, 1), "Seen": (0, 1)},
            {"ReqA": (False, True), "ReqB": (False, True)},
        )

        assert reduced == {}

    def test_elides_indirect_scratch_with_unmapped_default_slot(self) -> None:
        selector = Int("Selector", external=True, choices={10: "None", 11: "Go"})
        tmp = Int("Tmp", choices={0: "None", 7: "Go"})
        outv = Int("Out", choices={0: "None", 7: "Go"})
        table = Block("Table", TagType.INT, 10, 11)

        with Program(strict=False) as logic:
            with rung():
                copy(0, outv)
            with rung():
                copy(7, table[11])
            with rung():
                copy(table[selector], tmp)
            with rung(tmp != 0):
                copy(tmp, outv)
                copy(0, tmp)

        reduced = _elide_stateful_dims(
            logic,
            {"Tmp": (0, 7), "Out": (0, 7)},
            {"Selector": (10, 11)},
        )

        assert reduced == {}

    def test_keeps_prewrite_input_memory_when_next_scan_entry_matters(self) -> None:
        inp = Bool("Inp", external=True)
        tmp = Int("Tmp", choices={0: "No", 1: "Yes"})
        stored = Int("Stored", choices={0: "No", 1: "Yes"})

        with Program(strict=False) as logic:
            with rung(tmp == 1):
                copy(1, stored)
            with rung():
                copy(inp, tmp)

        reduced = _elide_stateful_dims(
            logic,
            {"Tmp": (0, 1), "Stored": (0, 1)},
            {"Inp": (False, True)},
        )

        assert reduced == {"Tmp": (0, 1), "Stored": (0, 1)}


class TestFunctionalDepAdvice:
    """Slice-elided or ordering-violating scratch is an advice-grade
    functional-dep projection that leaves the dims alone.  The jump-table
    pointer idiom is ``math(Req + 2, Scratch); copy(table[Scratch], Dest)``."""

    @staticmethod
    def _jump_pointer_program() -> Program:
        req = Int("Req")
        scratch = Int("Scratch")
        dest = Int("Dest")
        adv = Bool("Adv", external=True)
        table = Block("Table", TagType.INT, 1, 8)

        with Program() as logic:
            with rung(adv):
                copy(3, req)
            with rung():
                math(req + 2, scratch)
            with rung():
                copy(table[scratch], dest)
            with rung(dest != 0):
                copy(dest, req)  # rewrites Req AFTER Scratch's writer (loop shape)

        return logic

    def test_records_advice_without_touching_dims(self) -> None:
        context = _build_prior_facts(self._jump_pointer_program())
        assert not isinstance(context, Intractable)
        assert context.functional_dep_projections.get("Scratch") == ("Req", 1, 2)
        # Advice only: the scratch keeps its slice-elision classification
        # and never enters (or re-enters) the state dims.
        assert "Scratch" not in context.stateful_dims
        assert context.elided_tags.get("Scratch") != "functional_dep"


class TestFillOfANumberIntoText:
    """A Fill of a number into a TXT range writes text, so the facts never mix it with text."""

    @pytest.mark.parametrize("value", [5, 12, -5, 0, True, False])
    def test_check_and_the_how_prior_accept_it(self, value) -> None:
        chars = Block("Txt", TagType.CHAR, 1, 5)
        start, lit = Bool("Start"), Bool("Lit")
        with Program(strict=False) as logic:
            with rung(start):
                fill(value, chars.select(1, 3))
            with rung(chars[1] == chars[5]):
                out(lit)

        logic.check()
        facts = _build_prior_facts(logic)
        if not isinstance(facts, Intractable):
            for name in ("Txt1", "Txt2", "Txt3"):
                values = facts.stateful_dims.get(name, ())
                assert all(isinstance(value, str) for value in values), (name, values)
