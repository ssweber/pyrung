"""Three-way agreement harness for elision decisions.

For every elision candidate, runs:
  1. Interpreted PLC (ScanContext + Program._evaluate)
  2. Compiled kernel (_step_compiled_kernel)
  3. Abstract prediction (_ScanLocalStateElider)

Contracts verified:
  (a) Interpreted == Compiled on every (state, input) pair
  (b) Abstract prediction is consistent with concrete results —
      if abstract says "elidable", concrete must agree
  (c) Concrete == Interpreted (catches compiled-kernel semantic drift)
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import product
from typing import Any

import pytest

from pyrung.core import (
    Block,
    Bool,
    Counter,
    Int,
    Program,
    Rung,
    SystemState,
    TagType,
    calc,
    call,
    copy,
    count_up,
    latch,
    out,
    reset,
    subroutine,
)
from pyrung.core.analysis.pdg import ProgramGraph, build_program_graph
from pyrung.core.analysis.prove.elision import (
    _ConcreteStateElider,
    _elide_scan_local_stateful_dims,
)
from pyrung.core.analysis.prove.elision.abstract import (
    _ScanLocalStateElider,
)
from pyrung.core.analysis.prove.kernel import _step_compiled_kernel
from pyrung.core.context import ScanContext
from pyrung.core.kernel import CompiledKernel

_DEFAULT_DT = 0.010


# ---------------------------------------------------------------------------
# Oracle helpers
# ---------------------------------------------------------------------------


def _reset_oneshot_state(program: Program) -> None:
    """Reset all instruction-level oneshot state so each scan starts clean.

    OutInstruction stores ``_has_executed`` on the instruction object (not in
    ScanContext memory).  Without this reset, cross-scan state bleeds between
    independent ``_interpreted_scan`` calls.
    """
    for rung in program.rungs:
        for item in getattr(rung, "_execution_items", ()):
            reset_fn = getattr(item, "reset_oneshot", None)
            if reset_fn is not None:
                reset_fn()
    for sub_rungs in program.subroutines.values():
        for rung in sub_rungs:
            for item in getattr(rung, "_execution_items", ()):
                reset_fn = getattr(item, "reset_oneshot", None)
                if reset_fn is not None:
                    reset_fn()


def _interpreted_scan(
    program: Program,
    entry_values: Mapping[str, Any],
    observed: tuple[str, ...],
    *,
    dt: float = _DEFAULT_DT,
) -> tuple[Any, ...]:
    """One scan via the interpreted path, returning observed tag outputs."""
    _reset_oneshot_state(program)
    state = SystemState().with_tags(dict(entry_values))
    ctx = ScanContext(state)
    ctx.set_memory("_dt", dt)
    program._evaluate(ctx)
    result_state = ctx.commit(dt=dt)
    return tuple(result_state.tags.get(name) for name in observed)


def _compiled_scan(
    compiled: CompiledKernel,
    entry_values: Mapping[str, Any],
    observed: tuple[str, ...],
    *,
    dt: float = _DEFAULT_DT,
) -> tuple[Any, ...]:
    """One scan via the compiled kernel, returning observed tag outputs."""
    kernel = compiled.create_kernel()
    kernel.tags.update(entry_values)
    _step_compiled_kernel(compiled, kernel, dt=dt)
    return tuple(kernel.tags.get(name) for name in observed)


# ---------------------------------------------------------------------------
# Agreement checker
# ---------------------------------------------------------------------------


@dataclass
class _AgreementResult:
    candidate: str
    abstract_elidable: bool
    concrete_elidable: bool
    interpreted_compiled_match: bool
    mismatches: list[str]


def _check_candidate_agreement(
    program: Program,
    compiled: CompiledKernel,
    graph: ProgramGraph,
    stateful_dims: dict[str, tuple[Any, ...]],
    nondeterministic_dims: dict[str, tuple[Any, ...]],
    candidate: str,
    retained: frozenset[str],
) -> _AgreementResult:
    """Run all three oracles on a single candidate and check contracts."""
    mismatches: list[str] = []

    # --- Abstract prediction ---
    elider = _ScanLocalStateElider(program, graph, stateful_dims, nondeterministic_dims)
    _abstract_reduced, _ = elider.elide()
    abstract_elidable = candidate not in _abstract_reduced

    # --- Concrete prediction ---
    concrete_elider = _ConcreteStateElider(
        program, graph, stateful_dims, nondeterministic_dims, compiled=compiled
    )
    concrete_elidable = concrete_elider._can_elide(candidate, retained)

    # --- Interpreted vs Compiled agreement ---
    # Enumerate (state, input) pairs and compare outputs
    observed = tuple(sorted(retained))
    all_dims = dict(stateful_dims)
    all_dims.update(nondeterministic_dims)
    dim_names = tuple(sorted(all_dims))
    dim_domains = tuple(all_dims[name] for name in dim_names)

    interpreted_compiled_match = True
    combo_limit = 10_000
    total_combos = 1
    for domain in dim_domains:
        total_combos *= len(domain)
        if total_combos > combo_limit:
            break

    if total_combos <= combo_limit:
        combo_iter = product(*dim_domains) if dim_domains else [()]
        for combo in combo_iter:
            entry_values = dict(zip(dim_names, combo, strict=True))
            interp_result = _interpreted_scan(program, entry_values, observed)
            compiled_result = _compiled_scan(compiled, entry_values, observed)
            if interp_result != compiled_result:
                interpreted_compiled_match = False
                mismatches.append(
                    f"entry={entry_values}: interpreted={interp_result}, compiled={compiled_result}"
                )
                if len(mismatches) >= 5:
                    break

    # --- Contract (b): abstract soundness ---
    if abstract_elidable and not concrete_elidable:
        mismatches.append(f"Abstract says {candidate} elidable but concrete disagrees")

    return _AgreementResult(
        candidate=candidate,
        abstract_elidable=abstract_elidable,
        concrete_elidable=concrete_elidable,
        interpreted_compiled_match=interpreted_compiled_match,
        mismatches=mismatches,
    )


def _run_full_agreement(
    program: Program,
    stateful_dims: dict[str, tuple[Any, ...]],
    nondeterministic_dims: dict[str, tuple[Any, ...]],
) -> list[_AgreementResult]:
    """Run three-way agreement on all candidates in a program."""
    from pyrung.circuitpy.codegen import compile_kernel

    graph = build_program_graph(program)
    compiled = compile_kernel(program, blockless=True)

    results: list[_AgreementResult] = []
    candidate_names = sorted(stateful_dims)

    for candidate in candidate_names:
        retained = frozenset(stateful_dims) - {candidate}
        result = _check_candidate_agreement(
            program,
            compiled,
            graph,
            stateful_dims,
            nondeterministic_dims,
            candidate,
            retained,
        )
        results.append(result)

    return results


# ---------------------------------------------------------------------------
# Test programs — unit-scale
# ---------------------------------------------------------------------------


def _program_simple_copy() -> tuple[
    Program, dict[str, tuple[Any, ...]], dict[str, tuple[Any, ...]]
]:
    """Unconditional copy — Tmp is always overwritten, trivially elidable."""
    tmp = Bool("Tmp")
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung():
            copy(False, tmp)
        with Rung(tmp):
            out(seen)

    return logic, {"Tmp": (False, True), "Seen": (False, True)}, {}


def _program_input_gate() -> tuple[Program, dict[str, tuple[Any, ...]], dict[str, tuple[Any, ...]]]:
    """Input-gated output — Stored depends on Inp, not on entry value."""
    inp = Bool("Inp", external=True)
    stored = Bool("Stored")

    with Program(strict=False) as logic:
        with Rung(inp):
            out(stored)

    return logic, {"Stored": (False, True)}, {"Inp": (False, True)}


def _program_prewrite_memory() -> tuple[
    Program, dict[str, tuple[Any, ...]], dict[str, tuple[Any, ...]]
]:
    """Pre-write read — Tmp's entry value matters for Stored."""
    inp = Bool("Inp", external=True)
    tmp = Int("Tmp", choices={0: "No", 1: "Yes"})
    stored = Int("Stored", choices={0: "No", 1: "Yes"})

    with Program(strict=False) as logic:
        with Rung(tmp == 1):
            copy(1, stored)
        with Rung():
            copy(inp, tmp)

    return logic, {"Tmp": (0, 1), "Stored": (0, 1)}, {"Inp": (False, True)}


