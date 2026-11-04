"""A ``run_function`` that scans, stops or reboots the PLC running it raises, on both backends.

The refusal sits around the user's callable, not around the scan: a paused ``scan_steps``
generator lies between prepare and commit, and stepping beside it stays legal.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from pyrung import (
    Bool,
    Int,
    Program,
    Rung,
    calc,
    copy,
    run_enabled_function,
    run_function,
    subroutine,
)
from pyrung import (
    call as run_subroutine,
)
from pyrung.circuitpy.codegen import compile_kernel
from pyrung.core import compiled_run, scan_trace
from pyrung.core.compiled_plc import CompiledPLC
from pyrung.core.runner import PLC, _may_be_called_again

Cnt, X, Seen, Patched = Int("Cnt"), Int("X"), Int("Seen"), Int("Patched")
NEVER = Bool("Never")

REFUSAL = "run_function called step"

SCANNERS: dict[str, Callable[[Any], Any]] = {
    "step": lambda plc: plc.step(),
    "run": lambda plc: plc.run(1),
    "run_for": lambda plc: plc.run_for(0.05),
}
INTERPRETED_SCANNERS: dict[str, Callable[[Any], Any]] = {
    **SCANNERS,
    "run_for_stepped": lambda plc: plc.run_for(0.05, jit=False),
    "run_until": lambda plc: plc.run_until(NEVER, max_cycles=2),
    "run_until_stepped": lambda plc: plc.run_until(NEVER, max_cycles=2, jit=False),
    "run_until_callable": lambda plc: plc.run_until(lambda _state: False, max_cycles=2),
    "scan_steps": lambda plc: next(plc.debug.scan_steps()),
    "scan_steps_debug": lambda plc: next(plc.debug.scan_steps_debug()),
    "prepare_scan": lambda plc: plc.debug.prepare_scan(),
}
COMPILED_SCANNERS: dict[str, Callable[[Any], Any]] = {
    **SCANNERS,
    "step_replay": lambda plc: plc.step_replay(),
}

BACKENDS: dict[str, Callable[..., Any]] = {
    "interpreted": lambda program: PLC(program, dt=0.01),
    "compiled": lambda program: CompiledPLC(program, dt=0.01),
}


@pytest.fixture(params=sorted(BACKENDS))
def make(request: pytest.FixtureRequest) -> Callable[..., Any]:
    return BACKENDS[request.param]


class Call:
    """What the program's callable does on its next call: nothing, until a test says."""

    def __init__(self) -> None:
        self.action: Callable[[], Any] | None = None

    def __call__(self, c: int = 0) -> dict[str, int]:
        action, self.action = self.action, None
        if action is not None:
            action()
        return {"seen": c}


def _program(call: Call) -> Program:
    """The review's case: the callable sits below a write its scan owes, above the count."""
    with Program(strict=False) as program:
        with Rung(Cnt == 3):
            copy(5, X)
        with Rung():
            run_function(call, ins={"c": Cnt}, outs={"seen": Seen})
        with Rung():
            calc(Cnt + 1, Cnt)
    return program


def _tags(plc: Any) -> dict[str, Any]:
    return dict(plc.current_state.tags)


def _built(make: Callable[..., Any]) -> tuple[Any, Call]:
    call = Call()
    return make(_program(call)), call


def _refused_scan(plc: Any, call: Call, scanner: Callable[[Any], Any]) -> None:
    call.action = lambda: scanner(plc)
    with pytest.raises(RuntimeError, match=REFUSAL):
        plc.step()


# -- the refusal --


@pytest.mark.parametrize("scanner", INTERPRETED_SCANNERS.values(), ids=INTERPRETED_SCANNERS.keys())
def test_interpreted_scan_refuses_a_callable_scanning_its_plc(scanner):
    plc, call = _built(BACKENDS["interpreted"])
    plc.run(3)
    before = plc.current_state
    _refused_scan(plc, call, scanner)
    assert plc.current_state is before


