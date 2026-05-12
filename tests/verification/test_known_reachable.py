"""Known-answer reachability oracles for BFS state exploration."""

from __future__ import annotations

import pytest

from pyrung.core import (
    Bool,
    Counter,
    Int,
    Or,
    Program,
    Rung,
    Timer,
    copy,
    count_down,
    count_up,
    latch,
    on_delay,
    out,
    reset,
    rise,
)
from pyrung.core.analysis.prove import Intractable, reachable_states

pytestmark = pytest.mark.known_answer


def _state(**values: object) -> frozenset[tuple[str, object]]:
    return frozenset(values.items())


def _assert_exact_reachable(
    logic: Program,
    *,
    project: list[str],
    expected: frozenset[frozenset[tuple[str, object]]],
    depth_budget: int,
) -> None:
    actual = reachable_states(logic, project=project, depth_budget=depth_budget)
    assert not isinstance(actual, Intractable)
    assert actual == expected


def test_known_reachable_and_gate() -> None:
    a = Bool("A", external=True)
    b = Bool("B", external=True)
    q = Bool("Q")

    with Program(strict=False) as logic:
        with Rung(a, b):
            out(q)

    # No retained state: the reachable key is just the ND input cross-product.
    # Q is combinational and reconstructable from A/B, so the four A/B rows are
    # the whole reachable set.
    _assert_exact_reachable(
        logic,
        project=["A", "B"],
        expected=frozenset(
            {
                _state(A=False, B=False),
                _state(A=False, B=True),
                _state(A=True, B=False),
                _state(A=True, B=True),
            }
        ),
        depth_budget=5,
    )
    _assert_exact_reachable(
        logic,
        project=["A", "B", "Q"],
        expected=frozenset(
            {
                _state(A=False, B=False, Q=False),
                _state(A=False, B=True, Q=False),
                _state(A=True, B=False, Q=False),
                _state(A=True, B=True, Q=True),
            }
        ),
        depth_budget=5,
    )


def test_known_reachable_sr_latch() -> None:
    set_ = Bool("Set", external=True)
    reset_ = Bool("Reset", external=True)
    latch_bit = Bool("Latch")

    with Program(strict=False) as logic:
        with Rung(set_):
            latch(latch_bit)
        with Rung(reset_):
            reset(latch_bit)

    # From Latch=False:
    # - Set=0 Reset=0 holds False
    # - Set=1 Reset=0 sets True
    # - Set=0 Reset=1 resets False
    # - Set=1 Reset=1 ends False because the reset rung runs after the set rung
    #
    # Once Latch=True is reached, Set=0 Reset=0 holds True, and any reset path
    # returns to False. There is no reachable (Set=0, Reset=1, Latch=True) or
    # (Set=1, Reset=1, Latch=True) state.
    _assert_exact_reachable(
        logic,
        project=["Set", "Reset", "Latch"],
        expected=frozenset(
            {
                _state(Set=False, Reset=False, Latch=False),
                _state(Set=True, Reset=False, Latch=True),
                _state(Set=False, Reset=True, Latch=False),
                _state(Set=True, Reset=True, Latch=False),
                _state(Set=False, Reset=False, Latch=True),
            }
        ),
        depth_budget=6,
    )


def test_known_reachable_one_shot_rising() -> None:
    trigger = Bool("Trigger", external=True)
    pulse = Bool("Pulse")

    with Program(strict=False) as logic:
        with Rung(rise(trigger)):
            out(pulse)

    # The hidden state is Trigger's previous-scan value. reachable_states()
    # does not project that memory directly, so we use the visible witness:
    # with Trigger=True, both Pulse=True (prev=False) and Pulse=False
    # (prev=True) must be reachable. Trigger=False always gives Pulse=False.
    _assert_exact_reachable(
        logic,
        project=["Trigger", "Pulse"],
        expected=frozenset(
            {
                _state(Trigger=False, Pulse=False),
                _state(Trigger=True, Pulse=True),
                _state(Trigger=True, Pulse=False),
            }
        ),
        depth_budget=6,
    )


def test_known_reachable_ton_phase_graph() -> None:
    enable = Bool("Enable", external=True)
    timer = Timer.clone("T")
    phase = Int("Phase")

    with Program(strict=False) as logic:
        with Rung(enable):
            on_delay(timer, preset=30)
        with Rung():
            copy(0, phase)
        with Rung(timer.Acc > 0):
            copy(1, phase)
        with Rung(timer.Done):
            copy(2, phase)

    # TON has three abstract progress regions here:
    # - Phase 0: disabled / zero
    # - Phase 1: enabled and counting but not done
    # - Phase 2: enabled and done
    #
    # Because this is TON, dropping Enable resets immediately to Phase 0, so
    # there is no reachable (Enable=False, Phase=1) or (Enable=False, Phase=2).
    _assert_exact_reachable(
        logic,
        project=["Enable", "Phase"],
        expected=frozenset(
            {
                _state(Enable=False, Phase=0),
                _state(Enable=True, Phase=1),
                _state(Enable=True, Phase=2),
            }
        ),
        depth_budget=8,
    )


