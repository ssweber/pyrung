"""Pytest configuration and test helpers."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

import pytest

pytest_plugins = ["pytester"]

# ---------------------------------------------------------------------------
# Hard memory cap — kills the process before it OOMs the machine.
# Override with PYTEST_MEMORY_CAP_MB env var (default 2048).
# ---------------------------------------------------------------------------

_MEMORY_CAP_MB = int(os.environ.get("PYTEST_MEMORY_CAP_MB", "2048"))
_current_test: str | None = None


def pytest_configure(config: pytest.Config) -> None:
    try:
        import psutil
    except ImportError:
        return

    cap_bytes = _MEMORY_CAP_MB * 1024 * 1024
    proc = psutil.Process()

    def _monitor() -> None:
        while True:
            try:
                rss = proc.memory_info().rss
            except psutil.NoSuchProcess:
                return
            if rss > cap_bytes:
                test = _current_test or "<between tests>"
                sys.stderr.write(
                    f"\nFATAL: pytest RSS ({rss >> 20} MB) exceeded "
                    f"{_MEMORY_CAP_MB} MB cap during {test}. "
                    f"Aborting to prevent OOM.\n"
                )
                sys.stderr.flush()
                os._exit(99)
            time.sleep(2)

    threading.Thread(target=_monitor, daemon=True).start()


@pytest.fixture(autouse=True)
def _memory_cap_tracker(request: pytest.FixtureRequest) -> Iterator[None]:
    global _current_test
    _current_test = request.node.nodeid
    yield
    _current_test = None


from pyrung.core import PLC, CompiledPLC, Program, SystemState
from pyrung.core.analysis.prove import Counterexample, Proven, prove
from pyrung.core.analysis.prove import reachable_states as _original_reachable_states
from pyrung.core.condition import Condition
from pyrung.core.context import ScanContext
from pyrung.core.instruction import Instruction
from pyrung.core.program import Program as ProgramLogic
from pyrung.core.rung import Rung

_EXPENSIVE_MARKERS = frozenset(
    {
        "soundness",
        "hypothesis",
        "integration",
        "fuzz",
        "parity",
        "known_answer",
    }
)


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("pyrung-test", "pyrung test runner selection")
    group.addoption(
        "--runner-backend",
        action="store",
        default="interpreted",
        choices=("interpreted", "compiled", "both"),
        help=(
            "Backend used by tests that opt into runner_factory: "
            "'interpreted' uses PLC, 'compiled' uses CompiledPLC, "
            "'both' runs both and asserts state parity."
        ),
    )
    group = parser.getgroup("pyrung-prove", "pyrung prove debug tools")
    group.addoption(
        "--prove-debug",
        action="store_true",
        default=False,
        help="Inject _debug=True into prove()/reachable_states() calls and dump _ExploreContext on failure.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.option.markexpr or config.option.file_or_dir:
        return

    safe_items: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        if _EXPENSIVE_MARKERS & {m.name for m in item.iter_markers()}:
            deselected.append(item)
        else:
            safe_items.append(item)

    if not deselected:
        return

    config.hook.pytest_deselected(items=deselected)
    items[:] = safe_items
    tw = config.get_terminal_writer()
    tw.sep("!", f"Auto-skipped {len(deselected)} expensive tests (no -m filter)")
    tw.line("Use `make test` or pass -m explicitly. See Makefile for targets.")


def _assert_states_match(left: PLC | CompiledPLC, right: PLC | CompiledPLC) -> None:
    left_state = left.current_state
    right_state = right.current_state
    assert left_state.scan_id == right_state.scan_id
    assert left_state.timestamp == pytest.approx(right_state.timestamp)
    assert dict(left_state.tags) == dict(right_state.tags)
    assert dict(left_state.memory) == dict(right_state.memory)


class _RunnerPair:
    """Run both backends in lockstep and expose a PLC-like test surface."""

    def __init__(self, interpreted: PLC, compiled: CompiledPLC) -> None:
        self._interpreted = interpreted
        self._compiled = compiled
        _assert_states_match(self._interpreted, self._compiled)

    @property
    def current_state(self) -> SystemState:
        _assert_states_match(self._interpreted, self._compiled)
        return self._interpreted.current_state

    @property
    def simulation_time(self) -> float:
        _assert_states_match(self._interpreted, self._compiled)
        return self._interpreted.simulation_time

    @property
    def forces(self):  # noqa: ANN201
        assert dict(self._interpreted.forces) == dict(self._compiled.forces)
        return self._interpreted.forces

    @property
    def battery_present(self) -> bool:
        assert self._interpreted.battery_present == self._compiled.battery_present
        return self._interpreted.battery_present

    @battery_present.setter
    def battery_present(self, value: bool) -> None:
        self._interpreted.battery_present = value
        self._compiled.battery_present = value
        assert self._interpreted.battery_present == self._compiled.battery_present

    def patch(
        self,
        tags: dict[str, Any] | dict[Any, Any],
    ) -> None:
        self._interpreted.patch(tags)
        self._compiled.patch(tags)

    def force(self, tag: str | Any, value: bool | int | float | str) -> None:
        self._interpreted.force(tag, value)
        self._compiled.force(tag, value)
        assert dict(self._interpreted.forces) == dict(self._compiled.forces)

    def unforce(self, tag: str | Any) -> None:
        self._interpreted.unforce(tag)
        self._compiled.unforce(tag)
        assert dict(self._interpreted.forces) == dict(self._compiled.forces)

    def clear_forces(self) -> None:
        self._interpreted.clear_forces()
        self._compiled.clear_forces()
        assert dict(self._interpreted.forces) == dict(self._compiled.forces) == {}

    @contextmanager
    def forced(self, overrides: dict[str, Any] | dict[Any, Any]) -> Iterator[_RunnerPair]:
        with ExitStack() as stack:
            stack.enter_context(self._interpreted.forced(overrides))
            stack.enter_context(self._compiled.forced(overrides))
            assert dict(self._interpreted.forces) == dict(self._compiled.forces)
            yield self
        assert dict(self._interpreted.forces) == dict(self._compiled.forces)

    def set_rtc(self, value) -> None:  # noqa: ANN001
        self._interpreted.set_rtc(value)
        self._compiled.set_rtc(value)

    def step(self) -> SystemState:
        self._interpreted.step()
        self._compiled.step()
        _assert_states_match(self._interpreted, self._compiled)
        return self._interpreted.current_state

    def run(self, cycles: int) -> SystemState:
        self._interpreted.run(cycles)
        self._compiled.run(cycles)
        _assert_states_match(self._interpreted, self._compiled)
        return self._interpreted.current_state

    def run_for(self, seconds: float) -> SystemState:
        self._interpreted.run_for(seconds)
        self._compiled.run_for(seconds)
        _assert_states_match(self._interpreted, self._compiled)
        return self._interpreted.current_state

    def stop(self) -> None:
        self._interpreted.stop()
        self._compiled.stop()
        _assert_states_match(self._interpreted, self._compiled)

    def reboot(self) -> SystemState:
        self._interpreted.reboot()
        self._compiled.reboot()
        _assert_states_match(self._interpreted, self._compiled)
        return self._interpreted.current_state


@pytest.fixture
def runner_backend(request: pytest.FixtureRequest) -> str:
    return str(request.config.getoption("runner_backend"))


@pytest.fixture
def runner_factory(runner_backend: str):
    """Build a backend-selected runner for fixed-step Program tests."""

    def _build(*args: Any, **kwargs: Any) -> PLC | CompiledPLC | _RunnerPair:
        if len(args) > 1:
            pytest.skip("runner_factory only supports a single positional logic argument")

        logic = args[0] if args else kwargs.pop("logic", None)
        if logic is None:
            pytest.skip("runner_factory requires a Program when using compiled replay backends")
        if not isinstance(logic, Program):
            pytest.skip("runner_factory compiled backends currently support Program inputs only")

        unsupported = sorted(set(kwargs) - {"dt", "initial_state", "compiled"})
        if unsupported:
            joined = ", ".join(unsupported)
            pytest.skip(f"runner_factory compiled backends do not support kwargs: {joined}")

        if runner_backend == "interpreted":
            return PLC(logic, **kwargs)
        if runner_backend == "compiled":
            return CompiledPLC(logic, **kwargs)

        interpreted = PLC(logic, **kwargs)
        compiled = CompiledPLC(logic, **kwargs)
        return _RunnerPair(interpreted, compiled)

    return _build


def execute(instr: Instruction, state: SystemState, *, dt: float = 0.0) -> SystemState:
    """Execute an instruction and return the new state.

    Test helper that wraps the ScanContext API for simpler unit testing
    of individual instructions.

    Args:
        instr: The instruction to execute.
        state: The initial system state.
        dt: Time delta in seconds (for timer instructions).

    Returns:
        New SystemState with the instruction's effects applied.
    """
    ctx = ScanContext(state)
    ctx.set_memory("_dt", dt)  # For timer instructions
    instr.execute(ctx, True)
    return ctx.commit(dt=dt)


def evaluate_rung(rung: Rung, state: SystemState, *, dt: float = 0.0) -> SystemState:
    """Evaluate a rung and return the new state.

    Test helper that wraps the ScanContext API for simpler unit testing
    of individual rungs.

    Args:
        rung: The rung to evaluate.
        state: The initial system state.
        dt: Time delta in seconds (for timer instructions).

    Returns:
        New SystemState with the rung's effects applied.
    """
    ctx = ScanContext(state)
    ctx.set_memory("_dt", dt)  # For timer instructions
    rung.evaluate(ctx)
    return ctx.commit(dt=dt)


def evaluate_condition(cond: Condition, state: SystemState) -> bool:
    """Evaluate a condition and return the result.

    Test helper that wraps the ScanContext API for simpler unit testing
    of individual conditions.

    Args:
        cond: The condition to evaluate.
        state: The system state to evaluate against.

    Returns:
        Boolean result of the condition evaluation.
    """
    ctx = ScanContext(state)
    return cond.evaluate(ctx)


def evaluate_program(program: ProgramLogic, state: SystemState, *, dt: float = 0.0) -> SystemState:
    """Evaluate a program and return the new state.

    Test helper that wraps the ScanContext API for simpler unit testing
    of complete programs.

    Args:
        program: The program to evaluate.
        state: The initial system state.
        dt: Time delta in seconds (for timer instructions).

    Returns:
        New SystemState with the program's effects applied.
    """
    ctx = ScanContext(state)
    ctx.set_memory("_dt", dt)  # For timer instructions
    program._evaluate(ctx)
    return ctx.commit(dt=dt)


# ---------------------------------------------------------------------------
# --prove-debug: inject _debug=True and dump _ExploreContext on failure
# ---------------------------------------------------------------------------


def _format_context(ctx: Any) -> str:
    lines: list[str] = []
    lines.append(f"  stateful_names:         {ctx.stateful_names}")
    lines.append(f"  stateful_dims:          {dict(ctx.stateful_dims)}")
    lines.append(f"  nondeterministic_names: {ctx.nondeterministic_names}")
    lines.append(f"  nondeterministic_dims:  {dict(ctx.nondeterministic_dims)}")
    lines.append(f"  edge_tag_names:         {ctx.edge_tag_names}")
    lines.append(f"  memory_key_names:       {ctx.memory_key_names}")
    lines.append(f"  demoted_edge_names:     {ctx.demoted_edge_names}")

    lines.append(f"  threshold_vector_specs ({len(ctx.threshold_vector_specs)}):")
    for vs in ctx.threshold_vector_specs:
        lines.append(f"    acc={vs.acc_name}  kind={vs.kind}  atoms={vs.atoms}")

    lines.append(f"  done_event_specs ({len(ctx.done_event_specs)}):")
    for de in ctx.done_event_specs:
        lines.append(f"    acc={de.acc_name}  kind={de.kind}  preset={de.preset}")

    lines.append(f"  threshold_event_specs ({len(ctx.threshold_event_specs)}):")
    for te in ctx.threshold_event_specs:
        lines.append(f"    acc={te.acc_name}  kind={te.kind}  threshold={te.threshold}")

    if ctx.journal:
        lines.append(f"  journal ({len(ctx.journal)} tags):")
        for entry in ctx.journal:
            lines.append(f"    {entry.name}: {entry.outcome}")
            if entry.domain is not None:
                lines.append(f"      domain: {entry.domain} (source: {entry.domain_source})")
            for d in entry.decisions:
                lines.append(f"      [{d.pass_name}] {d.kind} -> {d.outcome}: {d.reason}")

    return "\n".join(lines)


def _dump_result(label: str, result: Any) -> str:
    lines: list[str] = [f"--- {label}: {type(result).__name__} ---"]

    ctx = getattr(result, "_debug_context", None)
    if ctx is not None:
        lines.append(_format_context(ctx))
    else:
        lines.append("  (no _debug_context attached)")

    if isinstance(result, Proven):
        lines.append(f"  states_explored: {result.states_explored}")
    if isinstance(result, Counterexample):
        lines.append(f"  trace ({len(result.trace)} steps):")
        for i, step in enumerate(result.trace):
            lines.append(f"    [{i}] inputs={step.inputs} scans={step.scans}")

    return "\n".join(lines)


@pytest.fixture(autouse=True)
def _prove_debug_dumper(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    if not request.config.getoption("prove_debug"):
        yield
        return

    captured: list[tuple[str, Any]] = []

    original_prove = prove

    def _debug_prove(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("_debug", True)
        kwargs.setdefault("journal", True)
        result = original_prove(*args, **kwargs)
        skip = kwargs.get("_skip_optimizations", False)
        opt_cfg = kwargs.get("_opt_config")
        if opt_cfg is not None:
            label = f"prove({','.join(opt_cfg.active_optimizations) or 'no-opts'})"
        elif skip:
            label = "prove(skip_opt)"
        else:
            label = "prove(optimized)"
        if isinstance(result, list):
            for i, r in enumerate(result):
                captured.append((f"{label}[{i}]", r))
        else:
            captured.append((label, result))
        return result

    def _debug_reachable(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("_debug", True)
        kwargs.setdefault("_journal", True)
        result = _original_reachable_states(*args, **kwargs)
        skip = kwargs.get("_skip_optimizations", False)
        label = "reachable(skip_opt)" if skip else "reachable(optimized)"
        captured.append((label, result))
        return result

    mod = request.module
    if hasattr(mod, "prove"):
        monkeypatch.setattr(mod, "prove", _debug_prove)
    if hasattr(mod, "reachable_states"):
        monkeypatch.setattr(mod, "reachable_states", _debug_reachable)

    yield

    if request.node.rep_call is not None and request.node.rep_call.failed and captured:
        out = sys.stderr
        out.write(f"\n{'=' * 70}\n")
        out.write(f"  --prove-debug dump for {request.node.nodeid}\n")
        out.write(f"{'=' * 70}\n")
        for label, result in captured:
            out.write(_dump_result(label, result))
            out.write("\n")
        out.write(f"{'=' * 70}\n")
        out.flush()


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):  # noqa: ANN001, ANN201
    import pluggy

    outcome: pluggy.Result = yield  # type: ignore[assignment]
    rep = outcome.get_result()
    if rep.when == "call":
        item.rep_call = rep  # type: ignore[attr-defined]