@pytest.mark.parametrize("scanner", COMPILED_SCANNERS.values(), ids=COMPILED_SCANNERS.keys())
def test_kernel_scan_refuses_a_callable_scanning_its_plc(scanner):
    plc, call = _built(BACKENDS["compiled"])
    plc.run(3)
    before = plc.current_state
    _refused_scan(plc, call, scanner)
    assert plc.current_state is before


def test_refusal_in_a_kernels_plant_pass():
    call = Call()
    program = _program(call)
    plc = CompiledPLC(program, dt=0.01, compiled=compile_kernel(program, split_after=2))
    plc.run(3)
    before = plc.current_state
    _refused_scan(plc, call, SCANNERS["step"])
    assert plc.current_state is before


def test_replay_scan_of_a_kernel_refuses_a_callable_scanning_its_plc():
    plc, call = _built(BACKENDS["compiled"])
    call.action = lambda: plc.step()
    with pytest.raises(RuntimeError, match=REFUSAL):
        plc.step_replay()


def test_enabled_function_refused_on_both_paths(make):
    def body(enabled: bool) -> dict[str, int]:
        if enabled:
            plc.step()
        return {"seen": 1}

    with Program(strict=False) as program:
        with Rung(Cnt == 2):
            run_enabled_function(body, outs={"seen": Seen})
        with Rung():
            calc(Cnt + 1, Cnt)
    plc = make(program)
    plc.run(2)
    with pytest.raises(RuntimeError, match=REFUSAL):
        plc.step()


def test_both_paths_raise_the_same_words():
    messages = []
    for backend in sorted(BACKENDS):
        plc, call = _built(BACKENDS[backend])
        call.action = plc.step
        with pytest.raises(RuntimeError) as raised:
            plc.step()
        messages.append(str(raised.value))
    assert messages[0] == messages[1]
    assert messages[0].isascii()
    assert "fork()" in messages[0]
    assert "CompiledPLC" in messages[0]
    assert "outs=" in messages[0]
    assert "stop()" in messages[0]
    assert "reboot()" in messages[0]
    assert "battery_present" in messages[0]


def test_the_review_case_leaves_nothing_to_diverge(make):
    """A scan that wrote X and stepped its PLC published X=0 and read X=5 ever after."""
    plc, call = _built(make)
    plain, _ = _built(make)
    plc.run(3)
    plain.run(3)

    _refused_scan(plc, call, SCANNERS["step"])
    assert _tags(plc) == _tags(plain)

    # Nothing of the refused scan stays behind: the same scan then runs as it would have.
    plc.step()
    plain.step()
    assert _tags(plc) == _tags(plain)
    assert plc.current_state.tags["X"] == 5
    if isinstance(plc, PLC):
        assert plc._state_tags_read_cache["X"] == 5


def test_a_callable_that_swallows_the_refusal_changes_nothing(make):
    """The refusal comes before the inner scan touches anything, a queued patch included."""
    plc, call = _built(make)
    plain, plain_call = _built(make)
    plc.run(3)
    plain.run(3)

    def tries() -> None:
        plc.patch({"Patched": 7})
        with pytest.raises(RuntimeError, match=REFUSAL):
            plc.step()

    call.action = tries
    plain_call.action = lambda: plain.patch({"Patched": 7})
    for _ in range(2):
        plc.step()
        plain.step()
        assert _tags(plc) == _tags(plain)
    assert plc.current_state.tags["Patched"] == 7


def test_a_callable_in_a_subroutine_is_refused(make):
    """A body below a call is in the same scan, and a kernel knows it calls one."""
    call = Call()
    with Program(strict=False) as program:
        with Rung():
            run_subroutine("worker")
        with subroutine("worker"):
            with Rung():
                run_function(call, ins={"c": Cnt}, outs={"seen": Seen})
    plc = make(program)
    plc.step()
    before = plc.current_state
    _refused_scan(plc, call, SCANNERS["step"])
    assert plc.current_state is before


