"""Boundary gates for the skiff instrument (``pilot/skiff.py``).

Trace and let-run have the burner Starting→Execute acceptance test; these are
the skiff's equivalents.  Both programs share the constant-table mask shape the
tide tables solve statically (``test_table_oracle.py``) — ``stateMask &
disabledMask == 0`` — except the disabled-mask word is **live**: rewritten at
runtime, so every static instrument punts and the documented escalation is the
skiff (isolated fork-pin-step probes returning observations the loop's RECORD
point applies to the compass; the verify pipeline confirms every learned edge
live).

Two tiers:

- **Command-selected** (``_command_mask_program``, PASSING — the wired skiff's
  acceptance test): the mask is selected among constant-table rows by Bool
  commands.  Two conditional writers, so the tide tables' single-writer operand
  model punts, but a pair probe (config select + start command) observably
  flips the channel register in isolation.
- **Free-word** (``_live_mask_program``): the mask is copied from an external
  word.  Its resolution is *not* eventual reachability of the undeclared
  program — an unconstrained external word has no complete domain, so the skiff
  has no sound probe values.  The honest answer is a **two-part gate**:

  1. *Undeclared* → an honest decline that **names the offending word** and
     nudges a ``choices=`` declaration (the single source of truth the prover,
     bounds, validators, and skiff all read).  Never a ``how()``-only guess.
  2. *Declared* (``choices=`` on the word) → the existing skiff resolves it with
     no new instrument: the declared values become sound probe candidates, the
     pair probe (word value × load pulse) learns the joint edge, and the live
     verify pipeline confirms it — so ``how()`` reaches the target.

Both gates keep the honesty pins: hand-driveable ground truth (a capability
gap, never an unreachable target), a ``solve_table_predicate`` punt (genuinely
live — if that assertion fails, the static layer got smarter and the gate
needs rework), and named-reason failures.
"""

from __future__ import annotations

from pyrung import PLC, Bool, Int, Program, calc, copy, out, rise, rung
from pyrung.core.analysis.pilot import pilot_how


def _live_mask_program(cfg_choices=None):
    """State machine whose enablement gate mixes a constant table with a live word.

    ``DisabledMask`` rests at 0x0040 (EXECUTE disabled). Unblocking requires a
    coordinated runtime config load: ``CfgWord`` nonzero with bit 6 clear, pulsed
    in via ``CfgLoad``. The ``DisabledMask != 0`` guard ("config must be valid")
    keeps the trivial resting pulse (``CfgWord=0``) from unblocking by accident.

    ``cfg_choices`` declares ``CfgWord``'s complete finite domain (the free-word
    tier's resolution): a small ``choices=`` mapping containing at least one
    permissive value (nonzero, bit 6 clear) and one blocking value.  ``None``
    leaves the word unconstrained — no complete domain, the honest-decline case.
    """
    from pyrung.click import ClickBlocks

    x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

    CmdStart = Bool("CmdStart", external=True)
    CfgLoad = Bool("CfgLoad", external=True)
    CfgWord = (
        Int("CfgWord", external=True)
        if cfg_choices is None
        else Int("CfgWord", external=True, choices=cfg_choices)
    )
    DisabledMask = Int("DisabledMask", default=0x0040)
    StateMaskIdx = Int("StateMaskIdx")
    StateMask = Int("StateMask")
    MaskResult = Int("MaskResult")
    StateRequested = Int("StateRequested", default=0)
    StateCurrent = Int("StateCurrent", default=1)
    Output = Bool("Output")

    ds.slot(300, name="mask_none", default=0x0000)
    ds.slot(301, name="mask_stopped", default=0x0000)
    ds.slot(306, name="mask_execute", default=0x0040)

    with Program() as prog:
        # The live half: DisabledMask is rewritten at runtime from an external
        # word. No constant table backs it; its domain is unknowable statically.
        with rung(rise(CfgLoad)):
            copy(CfgWord, DisabledMask)
        # Request BEFORE the mask chain: the enablement predicate must be
        # computed for the state being requested, or the transition rung
        # consumes a stale row and fires regardless of the mask.
        with rung(CmdStart, StateCurrent == 1):
            copy(6, StateRequested)
        # The constant half: per-state mask from a declared-constant table.
        with rung():
            calc(300 + StateRequested, StateMaskIdx)
        with rung():
            copy(ds[StateMaskIdx], StateMask)
        with rung():
            calc(StateMask & DisabledMask, MaskResult)
        with rung(StateRequested != 0, MaskResult == 0, DisabledMask != 0):
            copy(StateRequested, StateCurrent)
            copy(0, StateRequested)
        with rung(StateCurrent == 6):
            out(Output)

    return prog, Output