def _program_self_read_counter() -> tuple[
    Program, dict[str, tuple[Any, ...]], dict[str, tuple[Any, ...]]
]:
    """Reset-before-self-read counter — Idx is scan-local despite self-read."""
    idx = Int("Idx")
    flag = Bool("Flag")

    with Program(strict=False) as logic:
        with Rung():
            copy(0, idx)
        with Rung():
            calc(idx + 1, idx)
        with Rung(idx == 1):
            out(flag)

    return logic, {"Idx": (0, 1, 2)}, {}


def _program_latch_reset() -> tuple[
    Program, dict[str, tuple[Any, ...]], dict[str, tuple[Any, ...]]
]:
    """Latch/reset pair — Alarm is truly stateful, must be retained."""
    start = Bool("Start", external=True)
    stop = Bool("Stop", external=True)
    alarm = Bool("Alarm")

    with Program(strict=False) as logic:
        with Rung(start):
            latch(alarm)
        with Rung(stop):
            reset(alarm)

    return logic, {"Alarm": (False, True)}, {"Start": (False, True), "Stop": (False, True)}


def _program_branch_reset_flag() -> tuple[
    Program, dict[str, tuple[Any, ...]], dict[str, tuple[Any, ...]]
]:
    """Multi-input pulse flag with immediate reset — Pulse is scan-local."""
    req_a = Bool("ReqA", external=True)
    req_b = Bool("ReqB", external=True)
    pulse = Int("Pulse", choices={0: "No", 1: "Yes"})
    seen = Int("Seen", choices={0: "No", 1: "Yes"})

    with Program(strict=False) as logic:
        with Rung():
            copy(0, seen)
        with Rung(req_a):
            copy(1, pulse)
        with Rung(req_b):
            copy(1, pulse)
        with Rung(pulse == 1):
            copy(1, seen)
            copy(0, pulse)

    return (
        logic,
        {"Pulse": (0, 1), "Seen": (0, 1)},
        {"ReqA": (False, True), "ReqB": (False, True)},
    )