def test_a_kernel_says_whether_it_calls_a_function():
    """The marker is skipped on the word of this flag, so a body must never be left out."""
    with Program(strict=False) as plain:
        with Rung():
            calc(Cnt + 1, Cnt)
    assert compile_kernel(plain).calls_functions is False
    assert compile_kernel(_program(Call())).calls_functions is True


# -- stop and reboot: no scan, but the scan's commit would land on top of them --


LIFECYCLE: dict[str, Callable[[Any], Any]] = {
    "stop": lambda plc: plc.stop(),
    "reboot": lambda plc: plc.reboot(),
}
lifecycle = pytest.mark.parametrize("act", LIFECYCLE.values(), ids=LIFECYCLE.keys())


@lifecycle
def test_a_callable_may_not_stop_or_reboot_its_plc(make, act):
    plc, call = _built(make)
    plain, _ = _built(make)
    plc.run(3)
    plain.run(3)
    before = plc.current_state

    _refused_scan(plc, call, act)
    assert plc.current_state is before
    assert plc.mode == "run"

    # Nothing of it stays behind: the same scan then runs as it would have.
    plc.step()
    plain.step()
    assert _tags(plc) == _tags(plain)
    assert plc.current_state.scan_id == plain.current_state.scan_id


@lifecycle
def test_swallowing_the_lifecycle_refusal_changes_nothing(make, act):
    plc, call = _built(make)
    plain, _ = _built(make)
    plc.run(3)
    plain.run(3)

    def tries() -> None:
        with pytest.raises(RuntimeError, match=REFUSAL):
            act(plc)

    call.action = tries
    plc.step()
    plain.step()
    assert _tags(plc) == _tags(plain)
    assert plc.current_state.scan_id == plain.current_state.scan_id
    assert plc.mode == "run"


@lifecycle
def test_lifecycle_refusal_in_a_kernels_plant_pass(act):
    call = Call()
    program = _program(call)
    plc = CompiledPLC(program, dt=0.01, compiled=compile_kernel(program, split_after=2))
    plc.run(3)
    before = plc.current_state
    _refused_scan(plc, call, act)
    assert plc.current_state is before
    assert plc.mode == "run"


@lifecycle
def test_a_long_run_raises_on_a_lifecycle_call_too(act):
    """No kernel can run this body, so stepped is the path; jit=True takes it past the gate.

    Stepped here because --fold-check's twin runs the same closure, whose ``plc`` is then
    another PLC, on which the call is legal.  The gate itself is tested above.
    """

    def inner(c: int) -> dict[str, int]:
        if c == 20:
            act(plc)
        return {"seen": c}

    with Program(strict=False) as program:
        with Rung():
            run_function(inner, ins={"c": Cnt}, outs={"seen": Seen})
        with Rung():
            calc(Cnt + 1, Cnt)
    plc = PLC(program, dt=0.01)
    assert plc._compiled_replay_supported_kernel() is None
    with pytest.raises(RuntimeError, match=REFUSAL):
        plc.run_for(1.0, jit=False)
    assert plc.current_state.scan_id == 20
    assert plc.mode == "run"


@lifecycle
def test_lifecycle_calls_outside_a_callable_stay_legal(make, act):
    plc, call = _built(make)
    plc.run(3)
    _refused_scan(plc, call, act)  # The marker has cleared, so the same call now goes through.
    act(plc)
    if act is LIFECYCLE["stop"]:
        assert plc.mode == "stop"
        assert plc.current_state.scan_id == 3
    else:
        assert plc.mode == "run"
        assert plc.current_state.scan_id == 0


@lifecycle
@pytest.mark.parametrize("inner_backend", sorted(BACKENDS))
def test_a_callable_may_stop_or_reboot_a_different_plc(make, inner_backend, act):
    other, _ = _built(BACKENDS[inner_backend])
    other.run(2)
    plc, call = _built(make)
    call.action = lambda: act(other)
    assert plc.step().scan_id == 1
    if act is LIFECYCLE["stop"]:
        assert other.mode == "stop"
    else:
        assert other.current_state.scan_id == 0


