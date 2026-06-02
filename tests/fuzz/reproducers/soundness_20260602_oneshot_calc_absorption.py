"""Reproducer: threshold absorption unsound for oneshot calc accumulators.

Oneshot calc(Step + 1, Step, oneshot=True) increments Step once per rising
edge of the rung condition. Threshold absorption absorbs Step but falsely
concludes the threshold (Step == 3) can never be crossed — never(Done)
returns Proven when Done is actually reachable via Trigger on/off cycling.
"""

from pyrung.core import Bool, Int, Program, Rung, calc, latch
from pyrung.core.analysis.prove import Counterexample, Intractable, Proven, always


def test_reproducer():
    Trigger = Bool("Trigger", external=True)
    Step = Int("Step")
    Done = Bool("Done")
    with Program(strict=False) as logic:
        with Rung(Trigger):
            calc(Step + 1, Step, oneshot=True)
        with Rung(Step == 3):
            latch(Done)

    optimized = always(logic, ~Done, max_states=10_000, depth_budget=20)
    unoptimized = always(logic, ~Done, max_states=10_000, depth_budget=20,
                         _skip_optimizations=True)

    # optimized=Proven (wrong), unoptimized=Counterexample (correct)
    if isinstance(optimized, Intractable) or isinstance(unoptimized, Intractable):
        return
    assert type(optimized) is type(unoptimized), (
        f"optimized={type(optimized).__name__}, unoptimized={type(unoptimized).__name__}"
    )