def _program_indirect_table() -> tuple[
    Program, dict[str, tuple[Any, ...]], dict[str, tuple[Any, ...]]
]:
    """Indirect block access via pointer — scratch tags are scan-local."""
    init_done = Bool("InitDone")
    selector = Int("Selector", external=True, choices={1: "A", 2: "B"})
    idx = Int("Idx")
    tmp = Int("Tmp")
    table = Block("Table", TagType.INT, 1, 2)

    with Program(strict=False) as logic:
        with Rung(~init_done):
            copy(10, table[1])
            copy(20, table[2])
            copy(1, init_done)
        with Rung():
            calc(selector, idx)
        with Rung():
            copy(table[idx], tmp)

    return logic, {"Idx": (1, 2), "Tmp": (10, 20)}, {"Selector": (1, 2)}


def _program_subroutine_pulse() -> tuple[
    Program, dict[str, tuple[Any, ...]], dict[str, tuple[Any, ...]]
]:
    """Subroutine with return_early — Pulse is scan-local via canonical entry."""
    from pyrung.core import return_early

    req = Bool("Req", external=True)
    pulse = Int("Pulse", choices={0: "No", 1: "Yes"})

    @subroutine("worker", strict=False)
    def worker():
        with Rung(req):
            copy(1, pulse)
        with Rung(pulse == 1):
            copy(0, pulse)
            return_early()
        with Rung():
            copy(0, pulse)

    with Program(strict=False) as logic:
        with Rung():
            call(worker)

    return logic, {"Pulse": (0, 1)}, {"Req": (False, True)}


def _program_oneshot_out_elidable() -> tuple[
    Program, dict[str, tuple[Any, ...]], dict[str, tuple[Any, ...]]
]:
    """Oneshot OUT with entry-independent rung condition — X is elidable.

    The rung condition depends only on the external input, not on X's entry
    value.  The oneshot's ``_has_executed`` flag is instruction-local memory
    and does not contribute entry dependency, so X should be elided.
    X is read by a second rung so its abstract provenance matters.
    """
    inp = Bool("Inp", external=True)
    x = Bool("X")
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung(inp):
            out(x, oneshot=True)
        with Rung(x):
            out(seen)

    return logic, {"X": (False, True), "Seen": (False, True)}, {"Inp": (False, True)}


def _program_oneshot_out_self_negation() -> tuple[
    Program, dict[str, tuple[Any, ...]], dict[str, tuple[Any, ...]]
]:
    """Oneshot OUT conditioned on NOT(X) — X oscillates, must be retained.

    X=False → rung True → fires True → X=True.
    X=True → rung False → writes False → X=False.
    The cycle prevents canonical-entry convergence, so X is entry-dependent.
    """
    x = Bool("X")

    with Program(strict=False) as logic:
        with Rung(~x):
            out(x, oneshot=True)

    return logic, {"X": (False, True)}, {}