@lifecycle
@pytest.mark.parametrize("debug", [False, True], ids=["scan_steps", "scan_steps_debug"])
def test_lifecycle_calls_beside_a_paused_or_abandoned_scan_stay_legal(act, debug):
    paused, _ = _built(BACKENDS["interpreted"])
    paused.run(2)
    gen = _paused_past_the_callable(paused, debug=debug)
    act(paused)
    assert paused.mode == ("stop" if act is LIFECYCLE["stop"] else "run")
    gen.close()
    paused.debug.abandon_scan()

    abandoned, _ = _built(BACKENDS["interpreted"])
    abandoned.run(2)
    gen = _paused_past_the_callable(abandoned, debug=debug)
    gen.close()
    abandoned.debug.abandon_scan()
    act(abandoned)
    assert abandoned.mode == paused.mode
    assert abandoned.current_state.scan_id == paused.current_state.scan_id
    assert _tags(abandoned) == _tags(paused)


@pytest.mark.parametrize("debug", [False, True], ids=["scan_steps", "scan_steps_debug"])
def test_a_paused_scans_callable_may_not_stop_it_even_once_stopped(debug):
    """The refusal doesn't depend on the mode: a stopped PLC's paused scan gets the same answer."""
    plc, call = _built(BACKENDS["interpreted"])
    plc.run(3)
    gen = plc.debug.scan_steps_debug() if debug else plc.debug.scan_steps()
    next(gen)
    plc.stop()
    call.action = plc.stop
    with pytest.raises(RuntimeError, match=REFUSAL):
        for _ in gen:
            pass
    assert plc.mode == "stop"
    plc.debug.abandon_scan()


def test_a_commit_callback_may_stop_the_plc():
    """After the commit, as it always was: the scan stands and the mode is STOP."""
    plc, _call = _built(BACKENDS["interpreted"])
    fired: list[int] = []

    def stops(state: Any) -> None:
        if not fired:
            fired.append(state.scan_id)
            plc.stop()

    plc.when(Cnt == 2).do(stops)
    plc.run(2)
    assert fired == [2]
    assert plc.mode == "stop"
    assert plc.current_state.scan_id == 2
    assert plc.current_state.tags["Cnt"] == 2
    assert plc.step().scan_id == 1  # The next step restarts.


def test_a_commit_callback_may_reboot_the_plc():
    """After the commit, as it always was: a battery keeps the tags and the scan id restarts."""
    plc, _call = _built(BACKENDS["interpreted"])
    fired: list[int] = []

    def reboots(state: Any) -> None:
        if not fired:
            fired.append(state.scan_id)
            plc.reboot()

    plc.when(Cnt == 2).do(reboots)
    plc.run(2)
    assert fired == [2]
    assert plc.mode == "run"
    assert plc.current_state.scan_id == 0
    assert plc.current_state.tags["Cnt"] == 2
    assert plc.step().scan_id == 1


# -- the battery: the scan's commit lands on top of this too --


def _set_battery(plc: Any, value: bool) -> None:
    plc.battery_present = value


# True is what it already holds: the refusal doesn't depend on the value changing.
battery_values = pytest.mark.parametrize("value", [False, True], ids=["removed", "unchanged"])


@battery_values
def test_a_callable_may_not_set_its_plcs_battery(make, value):
    plc, call = _built(make)
    plain, _ = _built(make)
    plc.run(3)
    plain.run(3)
    before = plc.current_state

    _refused_scan(plc, call, lambda target: _set_battery(target, value))
    assert plc.current_state is before
    assert plc.battery_present is True

    plc.step()
    plain.step()
    assert _tags(plc) == _tags(plain)
    assert plc.battery_present is True


@battery_values
def test_swallowing_the_battery_refusal_changes_nothing(make, value):
    plc, call = _built(make)
    plain, _ = _built(make)
    plc.run(3)
    plain.run(3)

    def tries() -> None:
        with pytest.raises(RuntimeError, match=REFUSAL):
            _set_battery(plc, value)

    call.action = tries
    plc.step()
    plain.step()
    assert _tags(plc) == _tags(plain)
    assert plc.battery_present is True


