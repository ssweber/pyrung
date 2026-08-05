"""Successive scalar deadlines followed by a later same-source guard.

One selected command enters a multi-scan program-owned sequence. The first two
stages each have a zero-preset completion hazard. Once both scalar requirements
are repaired, a later target write is exposed to a final same-scan guard.

The adjustable variant can repair that guard and reach the target. The
program-owned variant cannot steer its guard and must remain unreached.
"""

from dataclasses import dataclass

from pyrung import PLC, Bool, Int, Program, Timer, copy, on_delay, out, rung

SOURCE = 0
ENTERED = 1
QUALIFIED = 2
ADVANCING = 3
TARGET = 4
FIRST_DIVERSION = 8
SECOND_DIVERSION = 9
GUARDED_DIVERSION = 10


@dataclass(frozen=True)
class Scenario:
    logic: Program
    SequenceState: Int
    StartCommand: Bool
    FirstPresetMs: Int
    SecondPresetMs: Int
    FinalGuard: Bool


def _scenario(prefix: str, *, program_owned_guard: bool) -> Scenario:
    state = Int(f"{prefix}SequenceState", default=SOURCE)
    start = Bool(f"{prefix}StartCommand", external=True)
    first_preset = Int(f"{prefix}FirstPresetMs")
    second_preset = Int(f"{prefix}SecondPresetMs")
    final_guard = Bool(f"{prefix}FinalGuard", external=not program_owned_guard)
    first_completion = Timer.clone(f"{prefix}FirstCompletion")
    second_completion = Timer.clone(f"{prefix}SecondCompletion")

    # The fixture factory selects between two declarative guard owners; the
    # generated rung bodies themselves contain no runtime Python branching.
    with Program(strict=False) as logic:
        if program_owned_guard:
            with rung():
                out(final_guard)

        # Each consumer is above its producer, making the scalar deadlines and
        # final guard observable on three successive scans.
        with rung(state == ADVANCING):
            copy(TARGET, state, oneshot=True)

        final_clobber = final_guard if program_owned_guard else ~final_guard
        with rung(state == TARGET, final_clobber):
            copy(GUARDED_DIVERSION, state)

        with rung(state == QUALIFIED):
            copy(ADVANCING, state, oneshot=True)
            on_delay(second_completion, second_preset)

        with rung(second_completion.Done):
            copy(SECOND_DIVERSION, state, oneshot=True)

        with rung(start, state == SOURCE):
            copy(ENTERED, state, oneshot=True)

        with rung(state == ENTERED):
            copy(QUALIFIED, state, oneshot=True)
            on_delay(first_completion, first_preset)

        with rung(first_completion.Done):
            copy(FIRST_DIVERSION, state, oneshot=True)

    return Scenario(
        logic=logic,
        SequenceState=state,
        StartCommand=start,
        FirstPresetMs=first_preset,
        SecondPresetMs=second_preset,
        FinalGuard=final_guard,
    )


adjustable = _scenario("OrderedRequirementAdjustable", program_owned_guard=False)
program_owned = _scenario("OrderedRequirementOwned", program_owned_guard=True)


def new_plc(scenario: Scenario) -> PLC:
    return PLC(scenario.logic, dt=0.010)