def test_live_mask_target_is_hand_driveable():
    """Ground truth: the plant can do it — load a permissive config, then start."""
    prog, _output = _live_mask_program()
    plc = PLC(prog)
    plc.step()

    plc.patch({"CfgWord": 0x0001, "CfgLoad": True})
    plc.step()
    plc.patch({"CfgLoad": False})
    plc.step()
    assert plc.state.tags["DisabledMask"] == 0x0001

    plc.patch({"CmdStart": True})
    for _ in range(3):
        plc.step()
    assert plc.state.tags["StateCurrent"] == 6
    assert plc.state.tags["Output"] is True


def test_static_read_punts_on_live_mask_operand():
    """The tide tables must return None — the mask operand is genuinely live.

    If this starts failing, the static layer learned to resolve the live word;
    the program is then no longer the skiff case and this gate needs rework.
    """
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.tide_tables import solve_table_predicate

    prog, _output = _live_mask_program()
    plc = PLC(prog)
    plc.step()
    pdg = build_program_graph(prog)
    snap = dict(plc.state.tags)

    sol = solve_table_predicate(
        "MaskResult",
        0,
        "==",
        snap,
        pdg,
        prog,
        fixed={"StateRequested": 6},
    )
    assert sol is None, "live DisabledMask operand must punt, not fabricate"


def _command_mask_program():
    """The skiff-winnable tier: the live mask is selected among constant-table
    rows by Bool commands.

    ``DisabledMask`` has TWO conditional table-copy writers, so every static
    read punts (the tide tables' operand model needs a single writer; the
    producible-domain chain needs a sole write) — but a command probe
    observably flips it, which is exactly what fork-pin-step learns. The
    unblock is a coordinated ``CfgProd`` (permissive row, bit 6 clear, nonzero)
    beside the ``CmdStart`` the readable half of the tree already knows.
    """
    from pyrung.click import ClickBlocks

    x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

    CmdStart = Bool("CmdStart", external=True)
    CfgProd = Bool("CfgProd", external=True)
    CfgMaint = Bool("CfgMaint", external=True)
    DisabledMask = Int("DisabledMask", default=0x0040)
    StateMaskIdx = Int("StateMaskIdx")
    StateMask = Int("StateMask")
    MaskResult = Int("MaskResult")
    StateRequested = Int("StateRequested", default=0)
    StateCurrent = Int("StateCurrent", default=1)
    Output = Bool("Output")

    ds.slot(201, name="cfg_prod_row", default=0x0001)
    ds.slot(202, name="cfg_maint_row", default=0x0264)
    ds.slot(300, name="cm_mask_none", default=0x0000)
    ds.slot(301, name="cm_mask_stopped", default=0x0000)
    ds.slot(306, name="cm_mask_execute", default=0x0040)

    with Program() as prog:
        with rung(rise(CfgProd)):
            copy(ds[201], DisabledMask)
        with rung(rise(CfgMaint)):
            copy(ds[202], DisabledMask)
        # Request BEFORE the mask chain — see _live_mask_program.
        with rung(CmdStart, StateCurrent == 1):
            copy(6, StateRequested)
        with rung():
            calc(300 + StateRequested, StateMaskIdx)
        with rung():
            copy(ds[StateMaskIdx], StateMask)
        with rung():
            calc(StateMask & DisabledMask, MaskResult)
        with rung(StateRequested != 0, MaskResult == 0, DisabledMask != 0):
            copy(StateRequested, StateCurrent)
            copy(0, StateRequested)
        with rung(StateCurrent == 6):
            out(Output)

    return prog, Output


def test_command_mask_target_is_hand_driveable():
    """Ground truth for the command-selected tier: select the permissive row, start."""
    prog, _output = _command_mask_program()
    plc = PLC(prog)
    plc.step()

    plc.patch({"CfgProd": True})
    plc.step()
    plc.patch({"CfgProd": False})
    plc.step()
    assert plc.state.tags["DisabledMask"] == 0x0001

    plc.patch({"CmdStart": True})
    for _ in range(3):
        plc.step()
    assert plc.state.tags["StateCurrent"] == 6
    assert plc.state.tags["Output"] is True