def test_battery_refusal_in_a_kernels_plant_pass():
    call = Call()
    program = _program(call)
    plc = CompiledPLC(program, dt=0.01, compiled=compile_kernel(program, split_after=2))
    plc.run(3)
    before = plc.current_state
    _refused_scan(plc, call, lambda target: _set_battery(target, False))
    assert plc.current_state is before
    assert plc.battery_present is True


def test_a_callable_may_read_its_plcs_battery(make):
    plc, call = _built(make)
    read: list[bool] = []
    call.action = lambda: read.append(plc.battery_present)
    plc.step()
    assert read == [True]


def test_setting_the_battery_outside_a_callable_stays_legal(make):
    plc, call = _built(make)
    plc.run(3)
    _refused_scan(plc, call, lambda target: _set_battery(target, False))
    _set_battery(plc, False)  # The marker has cleared, so the same call now goes through.
    assert plc.battery_present is False
    plc.step()
    assert plc.battery_present is False
    _set_battery(plc, True)
    plc.step()
    assert plc.battery_present is True


@pytest.mark.parametrize("inner_backend", sorted(BACKENDS))
def test_a_callable_may_set_a_different_plcs_battery(make, inner_backend):
    other, _ = _built(BACKENDS[inner_backend])
    other.run(2)
    plc, call = _built(make)
    call.action = lambda: _set_battery(other, False)
    assert plc.step().scan_id == 1
    assert other.battery_present is False
    assert plc.battery_present is True


@pytest.mark.parametrize("debug", [False, True], ids=["scan_steps", "scan_steps_debug"])
def test_setting_the_battery_beside_a_paused_scan_stays_legal(debug):
    plc, _call = _built(BACKENDS["interpreted"])
    plc.run(2)
    gen = _paused_past_the_callable(plc, debug=debug)
    _set_battery(plc, False)
    assert plc.battery_present is False
    gen.close()
    plc.debug.abandon_scan()


# -- a run of no scans --


ZERO_LENGTH: dict[str, Callable[[Any], Any]] = {
    "run": lambda plc: plc.run(0),
    "run_for": lambda plc: plc.run_for(0),
}
zero_length = pytest.mark.parametrize("act", ZERO_LENGTH.values(), ids=ZERO_LENGTH.keys())


@zero_length
def test_a_run_of_no_scans_is_refused_inside_a_callable(make, act):
    """Both backends answer the call, not the number of scans it would have run."""
    plc, call = _built(make)
    plc.run(3)
    before = plc.current_state
    _refused_scan(plc, call, act)
    assert plc.current_state is before


@zero_length
def test_a_run_of_no_scans_outside_a_callable_is_a_no_op(make, act):
    plc, _call = _built(make)
    plc.run(3)
    before = plc.current_state
    assert act(plc) is before
    assert plc.current_state is before
    assert plc.mode == "run"


# -- what a callable may still do to its PLC --


def test_start_in_run_mode_stays_a_no_op_inside_a_callable(make):
    """start() doesn't scan, and in RUN it does nothing."""
    plc, call = _built(make)
    plain, _ = _built(make)
    plc.run(3)
    plain.run(3)
    call.action = plc.start
    plc.step()
    plain.step()
    assert _tags(plc) == _tags(plain)


def test_a_commit_callback_may_step_the_plc():
    """A callback runs after the commit, with no callable running, so it is no scan inside a scan."""
    plc, _call = _built(BACKENDS["interpreted"])
    plain, _ = _built(BACKENDS["interpreted"])
    stepped = []

    def steps_once(_state: Any) -> None:
        if not stepped:
            stepped.append(plc.step().scan_id)

    plc.when(Cnt == 2).do(steps_once)
    plc.run(2)
    plain.run(3)
    assert stepped == [3]
    assert plc.current_state.scan_id == 3
    assert _tags(plc) == _tags(plain)


# -- the marker clears --


def test_marker_clears_when_the_callable_raises(make):
    plc, call = _built(make)
    plain, _ = _built(make)

    def fails() -> None:
        raise ValueError("from the callable")

    call.action = fails
    with pytest.raises(ValueError, match="from the callable"):
        plc.step()
    plc.step()
    plain.step()
    assert _tags(plc) == _tags(plain)