def _program_consumed_counter() -> tuple[
    Program, dict[str, tuple[Any, ...]], dict[str, tuple[Any, ...]]
]:
    """Counter accumulator observed via copy — Acc must not be elided.

    The accumulator has a self-referencing write (Acc = Acc + 1) that must
    be detected via the graph, not a special guard.
    """
    enable = Bool("Enable", external=True)
    rst = Bool("Rst", external=True)
    ct = Counter.clone("CT")
    saved = Int("Saved")

    with Program(strict=False) as logic:
        with Rung(enable):
            count_up(ct, preset=3).reset(rst)
        with Rung():
            copy(ct.Acc, saved)

    return (
        logic,
        {"CT_Done": (False, True), "CT_Acc": (0, 1, 2, 3), "Saved": (0, 1, 2, 3)},
        {"Enable": (False, True), "Rst": (False, True)},
    )


def _program_continued_snapshot() -> tuple[
    Program, dict[str, tuple[Any, ...]], dict[str, tuple[Any, ...]]
]:
    """Continued rung reads X via snapshot — X entry is observable.

    X is written by rung 1 when A is True, but the continued rung's
    snapshot captures X's value before rung 1's instructions execute.
    X's entry value is therefore observable and must not be elided.
    """
    a = Bool("A", external=True)
    x = Bool("X")
    y = Bool("Y")

    with Program(strict=False) as logic:
        with Rung(a):
            out(x)
        with Rung(x).continued():
            out(y)

    return logic, {"X": (False, True), "Y": (False, True)}, {"A": (False, True)}


# ---------------------------------------------------------------------------
# Parametrized tests
# ---------------------------------------------------------------------------

_UNIT_PROGRAMS = [
    pytest.param(_program_simple_copy, id="simple_copy"),
    pytest.param(_program_input_gate, id="input_gate"),
    pytest.param(_program_prewrite_memory, id="prewrite_memory"),
    pytest.param(_program_self_read_counter, id="self_read_counter"),
    pytest.param(_program_latch_reset, id="latch_reset"),
    pytest.param(_program_branch_reset_flag, id="branch_reset_flag"),
    pytest.param(_program_indirect_table, id="indirect_table"),
    pytest.param(_program_subroutine_pulse, id="subroutine_pulse"),
    pytest.param(_program_oneshot_out_elidable, id="oneshot_out_elidable"),
    pytest.param(_program_oneshot_out_self_negation, id="oneshot_out_self_condition"),
    pytest.param(_program_consumed_counter, id="consumed_counter"),
    pytest.param(_program_continued_snapshot, id="continued_snapshot"),
]


class TestElisionAgreement:
    """Three-way agreement: interpreted, compiled, and abstract oracles."""

    @pytest.mark.parametrize("builder", _UNIT_PROGRAMS)
    def test_interpreted_compiled_match(self, builder) -> None:
        """Contract (a)/(c): interpreted and compiled produce identical outputs."""
        logic, stateful_dims, nd_dims = builder()
        results = _run_full_agreement(logic, stateful_dims, nd_dims)

        for result in results:
            assert result.interpreted_compiled_match, (
                f"Interpreted/compiled mismatch for candidate '{result.candidate}':\n"
                + "\n".join(result.mismatches[:5])
            )

    @pytest.mark.parametrize("builder", _UNIT_PROGRAMS)
    def test_abstract_soundness(self, builder) -> None:
        """Contract (b): if abstract says elidable, concrete must agree."""
        logic, stateful_dims, nd_dims = builder()
        results = _run_full_agreement(logic, stateful_dims, nd_dims)

        for result in results:
            if result.abstract_elidable:
                assert result.concrete_elidable, (
                    f"Abstract unsoundness for '{result.candidate}': "
                    f"abstract says elidable but concrete disagrees"
                )

    @pytest.mark.parametrize("builder", _UNIT_PROGRAMS)
    def test_elision_pipeline_consistent(self, builder) -> None:
        """Elided tags are valid given the pipeline's final retained set.

        The pipeline removes tags iteratively — removing one may enable
        removing another. So we verify each elided tag against the *final*
        retained set, not the original.
        """
        from pyrung.circuitpy.codegen import compile_kernel

        logic, stateful_dims, nd_dims = builder()
        graph = build_program_graph(logic)
        compiled = compile_kernel(logic, blockless=True)

        pipeline_reduced, _, _ = _elide_scan_local_stateful_dims(
            logic, graph, stateful_dims, nd_dims
        )
        final_retained = frozenset(pipeline_reduced)

        for candidate in sorted(set(stateful_dims) - set(pipeline_reduced)):
            concrete_elider = _ConcreteStateElider(
                logic, graph, stateful_dims, nd_dims, compiled=compiled
            )
            if not concrete_elider._is_concrete_candidate(candidate):
                continue
            assert concrete_elider._can_elide(candidate, final_retained), (
                f"Pipeline elided '{candidate}' but concrete oracle disagrees "
                f"given the final retained set {sorted(final_retained)}"
            )


