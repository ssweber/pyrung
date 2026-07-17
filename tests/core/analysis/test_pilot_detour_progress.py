"""PILOT gates for target-relative progress across channel detours."""

from __future__ import annotations

from pyrung import (
    PLC,
    Bool,
    Int,
    Program,
    Timer,
    calc,
    copy,
    on_delay,
    rise,
    rung,
)


def _knock_three_times_program():
    """A channel revisit whose retained counter records real causal progress.

    Each knock bumps the machine away from the door.  It walks back by itself,
    and the third knock admits it.  Channel-only history sees the same
    ``AT_DOOR -> AWAY -> AT_DOOR`` loop twice; the meaningful states are the
    joint ``(channel, knock_count)`` visits.
    """

    at_door, away, inside = 0, 1, 2
    knock = Bool("Knock_Btn", external=True)
    channel = Int(
        "Knock_Channel",
        default=at_door,
        choices={at_door: "AtDoor", away: "Away", inside: "Inside"},
    )
    count = Int("Knock_Count")
    away_tmr = Timer.clone("Knock_AwayTmr")

    with Program() as logic:
        with rung(channel == at_door, rise(knock)):
            calc(count + 1, count)
            copy(away, channel)

        with rung(channel == away):
            on_delay(away_tmr, 30, "ms")

        with rung(channel == away, away_tmr.Done, count < 3):
            copy(at_door, channel)

        with rung(channel == away, away_tmr.Done, count >= 3):
            copy(inside, channel)

    return logic, knock, channel, count, at_door, away, inside


def _pulse_knock(plc: PLC, knock: Bool) -> None:
    plc.patch({knock.name: True})
    plc.step()
    plc.patch({knock.name: False})


def test_knock_three_times_premise() -> None:
    """The program itself reaches Inside after three separate knock edges."""

    logic, knock, channel, count, at_door, away, inside = _knock_three_times_program()
    plc = PLC(logic, dt=0.010)
    plc.step()

    for expected in (1, 2, 3):
        assert plc.state.tags[channel.name] == at_door
        _pulse_knock(plc, knock)
        assert plc.state.tags[channel.name] == away
        assert plc.state.tags[count.name] == expected
        plc.run_until(
            lambda state: state.tags[channel.name] != away,
            max_cycles=20,
        )
        # Give the off-rung scan a chance to clear the timer's Done state before
        # the next edge.  This is timer housekeeping, not a fourth action.
        if expected < 3:
            assert plc.state.tags[channel.name] == at_door
            plc.step()

    assert plc.state.tags[channel.name] == inside
    assert plc.state.tags[count.name] == 3


def test_pilot_reaches_counter_gated_target_across_channel_revisits() -> None:
    """PILOT knocks three times despite leaving and revisiting its channel.

    The search key threshold-abstracts ``Knock_Count`` — the joint visits
    ``(AtDoor, 1)`` and ``(AtDoor, 2)`` alias — so the SPIN / CYCLE / LATERAL
    gates consult the target-relative progress gauge (gauge.py): a
    knock that advanced the event-earned ordinal is real work, not a revisit.
    """

    logic, _knock, channel, count, _at_door, _away, inside = _knock_three_times_program()
    plc = PLC(logic, dt=0.010)

    path = plc.how(channel == inside, max_scans=3000)

    assert path.reachable, path.reason
    final = path.replay().state.tags
    assert final[channel.name] == inside
    assert final[count.name] == 3


def test_continuation_evidence_accepts_departure_already_at_goal() -> None:
    """A terminal departure has valid zero-edge continuation evidence."""
    from pyrung.core.analysis.pilot.navigation_evidence import (
        NavigationEvidence,
        Reachable,
    )

    status = NavigationEvidence.channel_continuation(
        (),
        "State",
        17,
        (6, 17),
        edge_allowed=lambda _edge: True,
    )
    assert isinstance(status, Reachable)