def test_marker_clears_after_a_refusal(make):
    plc, call = _built(make)
    plain, _ = _built(make)
    _refused_scan(plc, call, SCANNERS["run"])
    plc.run(2)
    plain.run(2)
    assert _tags(plc) == _tags(plain)


def test_marker_clears_on_a_base_exception(make):
    plc, call = _built(make)

    def interrupted() -> None:
        raise KeyboardInterrupt

    call.action = interrupted
    with pytest.raises(KeyboardInterrupt):
        plc.step()
    assert plc.step().scan_id == 1


# -- other PLCs stay legal --


@pytest.mark.parametrize("inner_backend", sorted(BACKENDS))
def test_callable_may_step_a_different_plc(make, inner_backend):
    other, _ = _built(BACKENDS[inner_backend])
    plc, call = _built(make)
    call.action = other.step
    plc.step()
    call.action = lambda: other.run(2)
    plc.step()
    assert plc.current_state.scan_id == 2
    assert other.current_state.scan_id == 3
    assert other.current_state.tags["Cnt"] == 3


def test_callable_may_step_another_plc_of_the_same_program():
    """The marker is on the PLC, not on the instruction or the program they share."""
    call = Call()
    program = _program(call)
    one, other = PLC(program, dt=0.01), PLC(program, dt=0.01)
    call.action = other.step
    one.step()
    assert one.current_state.scan_id == 1
    assert other.current_state.scan_id == 1


def test_callable_may_step_another_compiled_plc_of_the_same_kernel():
    call = Call()
    program = _program(call)
    kernel = compile_kernel(program)
    one = CompiledPLC(program, dt=0.01, compiled=kernel)
    other = CompiledPLC(program, dt=0.01, compiled=kernel)
    call.action = other.step
    one.step()
    assert one.current_state.scan_id == 1
    assert other.current_state.scan_id == 1


def test_callable_may_step_a_fork_of_its_plc():
    call = Call()
    plc = PLC(_program(call), dt=0.01)
    plc.run(2)
    forks = []

    def forks_and_steps() -> None:
        fork = plc.fork()
        fork.step()
        forks.append(fork)

    call.action = forks_and_steps
    plc.step()
    assert plc.current_state.scan_id == 3
    assert forks[0].current_state.scan_id == 3


def test_a_scan_coming_back_through_another_plc_is_refused(make):
    plc, call = _built(make)
    other, other_call = _built(make)
    call.action = other.step
    other_call.action = plc.step
    with pytest.raises(RuntimeError, match=REFUSAL):
        plc.step()
    assert plc.step().scan_id == 1
    assert other.step().scan_id == 1


# -- a paused scan stays out of it --


def _paused_past_the_callable(plc: PLC, *, debug: bool) -> Any:
    gen = plc.debug.scan_steps_debug() if debug else plc.debug.scan_steps()
    if debug:
        while next(gen).rung_index < 2:
            pass
    else:
        for _ in range(2):
            next(gen)
    return gen


@pytest.mark.parametrize("debug", [False, True], ids=["scan_steps", "scan_steps_debug"])
def test_stepping_beside_a_paused_scan_stays_legal(debug):
    plc, call = _built(BACKENDS["interpreted"])
    plain, _ = _built(BACKENDS["interpreted"])
    plc.run(2)
    plain.run(2)

    gen = _paused_past_the_callable(plc, debug=debug)
    assert plc.step().scan_id == 3
    plain.step()
    assert _tags(plc) == _tags(plain)

    # And the paused scan, begun before that step, still finishes on the scan it began from.
    for _ in gen:
        pass
    assert plc.current_state.scan_id == 3
    assert _tags(plc) == _tags(plain)
    assert call.action is None