def test_known_reachable_motor_start_stop_estop() -> None:
    start = Bool("Start", external=True)
    stop = Bool("Stop", external=True)
    estop = Bool("EStop", external=True)
    running = Bool("Running")

    with Program(strict=False) as logic:
        with Rung(start, ~stop, ~estop):
            latch(running)
        with Rung(Or(stop, estop)):
            reset(running)

    # Running starts False, can be latched True by Start, and is cleared by
    # either Stop or EStop. Both running phases are reachable.
    _assert_exact_reachable(
        logic,
        project=["Running"],
        expected=frozenset({_state(Running=False), _state(Running=True)}),
        depth_budget=8,
    )


def test_known_reachable_mutual_exclusion_interlock() -> None:
    start_a = Bool("StartA", external=True)
    start_b = Bool("StartB", external=True)
    motor_a = Bool("MotorA")
    motor_b = Bool("MotorB")

    with Program(strict=False) as logic:
        with Rung(start_a, ~start_b, ~motor_b):
            latch(motor_a)
        with Rung(start_b, ~start_a, ~motor_a):
            latch(motor_b)

    # Idle can launch A or B, but never both:
    # - simultaneous starts from idle are blocked by ~StartOther
    # - once one motor is latched, the other's rung sees ~MotorOther=False
    _assert_exact_reachable(
        logic,
        project=["MotorA", "MotorB"],
        expected=frozenset(
            {
                _state(MotorA=False, MotorB=False),
                _state(MotorA=True, MotorB=False),
                _state(MotorA=False, MotorB=True),
            }
        ),
        depth_budget=8,
    )


def test_known_reachable_counter_with_done_bit() -> None:
    count_pulse = Bool("CountPulse", external=True)
    never_reset = Bool("NeverReset")
    counter = Counter.clone("C")
    count_phase = Int("CountPhase")

    with Program(strict=False) as logic:
        with Rung(~counter.Done, rise(count_pulse)):
            count_up(counter, preset=3).reset(never_reset)

        # These observers force the verifier to retain the 0/1/2/3 progress
        # regions instead of collapsing all "Done=False" counts together.
        with Rung():
            copy(0, count_phase)
        with Rung(counter.Acc >= 1):
            copy(1, count_phase)
        with Rung(counter.Acc >= 2):
            copy(2, count_phase)
        with Rung(counter.Done):
            copy(3, count_phase)

    # CountPhase is a visible 0/1/2/3 proxy for the accumulator bands:
    # 0 -> Acc=0, 1 -> Acc=1, 2 -> Acc=2, 3 -> Acc>=3 (Done=True here because
    # further counts are gated off). We project that proxy because the verifier
    # is allowed to abstract raw C_Acc unless an observed threshold keeps it.
    #
    # Walk from (prev=False, Acc=0):
    # (F,0) --pulse=T--> (T,1) --pulse=F--> (F,1)
    # (F,1) --pulse=T--> (T,2) --pulse=F--> (F,2)
    # (F,2) --pulse=T--> (T,3) --pulse=F--> (F,3)
    #
    # There is no (T,0): the first high pulse is itself a rising edge and
    # increments immediately. There are no Acc>3 states because ~C.Done gates
    # further counts once preset 3 is reached.
    _assert_exact_reachable(
        logic,
        project=["CountPulse", "CountPhase", "C_Done"],
        expected=frozenset(
            {
                _state(CountPulse=False, CountPhase=0, C_Done=False),
                _state(CountPulse=True, CountPhase=1, C_Done=False),
                _state(CountPulse=False, CountPhase=1, C_Done=False),
                _state(CountPulse=True, CountPhase=2, C_Done=False),
                _state(CountPulse=False, CountPhase=2, C_Done=False),
                _state(CountPulse=True, CountPhase=3, C_Done=True),
                _state(CountPulse=False, CountPhase=3, C_Done=True),
            }
        ),
        depth_budget=20,
    )


def test_known_reachable_ton_with_acc_truthy_condition() -> None:
    """Regression: truthy read of timer Acc must not drop Done from BFS."""
    enable = Bool("Enable", external=True)
    timer = Timer.clone("T")
    active = Bool("Active")

    with Program(strict=False) as logic:
        with Rung(enable):
            on_delay(timer, preset=100)
        with Rung(timer.Acc):
            out(active)

    _assert_exact_reachable(
        logic,
        project=["Active", "T_Done"],
        expected=frozenset(
            {
                _state(Active=False, T_Done=False),
                _state(Active=True, T_Done=False),
                _state(Active=True, T_Done=True),
            }
        ),
        depth_budget=15,
    )


@pytest.mark.xfail(reason="count_down BFS event scheduling does not yet reach Done=True")
def test_known_reachable_countdown_with_acc_truthy_condition() -> None:
    """Regression: truthy read of counter Acc must not drop Done (descending)."""
    pulse = Bool("Pulse", external=True)
    counter = Counter.clone("C")
    active = Bool("Active")

    never_reset = Bool("NeverReset")

    with Program(strict=False) as logic:
        with Rung(rise(pulse)):
            count_down(counter, preset=3).reset(never_reset)
        with Rung(counter.Acc):
            out(active)

    result = reachable_states(logic, project=["Active", "C_Done"], depth_budget=15)
    assert not isinstance(result, Intractable)
    assert _state(Active=True, C_Done=True) in result