def test_skiff_gate_command_selected_mask():
    """THE skiff acceptance flip: how() through a command-selected live mask.

    Statically unreadable (two-writer mask — every tide-tables path punts), but the
    skiff's isolated probes learn which command flips it, the compass carries
    the bearing, and the live verify pipeline confirms the edge. This is the
    boundary gate the skiff instrument was kept dark for.
    """
    prog, output = _command_mask_program()
    plc = PLC(prog)
    path = pilot_how(plc, output, max_scans=600)
    assert path.reachable, f"skiff gate: {path.reason}"
    replay = path.replay()
    assert replay.state.tags["Output"] is True


def test_free_word_declines_naming_the_tag():
    """The free-word tier's resolution, part 1: honest decline.

    An unconstrained external word has no complete domain, so the skiff has no
    sound probe values.  The miss must be unreachable AND carry a specific,
    named reason — the offending word plus a ``choices=`` nudge — not a generic
    ``stuck`` or a silent ``reason=None``.
    """
    prog, output = _live_mask_program()
    plc = PLC(prog)
    path = pilot_how(plc, output, max_scans=600)
    assert not path.reachable, "an undeclared free word has no complete domain to probe"
    assert path.reason, "unreachable target must always name a reason"
    assert "CfgWord" in path.reason, f"reason must name the offending word: {path.reason}"
    assert "choices" in path.reason, f"reason must nudge a choices= declaration: {path.reason}"


def test_free_word_solves_under_declared_choices():
    """The free-word tier's resolution, part 2: declared domain, no new instrument.

    Declaring ``CfgWord``'s complete finite domain (``choices=``) makes its
    values sound probe candidates.  The skiff's pair probe (word value × load
    pulse) learns the joint edge, the compass carries the bearing, and the live
    verify pipeline confirms it — so ``how()`` reaches the target and the
    recording replays to the enabled output.
    """
    prog, output = _live_mask_program(cfg_choices={0x0001: "valid", 0x0040: "invalid"})
    plc = PLC(prog)
    path = pilot_how(plc, output, max_scans=600)
    assert path.reachable, f"declared-domain free word must resolve: {path.reason}"
    replay = path.replay()
    assert replay.state.tags["Output"] is True


def test_compass_contradict_falsifies_seeded_edge():
    """Live no-change evidence removes a learned edge; the probe mark stays."""
    from pyrung.core.analysis.pilot.compass import Compass, CompassObservation

    compass = Compass()
    compass, _ = compass.apply((CompassObservation("edge", "State", ("Cmd", True), 1, 6),))
    assert compass.find_path("State", 1, 6) == [("Cmd", True)]

    compass, changed = compass.apply((CompassObservation("contradict", "State", ("Cmd", True), 1),))
    assert changed is True
    assert compass.find_path("State", 1, 6) is None
    assert compass.unprobed_actions("State", 1, {("Cmd", True)}) == []
    # Idempotent: the tombstone stays and no new knowledge is reported.
    same, changed = compass.apply((CompassObservation("contradict", "State", ("Cmd", True), 1),))
    assert same is compass
    assert changed is False


def test_confirmed_provenance_only_from_outcome_factory():
    """CONFIRMED is minted solely by ``outcome.confirmed_entry``.

    The observation application path records runtime evidence as OBSERVED and
    exposes no mutation API that can forge CONFIRMED.
    """
    from pyrung.core.analysis.pilot.compass import Compass, CompassObservation, Provenance
    from pyrung.core.analysis.pilot.outcome import confirmed_entry

    compass = Compass()

    assert not hasattr(compass, "record")
    assert not hasattr(compass, "commit_confirmed")

    # The factory remains the sole minter.
    entry = confirmed_entry("State", 1, ("Cmd", True), 6)
    assert entry.provenance is Provenance.CONFIRMED
    assert entry.is_live
    compass, _ = compass.apply((CompassObservation("edge", "State", ("Cmd", True), 1, 6),))
    stored = tuple(compass.knowledge.tag_entries("State"))[0][2]
    assert stored.provenance is Provenance.OBSERVED


def test_is_composite_action_shapes():
    """Single action pairs vs skiff-learned joint causes."""
    from pyrung.core.analysis.pilot.compass import is_composite_action

    assert not is_composite_action(("Cmd", True))
    assert not is_composite_action(("Cmd", 3))
    assert is_composite_action((("CfgProd", True), ("CmdStart", True)))
    assert not is_composite_action(())
    assert not is_composite_action("Cmd")