@pytest.mark.parametrize("debug", [False, True], ids=["scan_steps", "scan_steps_debug"])
def test_abandoning_a_paused_scan_leaves_the_plc_free(debug):
    plc, _call = _built(BACKENDS["interpreted"])
    plain, _ = _built(BACKENDS["interpreted"])

    gen = _paused_past_the_callable(plc, debug=debug)
    gen.close()
    plc.debug.abandon_scan()
    for runner in (plc, plain):
        runner.step()
        assert runner.run(2).scan_id == 3
    assert _tags(plc) == _tags(plain)


@pytest.mark.parametrize("debug", [False, True], ids=["scan_steps", "scan_steps_debug"])
def test_a_paused_scans_own_callable_is_refused(debug):
    """A paused scan's own callable hits the refusal as any scan's does."""
    plc, call = _built(BACKENDS["interpreted"])
    plc.run(3)
    before = plc.current_state
    call.action = plc.step
    gen = plc.debug.scan_steps_debug() if debug else plc.debug.scan_steps()
    with pytest.raises(RuntimeError, match=REFUSAL):
        for _ in gen:
            pass
    assert plc.current_state is before
    plc.debug.abandon_scan()
    assert plc.step().scan_id == before.scan_id + 1


@pytest.mark.parametrize("debug", [False, True], ids=["scan_steps", "scan_steps_debug"])
def test_finishing_a_paused_scan_from_inside_a_callable_is_refused(debug):
    plc, call = _built(BACKENDS["interpreted"])
    plc.run(3)
    before = plc.current_state
    gen = plc.debug.scan_steps_debug() if debug else plc.debug.scan_steps()
    next(gen)

    def finishes() -> None:
        for _ in gen:
            pass

    call.action = finishes
    with pytest.raises(RuntimeError, match=REFUSAL):
        plc.step()
    assert plc.current_state is before
    assert plc.step().scan_id == before.scan_id + 1


def test_committing_a_prepared_scan_from_inside_a_callable_is_refused():
    plc, call = _built(BACKENDS["interpreted"])
    before = plc.current_state
    ctx, dt = plc.debug.prepare_scan()
    call.action = lambda: plc.debug.commit_scan(ctx, dt)
    with pytest.raises(RuntimeError, match=REFUSAL):
        plc.step()
    assert plc.current_state is before


# -- long runs --


def _self_stepper(fires_at: int) -> tuple[PLC, Any]:
    """A plain closure over its PLC, firing on a scan past the interpreted prefix."""

    def inner(c: int) -> dict[str, int]:
        if c == fires_at:
            plc.step()
        return {"seen": c}

    with Program(strict=False) as program:
        with Rung():
            run_function(inner, ins={"c": Cnt}, outs={"seen": Seen})
        with Rung():
            calc(Cnt + 1, Cnt)
    plc = PLC(program, dt=0.01)
    return plc, inner


@pytest.mark.parametrize(
    "long_run",
    [
        lambda plc: plc.run_for(1.0),
        lambda plc: plc.run_until(NEVER, max_cycles=100),
    ],
    ids=["run_for", "run_until"],
)
def test_a_long_run_stays_interpreted_and_raises_on_the_scan_stepping_does(monkeypatch, long_run):
    """A callable that can reach its PLC can't pass the gate, so no kernel or loop runs it."""
    monkeypatch.setattr(compiled_run, "INTERPRETED_PREFIX", 2)
    plc, inner = _self_stepper(fires_at=20)
    assert not _may_be_called_again(inner)
    assert plc._compiled_replay_supported_kernel() is None

    loop_scans = scan_trace.TOTALS["loop scans"]
    with pytest.raises(RuntimeError, match=REFUSAL):
        long_run(plc)
    assert plc.current_state.scan_id == 20
    assert plc.current_state.tags["Cnt"] == 20
    assert scan_trace.TOTALS["loop scans"] == loop_scans


def test_a_global_reference_to_the_plc_cannot_pass_the_gate_either():
    assert not _may_be_called_again(_steps_module_plc)


_module_plc: PLC | None = None


def _steps_module_plc(c: int) -> dict[str, int]:
    assert _module_plc is not None
    _module_plc.step()
    return {"seen": c}
