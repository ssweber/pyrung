"""Pytest configuration and test helpers."""

from __future__ import annotations

import copy
import os
import sys
import threading
import time
import weakref
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

import pytest

pytest_plugins = ["pytester"]

# ---------------------------------------------------------------------------
# Hard memory cap — kills the process before it OOMs the machine.
# Set PYTEST_MEMORY_CAP_MB to override it (default 4096).  The cap is per process, so each
# xdist worker has its own. The run fails when the cap kills a worker, and names the test.
# ---------------------------------------------------------------------------

_MEMORY_CAP_MB = int(os.environ.get("PYTEST_MEMORY_CAP_MB", "4096"))
_current_test: str | None = None


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("fold_check", default=False):
        from tests import fold_check

        fold_check.install()
    try:
        import psutil
    except ImportError:
        return

    cap_bytes = _MEMORY_CAP_MB * 1024 * 1024
    proc = psutil.Process()
    # Bind time.sleep now. A test's fake clock replaces it later, and a call from this
    # thread would advance that clock mid-scan.
    sleep = time.sleep

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
            sleep(2)

    threading.Thread(target=_monitor, daemon=True).start()


@pytest.fixture(autouse=True)
def _no_wall_clock_in_how(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop how() from reading the wall clock, so answers do not depend on machine load."""
    from pyrung.core.analysis.pilot import budget

    monkeypatch.setattr(budget, "WALL_CLOCK_ENABLED", False)


@pytest.fixture(autouse=True)
def _memory_cap_tracker(request: pytest.FixtureRequest) -> Iterator[None]:
    global _current_test
    _current_test = request.node.nodeid
    yield
    _current_test = None


_BLOCK_CONFIG_BASELINE: weakref.WeakKeyDictionary[Any, tuple[dict[int, Any], dict[int, Any]]] = (
    weakref.WeakKeyDictionary()
)


@pytest.fixture(autouse=True)
def _clean_block_state() -> Iterator[None]:
    """Restore import-time block config between tests.

    Blocks configured at module import (the prebuilt click banks, program
    fixtures like ``tests.fixtures.tumbler`` whose tags.py stamps slot
    defaults once) must keep that config across tests, while slot config and
    mapped-tag registrations added *inside* a test must not leak.  Snapshot
    each block's config the first time it is seen and restore that baseline
    before and after every test — a wipe here would de-configure cached
    fixture modules (Ref command constants read 0, commands silently
    swallowed).
    """
    from pyrung.click.tag_map._parsers import _HARDWARE_BLOCK_CACHE
    from pyrung.core.memory_block import Block
    from pyrung.core.tag import note_metadata_edit

    def _restore_all() -> None:
        for block in Block._all_instances:
            baseline = _BLOCK_CONFIG_BASELINE.get(block)
            if baseline is None:
                # SlotConfig entries are mutated in place; copy them so the
                # baseline stays pristine.
                _BLOCK_CONFIG_BASELINE[block] = (
                    {addr: copy.copy(entry) for addr, entry in block._slot_config.items()},
                    dict(block._mapped_tags),
                )
            else:
                slot_config, mapped_tags = baseline
                if block._slot_config != slot_config or (
                    {addr: id(tag) for addr, tag in block._mapped_tags.items()}
                    != {addr: id(tag) for addr, tag in mapped_tags.items()}
                ):
                    # This restore undoes a test's slot settings behind the block's back.
                    # A shared program's cached static facts may have been built with them.
                    note_metadata_edit()
                block._slot_config = {addr: copy.copy(e) for addr, e in slot_config.items()}
                block._mapped_tags = dict(mapped_tags)
            block._tag_cache.clear()
            block._indexed.clear()
        for block in _HARDWARE_BLOCK_CACHE.values():
            block.reset()
        _HARDWARE_BLOCK_CACHE.clear()

    _restore_all()
    yield
    _restore_all()


from pyrung.core import PLC, CompiledPLC, Program, SystemState
from pyrung.core.condition import Condition
from pyrung.core.context import ScanContext
from pyrung.core.instruction import Instruction
from pyrung.core.program import Program as ProgramLogic
from pyrung.core.rung import Rung
from pyrung.core.site_memory import bind_logic_list, bind_sites
from pyrung.core.synthesis import _rung_holding

_EXPENSIVE_MARKERS = frozenset(
    {
        "hypothesis",
        "integration",
        "fuzz",
        "tumbler",
        "conformance",
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
    group.addoption(
        "--fold-check",
        action="store_true",
        default=False,
        help=(
            "Check every run_until()/run_for() on the guarded loop and every coast "
            "against a stepped twin, scan by scan (tests/fold_check.py)."
        ),
    )
    group.addoption(
        "--fold-check-allow-vacuous",
        action="store_true",
        default=False,
        help=(
            "With --fold-check, judge only mismatches: a session whose long runs all decline "
            "is reported, not failed. For a few files, where the session-wide share of runs "
            "in the loop says little (the mergeq fold-new stage)."
        ),
    )
    group = parser.getgroup("pyrung-conformance", "pyrung conformance suite")
    group.addoption(
        "--conformance-plc",
        action="store",
        default=None,
        metavar="HOST[:PORT]",
        help=(
            "Run the conformance marker against a CLICK at HOST[:PORT] instead of "
            "pyrung's local soft PLC (env: PYRUNG_CONFORMANCE_PLC)."
        ),
    )
    group.addoption(
        "--conformance-group",
        action="store",
        default=None,
        metavar="NAME[,NAME...]",
        help=(
            "Run only these conformance groups over Modbus "
            "(env: PYRUNG_CONFORMANCE_GROUP); default is every group."
        ),
    )
    group.addoption(
        "--conformance-target",
        action="store",
        default=None,
        choices=("pyrung", "circuitpy"),
        help=(
            "Local target when no --conformance-plc is given: pyrung's soft PLC "
            "(default) or the program's generated P1AM-200 code run on this "
            "machine (env: PYRUNG_CONFORMANCE_TARGET)."
        ),
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
    assert _nan_free(left_state.tags) == _nan_free(right_state.tags)
    assert _nan_free(left_state.memory) == _nan_free(right_state.memory)


def _nan_free(values: Any) -> dict[str, Any]:
    """Return *values* as a dict in which NaN is spelled so that two NaNs compare equal.

    A REAL may hold NaN.
    """
    return {
        key: "<NaN>" if isinstance(value, float) and value != value else value
        for key, value in values.items()
    }


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
    def dt(self) -> float:
        assert self._interpreted.dt == self._compiled.dt
        return self._interpreted.dt

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

    def start(self) -> None:
        self._interpreted.start()
        self._compiled.start()
        _assert_states_match(self._interpreted, self._compiled)

    @property
    def mode(self) -> str:
        assert self._interpreted.mode == self._compiled.mode
        return self._interpreted.mode

    @property
    def pending_patches(self):  # noqa: ANN201
        assert dict(self._interpreted.pending_patches) == dict(self._compiled.pending_patches)
        return self._interpreted.pending_patches

    @property
    def keep_forces_on_run(self) -> bool:
        assert self._interpreted.keep_forces_on_run == self._compiled.keep_forces_on_run
        return self._interpreted.keep_forces_on_run

    @keep_forces_on_run.setter
    def keep_forces_on_run(self, value: bool) -> None:
        self._interpreted.keep_forces_on_run = value
        self._compiled.keep_forces_on_run = value

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
    if instr._state_key is None:
        # A lone instruction is the only site of its one-instruction program.
        bind_sites([_rung_holding(None, instr)])
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
    bind_logic_list([rung])
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


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):  # noqa: ANN001, ANN201
    import pluggy

    outcome: pluggy.Result = yield  # type: ignore[assignment]
    rep = outcome.get_result()
    if rep.when == "call":
        item.rep_call = rep  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _a_fold_mismatch_fails_its_test(request: pytest.FixtureRequest) -> Any:
    """Fail the test that met a mismatch, even if something on the way out caught it."""
    module = sys.modules.get("tests.fold_check")
    before = 0 if module is None else len(module.undeclared())
    yield
    module = sys.modules.get("tests.fold_check")
    if module is None:
        return
    found = module.undeclared()[before:]
    call = getattr(request.node, "rep_call", None)
    if found and (call is None or not call.failed):
        pytest.fail(f"a fold mismatch was caught and hidden: {found[0][1]}", pytrace=False)


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: Any) -> None:
    """Absorb an xdist worker's record of the check, for the controller to judge with the rest."""
    record = getattr(node, "workeroutput", {}).get("fold_check")
    if record is not None:
        from tests import fold_check

        fold_check.absorb(record)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    fold_check = sys.modules.get("tests.fold_check")
    if fold_check is None:
        return
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None:
        # The controller sets the exit code, and a worker has only part of the session.
        workeroutput["fold_check"] = fold_check.export()
        return
    if exitstatus != 0:
        return
    vacuous = (
        session.config.getoption("fold_check", default=False)
        and not session.config.getoption("fold_check_allow_vacuous", default=False)
        and fold_check.vacuous()
    )
    if fold_check.undeclared() or vacuous:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    if config.getoption("fold_check", default=False):
        from tests import fold_check

        terminalreporter.write_line("fold-check:")
        for line in fold_check.summary():
            terminalreporter.write_line(line)
        problem = fold_check.vacuous()
        if problem is not None:
            if config.getoption("fold_check_allow_vacuous", default=False):
                terminalreporter.write_line(f"fold-check (vacuity not judged): {problem}")
            else:
                terminalreporter.write_line(f"fold-check VACUOUS: {problem}", red=True)
