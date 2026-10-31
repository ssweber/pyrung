"""Drums: the ``drum`` group (subroutine ``Conf_Drum``).

A drum's outputs, events, completion flag and control inputs are C bits and its
accumulator a TD register, and no slot field is either, so each case uses raw
registers it owns: C1901..C2000, C801..C850 and TD401..TD500
(``memory.OWNED``), ten C bits per case (cases 11 on take theirs from
C801..C850).  The current step is a slot field (DS), ``Result1`` in every
case; a jump target is a slot field too (CLICK takes it from a DS).  Each
drum's reset input is ``Cmd == 0``, so the reset puts it back on step 1 with
its time cleared.  A case that also resets mid-run puts an ``Or`` of
``Cmd == 0`` and its own reset scan on the reset input, with the jump and jog
pins after it (its second branch row sits between the pin rows).

The event-drum cases count their own scans (``scans.py``): a C bit driven by a
rung keyed on the scan number is the event, the jog or the jump, and rungs
below the drum record the scan each step is entered, the step on a given scan,
and when an output or the completion flag was last on.  ``Fired`` goes up once
the record is final, so every expectation is an exact int.  The time drum case
paces itself on its accumulator instead (each control fires once the step has
timed 100 ms), so it runs on a CLICK whose scan is far shorter than 10 ms.

The expectations are what a CLICK does where hardware round 1 (2026-10-29)
answered, and the adopted answer of an open probe otherwise.  A CLICK event
drum (drum/model_a.py in the round-1 notes reproduces every reading): jump and
jog act on their rising edges even while the drum is paused; reset is a level
that wins, taking the drum to step 1 with its flag off and ALL its outputs off,
and drops a jump or jog on its scan; jump beats jog on one scan; a paused drum
does not write its outputs; at completion all its outputs go off until a jog
(hardware round 2: a jump moves the step but leaves them off).  pyrung matches
each since step DRUM; what pyrung read before it is noted below as "pyrung
before DRUM".
What each case tells apart:

- ``te_acc_counts_while_waiting``, ``te_level_event_already_on``: the open
  probes D1 (does a Time & Event step's elapsed time keep counting past its
  duration while it waits for the event?) and D3 (is the event a level: does an
  event already on when the time is up end the step?).
- ``events_are_edges``: an event drum steps on an event's rising edge, and a
  step entered with its event already on waits for the event to go off and on
  again, whether it was entered by a jog or by an event.  A drum that steps on
  event levels, or that counts an event already on at step entry as an edge,
  or that compares the new step's event with the old step's event from the
  scan before, enters step 3 on scan 5 (Result3 = 5) and completes on scan 7
  (Result4 = 7).
- ``jump_jog_edge_triggered``: jump and jog act on their rising edges.  A level
  jog walks the drum a step a scan (step 3 entered on scan 3, Result3 = 3); a
  level jump keeps pulling the drum back to step 3 (Result1 = 3).
- ``jump_jog_keep_completion``: only reset clears the completion flag.  If a
  jump clears it, it is on for 2 scans, last on scan 5 (Result2 = 2,
  Result3 = 5); if a jog does, 3 scans, last on scan 6.
- ``reset_no_stale_outputs``: a drum on step 2 of 3 (never complete) reset
  on scans 5 and 6.  A reset that turns every output off keeps step 1's
  output off until the reset ends, on from scan 7 (Result2 = 7); pyrung
  before DRUM wrote step 1's outputs on the reset scan (Result2 = 5).  A
  drum that writes its outputs before it handles the reset leaves step 2's
  output on for a stale scan (Result3 = 5).  Result4 = 1: the step on scan 5.
- ``reset_jump_jog_order``: reset wins, and jump beats jog: steps on scans 3
  and 5 read 1 (Result2 = 1, Result3 = 1), and jump with jog on scan 7 leaves
  step 3 (Result1 = 3).  pyrung before DRUM applied the reset, then the
  jump, then the jog, each from where the last left the step: 3, 2, 4.
- ``pause_holds_and_drops_jog``: retired, the drum it asked about acts on a
  paused jog (see ``paused_drum_jogs_and_resets``).
- ``paused_drum_jogs_and_resets``: paused on step 2 for scans 3..5, a jog on
  scan 4 still steps (the step on scan 4 reads 3, Result2), the paused drum
  leaves step 2's output on through scan 4 (Result3 = 4), a reset on scan 5
  turns every output off, and step 1's output comes on when the rung comes
  back, scan 6 (Result4).  pyrung before DRUM ignored the paused jog
  (Result2 = 2) and wrote step 1's output on the reset scan (Result4 = 5).
- ``completion_turns_outputs_off``: a 2-step drum completed on scan 3 turns
  its outputs off, so step 2's output is on for scan 2 only, and on scan 8
  again after a jog (Result2 = 2 scans).  After completion a jump to step 1
  on scan 6 moves the step but leaves step 1's output off (Result3 = 0); the
  jog on scan 8 brings step 2's back (ErrorCode = 8) (hardware round 2, 2026-10-30: the adopted
  answer was Result3 = 6, the jump turning step 1's output on).  pyrung
  before DRUM left step 2's output on until the jump (Result2 = 5).
- ``paused_drum_jumps`` (answered as adopted): paused on step 1 for scans 3..5, a jump to
  step 3 on scan 4.  A jump taken while paused (adopted) reads step 3 on scan
  4 (Result2) and at the end (Result1); step 1's output stays on until the
  rung comes back (last on scan 5, Result3) and step 3's comes on then
  (Result4 = 6).  A paused drum that ignores the jump reads 1, 1, 8, 0.
- ``reset_swallows_held_jog`` (answered as adopted): a jog that rises on a reset scan (4)
  and is held to scan 6.  Reset drops it and its edge is spent (adopted): the
  drum stays on step 1 (Result1 = 1) and never enters step 2 (Result2 = 0).
  An edge kept for the scan after the reset enters step 2 on scan 5; pyrung
  before DRUM applied the jog after the reset on scan 4 (Result2 = 4).
- ``invalid_step_corrects_to_1``: an enabled drum whose step register is out of
  range goes to step 1 and writes step 1's outputs; a paused one leaves the
  register alone.  Clamping to the last step reads Result3 = 3, wrapping
  Result3 = 2, ignoring the bad step Result3 = 5; correcting while paused
  reads Result2 = 1.
- ``time_acc_clears_on_every_transition``: a time drum's accumulator is 0 on
  the scan of a jog, a jump, an advance and a reset.  A drum that keeps its
  time through a jog or jump reads 100 or more; one that clears it and then
  adds the scan's time reads a scan's worth (about 10 on the sim); one that
  carries a step's overshoot into the next step reads 5 in-process on the
  advance (Result4).
- ``pins_read_before_step_fix`` (probe, box atomicity): a box reads its
  enable and every pin at one moment, before it executes.  A step register
  staged out of range (5 of 2) on scan 3 is the shared condition (Result1 > 2)
  of the drum's rung and its ``.jog()`` pin (the shared contact copied into the
  pin's row: the exporter's rail form), with a jog pulse on scan 3.  The drum
  corrects the step to 1 as it runs.  Pins read before that write (adopted,
  pyrung's model): the jog sees the shared condition on, jogs the corrected
  step to 2 (Result1 = 2) and step 2's output comes on at scan 3 (Result3 = 3,
  Result4 = 0).  A jog pin read after the correction sees step 1, the shared
  condition off, and no jog: Result1 = 1, Result3 = 0, step 1's output first on
  at scan 3 (Result4 = 3).  The pin drawn teeing off the enable wire is the
  other CLICK drawing of the same logic; pyrung's exporter always copies the
  contacts.
- ``pins_read_before_step_fix_tee``: the same question with the ``.reset()``
  and ``.jog()`` pins drawn teeing off the drum's input wire after the one
  shared contact (Result1 != 1), each with one contact of its own (Cmd == 0,
  C1980).  The shared condition is true while idle (step 0) and on the invalid
  step, so the reset pin can be a tee too.  Same readings as above.  pyrung
  runs the same rungs in every target; the CLICK export redraws the pins
  (``tee=``), and ``test_tee_twins.py`` imports the drawing and compares it
  with the rail drawing.  A CLICK that reads the two drawings differently is
  the answer.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, TeeSpec, assert_all_passed, case, run
from pyrung.core import (
    And,
    Or,
    Rung,
    calc,
    copy,
    event_drum,
    latch,
    out,
    reset,
    time_drum,
    time_event_drum,
)
from tests.conformance.memory import c, td
from tests.conformance.scans import WINDOW, count_scans, done, scans_for

SCANS = scans_for()


def entered(slot: Any, step: int, into: Any) -> None:
    """*into* = the scan the drum (``Result1``) first reads *step*.  Below the drum."""
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, slot.Result1 == step, into == 0):
        copy(slot.Scratch, into)


def at_scan(slot: Any, scan: int, value: Any, into: Any) -> None:
    """*into* = *value* as scan *scan* reaches this rung."""
    with Rung(slot.Cmd != 0, slot.Scratch == scan):
        copy(value, into)


def first_on_from(slot: Any, bit: Any, scan: int, into: Any) -> None:
    """*into* = the first scan from *scan* on that *bit* is on (0 if none)."""
    with Rung(slot.Cmd != 0, slot.Scratch >= scan, slot.Scratch <= WINDOW, bit, into == 0):
        copy(slot.Scratch, into)


def last_on(slot: Any, bit: Any, into: Any, count: Any = None) -> None:
    """*into* = the last scan *bit* is on; *count* = how many scans it is on."""
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, bit):
        copy(slot.Scratch, into)
        if count is not None:
            calc(count + 1, count)


def prove_te_acc_counts_while_waiting(slot):
    # Step 1 is up after 300 ms but its event (C1903) never comes.
    with Rung(slot.Cmd != 0):
        time_event_drum(
            outputs=[c[1901], c[1902]],
            presets=[300, None],
            events=[c[1903], c[1904]],
            unit="ms",
            pattern=[[1, 0], [0, 1]],
            current_step=slot.Result1,
            accumulator=td[401],
            completion_flag=c[1905],
        ).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, slot.Result1 == 1, td[401] >= 500):
        copy(1, slot.Result2, oneshot=True)
    with Rung(slot.Cmd != 0):
        copy(1, slot.Fired, oneshot=True)


def prove_te_level_event_already_on(slot):
    # The event is on before step 1's time is up, and stays on through the
    # event-only step 2.
    with Rung(slot.Cmd != 0):
        latch(c[1913])
    with Rung(slot.Cmd != 0):
        time_event_drum(
            outputs=[c[1911], c[1912]],
            presets=[300, None],
            events=[c[1913], c[1913]],
            unit="ms",
            pattern=[[1, 0], [0, 1]],
            current_step=slot.Result1,
            accumulator=td[411],
            completion_flag=c[1915],
        ).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, c[1915]):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0):
        copy(1, slot.Fired, oneshot=True)


def prove_events_are_edges(slot):
    # C1923, the event of steps 2 and 3, is on from scan 2 except scan 6.  A
    # jog on scan 4 enters step 2 with it already on.
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch >= 2, slot.Scratch != 6):
        out(c[1923])
    with Rung(slot.Cmd != 0, slot.Scratch == 4):
        out(c[1924])
    with Rung(slot.Cmd != 0):
        event_drum(
            outputs=[c[1921]],
            events=[c[1922], c[1923], c[1923]],
            pattern=[[1], [0], [1]],
            current_step=slot.Result1,
            completion_flag=c[1925],
        ).reset(slot.Cmd == 0).jog(c[1924])
    entered(slot, 2, slot.Result2)
    entered(slot, 3, slot.Result3)
    first_on_from(slot, c[1925], 1, slot.Result4)
    done(slot)


def prove_jump_jog_edge_triggered(slot):
    # Jog (C1933) is on for scans 2..4 and 7; jump to step 3 (C1934) from scan 5 on.
    count_scans(slot)
    with Rung(slot.Cmd != 0):
        copy(3, slot.Result4)
    with Rung(
        slot.Cmd != 0,
        slot.Scratch >= 2,
        slot.Scratch <= 7,
        slot.Scratch != 5,
        slot.Scratch != 6,
    ):
        out(c[1933])
    with Rung(slot.Cmd != 0, slot.Scratch >= 5):
        out(c[1934])
    with Rung(slot.Cmd != 0):
        event_drum(
            outputs=[c[1931]],
            events=[c[1932], c[1932], c[1932], c[1932]],
            pattern=[[1], [0], [1], [0]],
            current_step=slot.Result1,
            completion_flag=c[1935],
        ).reset(slot.Cmd == 0).jump(c[1934], step=slot.Result4).jog(c[1933])
    entered(slot, 2, slot.Result2)
    entered(slot, 3, slot.Result3)
    done(slot)


def prove_jump_jog_keep_completion(slot):
    # Events on scans 2, 3 and 4 complete the drum; jump to step 1 on scan 6,
    # jog on scan 7.
    count_scans(slot)
    with Rung(slot.Cmd != 0):
        copy(1, slot.Result4)
    with Rung(slot.Cmd != 0, slot.Scratch == 2):
        out(c[1942])
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        out(c[1943])
    with Rung(slot.Cmd != 0, slot.Scratch == 4):
        out(c[1944])
    with Rung(slot.Cmd != 0, slot.Scratch == 6):
        out(c[1946])
    with Rung(slot.Cmd != 0, slot.Scratch == 7):
        out(c[1947])
    with Rung(slot.Cmd != 0):
        event_drum(
            outputs=[c[1941]],
            events=[c[1942], c[1943], c[1944]],
            pattern=[[1], [0], [1]],
            current_step=slot.Result1,
            completion_flag=c[1945],
        ).reset(slot.Cmd == 0).jump(c[1946], step=slot.Result4).jog(c[1947])
    last_on(slot, c[1945], slot.Result3, count=slot.Result2)
    done(slot)


def prove_reset_no_stale_outputs(slot):
    # An event on scan 2 takes the drum to step 2 of 3 (not the last); reset
    # (C1956, also on while Cmd == 0) on scans 5 and 6.
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 2):
        out(c[1953])
    with Rung(Or(slot.Cmd == 0, slot.Scratch == 5, slot.Scratch == 6)):
        out(c[1956])
    with Rung(slot.Cmd != 0):
        event_drum(
            outputs=[c[1951], c[1952]],
            events=[c[1953], c[1954], c[1954]],
            pattern=[[1, 0], [0, 1], [1, 1]],
            current_step=slot.Result1,
            completion_flag=c[1955],
        ).reset(c[1956])
    at_scan(slot, 5, slot.Result1, slot.Result4)
    first_on_from(slot, c[1951], 3, slot.Result2)
    last_on(slot, c[1952], slot.Result3)
    done(slot)


def prove_reset_jump_jog_order(slot):
    # Jog alone on scan 2; reset (also on while Cmd == 0) and jump to step 3
    # on scan 3; reset and jog on scan 5; jump and jog on scan 7.
    count_scans(slot)
    with Rung(slot.Cmd != 0):
        copy(3, slot.Result4)
    with Rung(slot.Cmd != 0, Or(slot.Scratch == 3, slot.Scratch == 7)):
        out(c[1965])
    with Rung(slot.Cmd != 0, Or(slot.Scratch == 2, slot.Scratch == 5, slot.Scratch == 7)):
        out(c[1966])
    with Rung(slot.Cmd != 0):
        event_drum(
            outputs=[c[1961]],
            events=[c[1962], c[1962], c[1962], c[1962]],
            pattern=[[1], [0], [1], [0]],
            current_step=slot.Result1,
            completion_flag=c[1963],
        ).reset(Or(slot.Cmd == 0, slot.Scratch == 3, slot.Scratch == 5)).jump(
            c[1965], step=slot.Result4
        ).jog(c[1966])
    at_scan(slot, 3, slot.Result1, slot.Result2)
    at_scan(slot, 5, slot.Result1, slot.Result3)
    done(slot)


def prove_paused_drum_jogs_and_resets(slot):
    # Jog on scan 2 takes the drum to step 2.  Its rung is false (C835) on
    # scans 3..5; jog (C836) is on again for scans 4..6 and reset (also on
    # while Cmd == 0) on scan 5.
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3, slot.Scratch <= 5):
        out(c[835])
    with Rung(slot.Cmd != 0, Or(slot.Scratch == 2, slot.Scratch >= 4), slot.Scratch <= 6):
        out(c[836])
    with Rung(slot.Cmd != 0, ~c[835]):
        event_drum(
            outputs=[c[831], c[832]],
            events=[c[833], c[833], c[833]],
            pattern=[[1, 0], [0, 1], [0, 0]],
            current_step=slot.Result1,
            completion_flag=c[834],
        ).reset(Or(slot.Cmd == 0, slot.Scratch == 5)).jog(c[836])
    at_scan(slot, 4, slot.Result1, slot.Result2)
    last_on(slot, c[832], slot.Result3)
    first_on_from(slot, c[831], 3, slot.Result4)
    done(slot)


def prove_completion_turns_outputs_off(slot):
    # Events on scans 2 and 3 complete the 2-step drum; jump to step 1 (C806)
    # on scan 6, jog (C807) on scan 8.
    count_scans(slot)
    with Rung(slot.Cmd != 0):
        copy(1, slot.Result4)
    with Rung(slot.Cmd != 0, slot.Scratch == 2):
        out(c[803])
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        out(c[804])
    with Rung(slot.Cmd != 0, slot.Scratch == 6):
        out(c[806])
    with Rung(slot.Cmd != 0, slot.Scratch == 8):
        out(c[807])
    with Rung(slot.Cmd != 0):
        event_drum(
            outputs=[c[801], c[802]],
            events=[c[803], c[804]],
            pattern=[[1, 0], [0, 1]],
            current_step=slot.Result1,
            completion_flag=c[805],
        ).reset(slot.Cmd == 0).jump(c[806], step=slot.Result4).jog(c[807])
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, c[802]):
        calc(slot.Result2 + 1, slot.Result2)
    first_on_from(slot, c[801], 3, slot.Result3)
    first_on_from(slot, c[802], 7, slot.ErrorCode)
    done(slot)


def prove_paused_drum_jumps(slot):
    # The drum's rung is false (C814) on scans 3..5; a jump (C815) to step 3
    # (ErrorCode) on scan 4.  Nothing drives the event, C813.
    count_scans(slot)
    with Rung(slot.Cmd != 0):
        copy(3, slot.ErrorCode)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3, slot.Scratch <= 5):
        out(c[814])
    with Rung(slot.Cmd != 0, slot.Scratch == 4):
        out(c[815])
    with Rung(slot.Cmd != 0, ~c[814]):
        event_drum(
            outputs=[c[811], c[812]],
            events=[c[813], c[813], c[813]],
            pattern=[[1, 0], [0, 0], [0, 1]],
            current_step=slot.Result1,
            completion_flag=c[816],
        ).reset(slot.Cmd == 0).jump(c[815], step=slot.ErrorCode)
    at_scan(slot, 4, slot.Result1, slot.Result2)
    last_on(slot, c[811], slot.Result3)
    first_on_from(slot, c[812], 1, slot.Result4)
    done(slot)


def prove_reset_swallows_held_jog(slot):
    # Jog (C826) on scans 4..6; reset (also on while Cmd == 0) on scan 4.
    # Nothing drives the event, C823.
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch >= 4, slot.Scratch <= 6):
        out(c[826])
    with Rung(slot.Cmd != 0):
        event_drum(
            outputs=[c[821]],
            events=[c[823], c[823], c[823]],
            pattern=[[1], [0], [1]],
            current_step=slot.Result1,
            completion_flag=c[825],
        ).reset(Or(slot.Cmd == 0, slot.Scratch == 4)).jog(c[826])
    entered(slot, 2, slot.Result2)
    done(slot)


def prove_invalid_step_corrects_to_1(slot):
    # Jog on scan 2 takes the drum to step 2.  Its rung is false (C1985) on
    # scans 3..4, and step 5 of 3 is written into its step register on scan 3.
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3, slot.Scratch <= 4):
        out(c[1985])
    with Rung(slot.Cmd != 0, slot.Scratch == 2):
        out(c[1986])
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        copy(5, slot.Result1)
    with Rung(slot.Cmd != 0, ~c[1985]):
        event_drum(
            outputs=[c[1981], c[1982]],
            events=[c[1983], c[1983], c[1983]],
            pattern=[[1, 0], [0, 1], [0, 0]],
            current_step=slot.Result1,
            completion_flag=c[1984],
        ).reset(slot.Cmd == 0).jog(c[1986])
    at_scan(slot, 4, slot.Result1, slot.Result2)
    at_scan(slot, 5, slot.Result1, slot.Result3)
    first_on_from(slot, c[1981], 3, slot.Result4)
    done(slot)


def prove_time_acc_clears_on_every_transition(slot):
    # Each step times 100 ms before the next control: a jog (C1993) from step
    # 1, a jump (C1994) from step 2 to step 3, then step 3 advances on its own
    # 205 ms, and a reset (C1995, latched; the reset input is it or
    # Cmd == 0) from step 4.  Below the drum, each transition's scan records
    # TD491.
    with Rung(slot.Cmd != 0):
        copy(3, slot.Scratch)
    with Rung(slot.Cmd != 0, slot.Result1 == 1, td[491] >= 100):
        out(c[1993])
    with Rung(slot.Cmd != 0, slot.Result1 == 2, td[491] >= 100):
        out(c[1994])
    with Rung(slot.Cmd != 0, slot.Result1 == 4, td[491] >= 100):
        latch(c[1995])
    with Rung(slot.Cmd != 0):
        time_drum(
            outputs=[c[1991]],
            presets=[20000, 20000, 205, 20000],
            unit="ms",
            pattern=[[1], [0], [1], [0]],
            current_step=slot.Result1,
            accumulator=td[491],
            completion_flag=c[1992],
        ).reset(Or(slot.Cmd == 0, c[1995])).jump(c[1994], step=slot.Scratch).jog(c[1993])
    with Rung(slot.Cmd != 0, c[1993], slot.Result1 == 2):
        copy(td[491], slot.Result2)
    with Rung(slot.Cmd != 0, c[1994], slot.Result1 == 3):
        copy(td[491], slot.Result3)
    with Rung(slot.Cmd != 0, slot.Result1 == 4, ~c[1996]):
        copy(td[491], slot.Result4)
        latch(c[1996])
    with Rung(slot.Cmd != 0, c[1995], ~c[1997]):
        copy(td[491], slot.ErrorCode)
        latch(c[1997])
    with Rung(slot.Cmd != 0, c[1997]):
        copy(1, slot.Fired)


def init_te_level_event_already_on(slot):
    reset(c[1913])


def init_time_acc_clears_on_every_transition(slot):
    reset(c[1995])
    reset(c[1996])
    reset(c[1997])


def prove_pins_read_before_step_fix(slot):
    # Step 5 is written into the step register (Result1) on scan 3, above the
    # drum.  Its rung and its jog pin share the condition Result1 > 2; the jog
    # input is that and scan 3.
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        copy(5, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Result1 > 2):
        event_drum(
            outputs=[c[1971], c[1972]],
            events=[c[1973], c[1974]],
            pattern=[[1, 0], [0, 1]],
            current_step=slot.Result1,
            completion_flag=c[1975],
        ).reset(slot.Cmd == 0).jog(And(slot.Result1 > 2, slot.Scratch == 3))
    first_on_from(slot, c[1972], 3, slot.Result3)
    first_on_from(slot, c[1971], 3, slot.Result4)
    done(slot)


def prove_pins_read_before_step_fix_tee(slot):
    # Step 5 is written into the step register (Result1) on scan 3, above the
    # drum.  The drum's rung and its two pins share Result1 != 1 (true while
    # idle, at step 0, and on the bad step); the jog input is C1980, on scan 3.
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        copy(5, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        out(c[1980])
    with Rung(slot.Result1 != 1):
        event_drum(
            outputs=[c[1976], c[1977]],
            events=[c[1978], c[1978]],
            pattern=[[1, 0], [0, 1]],
            current_step=slot.Result1,
            completion_flag=c[1979],
        ).reset(And(slot.Result1 != 1, slot.Cmd == 0)).jog(And(slot.Result1 != 1, c[1980]))
    first_on_from(slot, c[1977], 3, slot.Result3)
    first_on_from(slot, c[1976], 3, slot.Result4)
    done(slot)


group = Group(
    "drum",
    [
        case(
            "A Time & Event step whose time is up keeps timing while it waits for its "
            "event: the step stays 1 and its elapsed time passes 500 ms on a 300 ms "
            "duration (Result2 = 1)",
            ladder=prove_te_acc_counts_while_waiting,
            expect={"Result1": 1, "Result2": 1},
            scans=80,
        ),
        case(
            "A Time & Event event is a level: one already on ends step 1 when its 300 ms "
            "is up, and, still on, ends the event-only last step, so the drum completes "
            "(Result2 = 1) on step 2",
            ladder=prove_te_level_event_already_on,
            init=init_te_level_event_already_on,
            expect={"Result1": 2, "Result2": 1},
            scans=60,
        ),
        case(
            "An event drum steps on an event's rising edge: entered with its event "
            "already on, by a jog on scan 4 or by that event on scan 7, a step waits "
            "for the event to go off and on again. The event of steps 2 and 3 is on "
            "from scan 2 except scan 6: step 2 on scan 4 (Result2), step 3 on scan 7 "
            "(Result3), never complete (Result4 = 0)",
            ladder=prove_events_are_edges,
            expect={"Result1": 3, "Result2": 4, "Result3": 7, "Result4": 0},
            scans=SCANS,
        ),
        case(
            "Drum jump and jog act on their rising edges: a jog on for scans 2..4 "
            "steps once (step 2 on scan 2 = Result2), a jump to step 3 on from scan 5 "
            "jumps once (step 3 on scan 5 = Result3), so a jog on scan 7 leaves the "
            "drum on step 4 (Result1)",
            ladder=prove_jump_jog_edge_triggered,
            expect={"Result1": 4, "Result2": 2, "Result3": 5},
            scans=SCANS,
        ),
        case(
            "Drum jump and jog leave the completion flag on: completed on scan 4, "
            "jumped to step 1 on scan 6 and jogged to step 2 (Result1) on scan 7, the "
            "flag is on for 5 scans (Result2), last on scan 8 (Result3)",
            ladder=prove_jump_jog_keep_completion,
            expect={"Result1": 2, "Result2": 5, "Result3": 8},
            scans=SCANS,
        ),
        case(
            "Drum reset is a level that turns every output off: a drum on step 2 of 3 "
            "reset on scans 5 and 6 is on step 1 on scan 5 (Result4) and at the end "
            "(Result1), step 2's output is last on on scan 4 (Result3), and step 1's "
            "comes on once the reset ends, scan 7 (Result2)",
            ladder=prove_reset_no_stale_outputs,
            expect={"Result1": 1, "Result2": 7, "Result3": 4, "Result4": 1},
            scans=SCANS,
        ),
        case(
            "Drum reset wins over jump and jog on its scan, and jump beats jog: from "
            "step 2, reset and jump to 3 leave step 1 (Result2); reset and jog leave "
            "step 1 (Result3); then jump to 3 and jog leave step 3 (Result1)",
            ladder=prove_reset_jump_jog_order,
            expect={"Result1": 3, "Result2": 1, "Result3": 1},
            scans=SCANS,
        ),
        case(
            "A drum whose step register holds a step it does not have goes to step 1 "
            "when its rung is true: 5 written on step 2 of 3 while paused stays 5 "
            "(Result2), and the scan the rung comes back the step is 1 (Result3) and "
            "step 1's output is on (Result4 = 5)",
            ladder=prove_invalid_step_corrects_to_1,
            expect={"Result1": 1, "Result2": 5, "Result3": 1, "Result4": 5},
            scans=SCANS,
        ),
        case(
            "A time drum's accumulator reads 0 on the scan of every step change, "
            "each after 100 ms or more of timing: a jog (Result2), a jump (Result3), "
            "an advance on a 205 ms step (Result4) and a reset (ErrorCode), which "
            "leaves step 1 (Result1)",
            ladder=prove_time_acc_clears_on_every_transition,
            init=init_time_acc_clears_on_every_transition,
            expect={"Result1": 1, "Result2": 0, "Result3": 0, "Result4": 0, "ErrorCode": 0},
            scans=70,
        ),
        case(
            "A paused drum still takes a jog and a reset, and leaves its outputs as they "
            "are: paused on step 2 for scans 3..5, a jog rising on scan 4 takes it to "
            "step 3 (Result2), step 2's output is last on on scan 4 (Result3), a reset "
            "on scan 5 leaves step 1 (Result1), and step 1's output comes on when the "
            "rung comes back, scan 6 (Result4)",
            ladder=prove_paused_drum_jogs_and_resets,
            expect={"Result1": 1, "Result2": 3, "Result3": 4, "Result4": 6},
            scans=SCANS,
        ),
        case(
            "A drum turns its outputs off when it completes; after that a jog brings them "
            "back but a jump does not: completed on scan 3, step 2's output is on for 2 "
            "scans (Result2), scan 2 and after a jog on scan 8 (ErrorCode = 8) to step "
            "2 (Result1); a jump to step 1 (Result4) on scan 6 leaves step 1's off "
            "(Result3 = 0)",
            ladder=prove_completion_turns_outputs_off,
            expect={"Result1": 2, "Result2": 2, "Result3": 0, "Result4": 1, "ErrorCode": 8},
            scans=SCANS,
        ),
        case(
            "A paused drum takes a jump: paused on step 1 for scans 3..5, a jump to step "
            "3 on scan 4 reads 3 on that scan (Result2) and at the end (Result1); step "
            "1's output stays on through the pause (last on scan 5, Result3) and step "
            "3's comes on when the rung comes back (Result4 = 6)",
            ladder=prove_paused_drum_jumps,
            expect={"Result1": 3, "Result2": 3, "Result3": 5, "Result4": 6},
            scans=SCANS,
        ),
        case(
            "A jog that rises on a reset scan is spent: reset on scan 4 with a jog held "
            "from scan 4 to 6, the drum stays on step 1 (Result1) and never enters step "
            "2 (Result2 = 0)",
            ladder=prove_reset_swallows_held_jog,
            expect={"Result1": 1, "Result2": 0},
            scans=SCANS,
        ),
        case(
            "A drum reads its rung and its pins at one moment, before it runs: a step "
            "register staged out of range on scan 3 is the condition its rung and its "
            "jog pin share, and the jog sees it though the drum corrects the step to 1 "
            "as it runs; the jog takes the corrected step to 2 (Result1), whose output "
            "is on from scan 3 (Result3), and step 1's never comes on (Result4 = 0)",
            ladder=prove_pins_read_before_step_fix,
            probe="pins read before the box executes",
            expect={"Result1": 2, "Result3": 3, "Result4": 0},
            scans=SCANS,
        ),
        case(
            "The tee drawing of the same question: a drum whose reset and jog pins tee "
            "off its input wire after Result1 != 1 reads its rung and pins at one "
            "moment, before it runs; a step register staged out of range on scan 3 is "
            "that condition, the jog sees it though the drum corrects the step to 1 as "
            "it runs, and takes the corrected step to 2 (Result1), whose output is on "
            "from scan 3 (Result3), and step 1's never comes on (Result4 = 0)",
            ladder=prove_pins_read_before_step_fix_tee,
            probe="pins read before the box executes",
            tee=TeeSpec("event_drum(", (".reset()", ".jog()"), after=1),
            expect={"Result1": 2, "Result3": 3, "Result4": 0},
            scans=SCANS,
        ),
    ],
)


def test_drum():
    assert_all_passed(run(group))