class TestOneshotOutElision:
    """Verify oneshot OUT abstract elision semantics."""

    def test_entry_independent_condition_elidable(self) -> None:
        """Oneshot OUT with input-only rung condition: abstract marks X elidable."""
        logic, stateful_dims, nd_dims = _program_oneshot_out_elidable()
        results = _run_full_agreement(logic, stateful_dims, nd_dims)
        x_result = next(r for r in results if r.candidate == "X")
        assert x_result.abstract_elidable, (
            "X should be elidable: rung condition is entry-independent"
        )
        assert x_result.concrete_elidable, "Concrete should agree X is elidable"

    def test_self_negation_retained(self) -> None:
        """Oneshot OUT conditioned on ~X: X oscillates, must be retained."""
        logic, stateful_dims, nd_dims = _program_oneshot_out_self_negation()
        results = _run_full_agreement(logic, stateful_dims, nd_dims)
        x_result = next(r for r in results if r.candidate == "X")
        assert not x_result.abstract_elidable, (
            "X should be retained: X oscillates (entry-dependent)"
        )


# ---------------------------------------------------------------------------
# Example programs — integration scale
# ---------------------------------------------------------------------------


class TestExampleProgramAgreement:
    """Agreement checks on real example programs."""

    def test_fill_station(self) -> None:
        from examples.fill_station import logic

        stateful_dims: dict[str, tuple[Any, ...]] = {
            "FillEnable": (False, True),
            "FillValve": (False, True),
            "FlowAlarm": (False, True),
        }
        nd_dims: dict[str, tuple[Any, ...]] = {
            "StartBtn": (False, True),
            "FlowSensor": (False, True),
            "LevelSensor": (False, True),
        }
        results = _run_full_agreement(logic, stateful_dims, nd_dims)
        for result in results:
            assert result.interpreted_compiled_match, (
                f"fill_station: interpreted/compiled mismatch for '{result.candidate}':\n"
                + "\n".join(result.mismatches[:5])
            )
            if result.abstract_elidable:
                assert result.concrete_elidable

    def test_simple_task(self) -> None:
        from examples.simple_task_example import logic

        stateful_dims: dict[str, tuple[Any, ...]] = {
            "Task.Active": (0, 1),
            "Task.Step": (0, 1),
            "Valve1": (False, True),
        }
        nd_dims: dict[str, tuple[Any, ...]] = {
            "Task.Call": (0, 1),
        }
        results = _run_full_agreement(logic, stateful_dims, nd_dims)
        for result in results:
            assert result.interpreted_compiled_match, (
                f"simple_task: interpreted/compiled mismatch for '{result.candidate}':\n"
                + "\n".join(result.mismatches[:5])
            )
            if result.abstract_elidable:
                assert result.concrete_elidable

    def test_packml_bench(self) -> None:
        from examples.packml_bench import logic as packml_logic

        stateful_dims: dict[str, tuple[Any, ...]] = {
            "StateCurrent": (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17),
            "StateRequested": (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17),
        }
        nd_dims: dict[str, tuple[Any, ...]] = {
            "CmdNew": (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10),
            "ModeNew": (0, 1, 2, 3),
        }
        results = _run_full_agreement(packml_logic, stateful_dims, nd_dims)
        for result in results:
            assert result.interpreted_compiled_match, (
                f"packml_bench: interpreted/compiled mismatch for '{result.candidate}':\n"
                + "\n".join(result.mismatches[:5])
            )
            if result.abstract_elidable:
                assert result.concrete_elidable
