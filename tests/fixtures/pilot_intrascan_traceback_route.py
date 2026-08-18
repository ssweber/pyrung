"""Neutral runnable route for recursive occurrence-local traceback.

The source World is prepared at ``Step=98`` after one unrelated entry scan.
During the attempted consumer scan, ordinary main-program writers carry that
value through ``22`` to ``40`` before the later subroutine reads it.  Reset can
write ``10`` early, but that value follows the same overwrite path.  Preventing
the overwrite depends on a program-owned mode value with a real earlier writer,
so WorkingTheory must investigate two backward hops without inventing a
mid-scan production patch.
"""

from pyrung import PLC, Bool, Int, Or, Program, Rung, call, copy, out, subroutine
from pyrung.core.state import SystemState

Step = Int("TracebackRouteStep", default=40)
RouteMode = Int("TracebackRouteMode", default=100)

Link = Bool("TracebackRouteLink", external=True)
Reset = Bool("TracebackRouteReset", external=True)
ModeReset = Bool("TracebackRouteModeReset", external=True)
Completed = Bool("TracebackRouteCompleted")


@subroutine("traceback_route_consumer")
def traceback_route_consumer() -> None:
    with Rung(~Link, Step <= 20):
        copy(98, Step, oneshot=True)

    with Rung(Link, Step == 98):
        copy(10, Step, oneshot=True)
        out(Completed)


with Program(strict=False) as logic:
    # Real earlier writer for the program-owned guard exposed after Reset.
    with Rung(ModeReset):
        copy(0, RouteMode, oneshot=True)

    # Real earlier writer for the <=20 producer guard.
    with Rung(Reset, Step >= 90):
        copy(10, Step, oneshot=True)

    # The two intervening main-program writes model ClickNick's 10 -> 22 -> 40
    # conductivity before ErrorHandling sees the sequence channel.
    with Rung(Or(Step == 98, Step == 10), RouteMode == 100):
        copy(22, Step)

    with Rung(Step == 22):
        copy(40, Step)

    with Rung():
        call(traceback_route_consumer)


def watch_plc(*, dt: float) -> PLC:
    """Reproduce an already-stepped console boundary with ``Step`` at 98."""

    source = SystemState(scan_id=1, timestamp=dt).with_tags(
        {
            Step.name: 98,
            RouteMode.name: 100,
        }
    )
    return PLC(logic, initial_state=source, dt=dt)
