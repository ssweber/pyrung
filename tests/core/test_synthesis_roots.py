"""Two-roots discipline + replay model for the synthesis overlay.

Synthesis (harness couplings, PILOT holds) is injected only on the soft-exec
PLC; it must never reach the *deploy* root (Click ladder / CircuitPython codegen)
or the ``prove`` verifier — both of which walk the user ``Program``.  And because
synthesis is *logic* (rungs), its writes are **recomputed** on replay, not
recorded as nondeterministic input.
"""

from __future__ import annotations

from typing import Any

import pytest

from pyrung import Bool, Int, Program, Real, Rung, Timer, copy, latch, on_delay, out
from pyrung.core.analysis.prove import Counterexample, Intractable, always
from pyrung.core.compiled_plc import CompiledPLC
from pyrung.core.harness import Coupling, Harness
from pyrung.core.physical import Physical
from pyrung.core.runner import PLC
from tests.core.scan_loop_counts import ScansOutsideLoop

_SWITCH = Physical("Switch", on_delay="200ms", off_delay="100ms")


def _coupling_program() -> Program:
    Enable = Bool("Enable", external=True)
    Fb = Bool("Fb", physical=_SWITCH, link="Enable")
    Stage = Int("Stage")
    with Program() as prog:
        with Rung(Enable, Fb):
            copy(1, Stage)
    return prog


# ── Root separation: synthesis is on the PLC, never on the user Program ───────


def test_synthesis_lives_on_plc_not_program() -> None:
    prog = _coupling_program()
    plc = PLC(prog, dt=0.1)
    Harness(plc).install()

    # The overlay exists on the PLC...
    assert plc._synthesis is not None
    assert plc._synthesis.plant  # bool coupling lowered to plant rungs

    # ...but the user Program is pristine — no synthesis rungs, no overlay
    # subroutines, no internal coupling tags anywhere in the deploy/prove root.
    assert "__holds__" not in prog.subroutines
    assert "__plant__" not in prog.subroutines
    assert prog.subroutines == {}
    assert len(prog.rungs) == 1  # only the user's single rung


def test_synthesis_absent_from_deploy_compile_root() -> None:
    # The compiled kernel that both deploy (CircuitPython) and the soft-exec
    # share compiles the *program* — the brackets must not appear in it.
    from pyrung.circuitpy.codegen import compile_kernel

    prog = _coupling_program()
    plc = PLC(prog, dt=0.1)
    Harness(plc).install()

    kernel = compile_kernel(plc._program)
    synth_tags = [name for name in kernel.referenced_tags if name.startswith("__cpl_")]
    assert synth_tags == []  # no coupling-timer accumulators in the deploy kernel


def test_holds_live_on_overlay_not_program_or_deploy_root() -> None:
    # PILOT holds are synthesis-overlay rungs on the PLC, never in the user
    # Program — so deploy (compile the bare program) and prove never see them,
    # the same two-roots discipline as the plant.
    from pyrung.circuitpy.codegen import compile_kernel
    from pyrung.core.synthesis import Synthesis, copy_hold_rung

    Enable = Bool("Enable", external=True)
    Held = Bool("Held", external=True)
    with Program() as prog:
        with Rung(Enable):
            copy(1, Int("Stage"))
    plc = PLC(prog, dt=0.1)
    plc._synthesis = Synthesis(holds=[copy_hold_rung(value=True, dest=Held)])

    # The user Program is pristine — the hold is not one of its rungs.
    assert len(prog.rungs) == 1
    # The soft-exec root brackets the hold in; the deploy root (bare program) omits it.
    assert len(plc._soft_exec_program().rungs) == 2
    assert "Held" not in compile_kernel(plc._program).referenced_tags


def test_prove_treats_coupling_feedback_as_free() -> None:
    # ``prove`` walks the bare program with no harness, so the feedback tag ``Fb``
    # is a free/nondeterministic input (sound over-approximation): it can be both
    # True and False, so "Stage is always 0" must NOT hold (a counterexample where
    # Enable & Fb fires the copy exists), proving feedback is unconstrained.
    Enable = Bool("Enable", external=True)
    Fb = Bool("Fb", physical=_SWITCH, link="Enable")
    Stage = Int("Stage")
    with Program() as prog:
        with Rung(Enable, Fb):
            copy(1, Stage)

    result = always(prog, Stage == 0, max_states=10_000, depth_budget=10)
    if isinstance(result, Intractable):
        return  # pragma: no cover - environment-dependent
    # Free feedback ⇒ the prover finds Enable & Fb ⇒ Stage := 1, refuting "always 0".
    assert isinstance(result, Counterexample)


# ── Replay model: bool feedback recomputes (it is logic, not recorded) ────────


def _drive(plc: PLC, en_seq: list[bool]) -> list[bool]:
    out = []
    for en in en_seq:
        plc.patch({"Enable": en})
        plc.step()
        out.append(plc.state.tags["Fb"])
    return out


def test_bool_feedback_not_recorded_as_patch() -> None:
    prog = _coupling_program()
    plc = PLC(prog, dt=0.1)
    Harness(plc).install()
    _drive(plc, [True] * 5 + [False] * 3)

    # The ScanLog records the *command* (Enable patches) the synthesis reads —
    # never the synthesized feedback, which replay re-derives by re-running it.
    log = plc._scan_log.snapshot()
    for _scan_id, patches in log.patches_by_scan.items():
        assert "Fb" not in patches
        assert not any(name.startswith("__cpl_") for name in patches)


def test_bool_feedback_recomputes_on_fork() -> None:
    prog = _coupling_program()
    plc = PLC(prog, dt=0.1)
    Harness(plc).install()
    en_seq = [True] * 5 + [False] * 3
    live = _drive(plc, en_seq)

    # Fork at the start and re-run the same commands: the overlay travels with the
    # fork (re-installed via fork_onto) and recomputes the feedback bit-for-bit.
    fork = plc.fork(scan_id=0)
    replayed = _drive(fork, en_seq)
    assert replayed == live


def test_bool_feedback_recomputes_on_history_replay() -> None:
    prog = _coupling_program()
    # cache=0 forces every historical read through the actual replay machinery
    # (no recent-state-cache hits); with checkpoints every 8, most reads replay
    # forward from a checkpoint — the path that *froze* feedback before the
    # synthesis was compiled into the replay kernel.  Toggle every 7 scans so the
    # sampled window straddles many rise/fall *transitions*, not just stable holds
    # (a phase bug hides inside a hold and only shows at a transition).
    plc = PLC(prog, dt=0.1, cache=0, history=10_000, checkpoint_interval=8)
    Harness(plc).install()
    en = True
    live: dict[int, bool] = {}
    for i in range(80):
        if i % 7 == 0:
            en = not en
        plc.patch({"Enable": en})
        plc.step()
        live[plc.state.scan_id] = plc.state.tags["Fb"]

    # Re-derived deterministically at *every* scan — never frozen, never phase-shifted.
    for scan_id in range(1, 81):
        assert plc.history.at(scan_id).tags["Fb"] == live[scan_id]


def test_bool_replay_compiled_and_interpreted_agree() -> None:
    # Both replay surfaces — compiled (synthesis compiled into the kernel, plant as
    # a pre-drain pass) and interpreted (synthesis re-installed via fork_onto) —
    # must reproduce the *live* forward run scan-for-scan.  Toggle the command
    # every scan so the plant is perpetually mid-transition: this is what exposes a
    # one-scan phase divergence (sampling mid-hold, where Fb is stable, hides it —
    # the plant-post compiled bug passed a coarse sample but fails here).
    prog = _coupling_program()
    plc = PLC(prog, dt=0.1, cache=0, history=10_000, checkpoint_interval=5)
    Harness(plc).install()
    en_seq = [True, True, False, True, False, False, True, True, True, False, True, False]
    live = _drive(plc, en_seq)

    kernel = plc._compiled_replay_supported_kernel()
    assert kernel is not None  # bool feedback ⇒ compilable (no io-gaps)
    assert kernel.pre_step_fn is not None  # the plant is a separate pre-drain pass
    for s in range(1, len(en_seq) + 1):
        compiled = plc._replay_to_compiled(s, kernel)
        interpreted = plc._replay_to_interpreted(s)
        assert compiled.current_state.tags["Fb"] == live[s - 1]
        assert interpreted.current_state.tags["Fb"] == live[s - 1]
        assert compiled.current_state.tags["Stage"] == interpreted.current_state.tags["Stage"]


# ── A plant's tags are in the state from the install ──────────────────────────


def test_the_install_gives_each_plant_tag_its_default() -> None:
    plc = PLC(_coupling_program(), dt=0.1)
    Harness(plc).install()

    assert plc._initial_state is plc.state
    assert {name: value for name, value in plc.state.tags.items() if name.startswith("__cpl_")} == {
        "__cpl_ond__Fb": False,
        "__cpl_on__Fb": 0,
        "__cpl_offd__Fb": False,
        "__cpl_off__Fb": 0,
    }


def _exact(tags: Any) -> dict[str, tuple[type, Any]]:
    return {name: (type(value), value) for name, value in tags.items()}


@pytest.mark.parametrize("exact_anchor", [False, True], ids=["preview", "anchor"])
@pytest.mark.parametrize("enable", [True, False], ids=["enabled", "not_enabled"])
def test_a_kernel_holds_the_plant_tags_stepping_does(exact_anchor: bool, enable: bool) -> None:
    """A plant stores its timers' zeros from its first scan, into a tag no rung has written."""
    plc = PLC(_coupling_program(), dt=0.01)
    Harness(plc).install()
    plc.patch({"Enable": enable})
    plc.step()
    twin = plc.fork()
    kernel = plc._compiled_replay_supported_kernel()
    assert kernel is not None
    replay = CompiledPLC(
        plc._soft_exec_program(),
        initial_state=plc.state,
        dt=plc._dt,
        compiled=kernel,
        exact_anchor=exact_anchor,
    )
    replay._set_rtc_internal(plc._system_runtime._rtc_now(plc.state), plc.state.timestamp)

    for _ in range(40):
        replay.step_replay()
        twin.step()
        assert _exact(replay._materialize_replay_state().tags) == _exact(twin.state.tags)


def test_the_plant_tags_first_transition_starts_at_the_default() -> None:
    plc = PLC(_coupling_program(), dt=0.01)
    Harness(plc).install()
    plc.patch({"Enable": True})
    plc.run(5)

    transition = plc._causal_last_committed_transition_before("__cpl_on__Fb", 3)

    assert transition is not None
    assert (transition.from_value, transition.to_value) == (0, 10)


# â”€â”€ A plant joining a run leaves the scans that ran without it â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _late_link_program() -> Program:
    Enable = Bool("Enable", external=True)
    Fb = Bool("Fb")
    Stage = Int("Stage")
    with Program(strict=False) as prog:
        with Rung(Enable, Fb):
            copy(1, Stage)
    return prog


def _late_coupling() -> dict[str, Coupling]:
    return {"Fb": Coupling(en_name="Enable", fb_name="Fb", physical=_SWITCH)}


def _run_recording(plc: PLC, scans: int) -> dict[int, Any]:
    states = {}
    for _ in range(scans):
        plc.step()
        states[plc.state.scan_id] = plc.state
    return states


def test_a_coupling_change_leaves_earlier_scans_as_they_ran() -> None:
    plc = PLC(_late_link_program(), dt=0.01)
    harness = Harness(plc)
    harness.install()
    plc.patch({"Enable": True})
    ran = _run_recording(plc, 20)

    harness.set_overrides(_late_coupling())
    tip = plc.state
    assert "__cpl_on__Fb" in tip.tags
    assert len(plc._recent_state_cache) == 21
    ran.update(_run_recording(plc, 25))
    assert plc.state.tags["Stage"] == 1

    for scan_id, state in ran.items():
        if scan_id != tip.scan_id:
            assert _exact(plc.history.at(scan_id).tags) == _exact(state.tags), scan_id
    assert _exact(plc.history.at(tip.scan_id).tags) == _exact(tip.tags)
    assert _exact(plc.fork_from(15).state.tags) == _exact(ran[15].tags)
    assert plc._recent_state_cache_bytes == sum(
        estimate for _, estimate in plc._recent_state_cache.values()
    )


def test_an_uninstall_leaves_earlier_scans_as_they_ran() -> None:
    plc = PLC(_late_link_program(), dt=0.01)
    harness = Harness(plc)
    harness.set_overrides(_late_coupling())
    harness.install()
    plc.patch({"Enable": True})
    ran = _run_recording(plc, 20)
    assert any(name.startswith("_frac:") for name in plc.state.memory)

    tip = plc.state.scan_id
    coil_memory = {name for name in plc.state.memory if ":plant:fb." in name}
    assert coil_memory
    harness.uninstall()
    ran.update(_run_recording(plc, 20))
    plc._recent_state_cache.clear()
    plc._replay_slabs.clear()

    for scan_id, state in ran.items():
        assert _exact(plc.history.at(scan_id).tags) == _exact(state.tags), scan_id
        memory = dict(state.memory)
        if scan_id == tip:
            # The uninstall clears the coils' memory at the tip, so a later install settles.
            memory = {name: value for name, value in memory.items() if name not in coil_memory}
        assert dict(plc.history.at(scan_id).memory) == memory, scan_id


def test_a_coupling_change_on_a_checkpoint_edits_the_checkpoint() -> None:
    plc = PLC(_late_link_program(), dt=0.01)
    harness = Harness(plc)
    harness.install()
    plc.patch({"Enable": True})
    plc.step()
    while plc.state.scan_id not in plc._checkpoints:
        plc.step()

    harness.set_overrides(_late_coupling())

    assert plc._checkpoints[plc.state.scan_id] is plc.state


def test_a_late_install_leaves_earlier_scans_as_they_ran() -> None:
    plc = PLC(_coupling_program(), dt=0.01)
    plc.patch({"Enable": True})
    ran = _run_recording(plc, 40)

    Harness(plc).install()

    for scan_id, state in ran.items():
        if scan_id != plc.state.scan_id:
            assert _exact(plc.history.at(scan_id).tags) == _exact(state.tags), scan_id
    assert _exact(plc.fork_from(30).state.tags) == _exact(ran[30].tags)
    assert plc.history.at(plc.state.scan_id) is plc.state


# â”€â”€ A reset keeps a plant's tags in the state â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _reboot(plc: PLC) -> None:
    plc.reboot()


def _reboot_without_battery(plc: PLC) -> None:
    plc.battery_present = False
    plc.reboot()


def _stop_and_run(plc: PLC) -> None:
    plc.stop()
    plc.start()


_RESETS = [_reboot, _reboot_without_battery, _stop_and_run]
_PLANT_AT_DEFAULTS = {
    "__cpl_ond__Fb": False,
    "__cpl_on__Fb": 0,
    "__cpl_offd__Fb": False,
    "__cpl_off__Fb": 0,
}


def _mid_dwell(plc: PLC) -> PLC:
    Harness(plc).install()
    plc.patch({"Enable": True})
    plc.run(5)
    assert plc.state.tags["__cpl_on__Fb"] > 0
    return plc


@pytest.mark.parametrize("reset", _RESETS)
def test_a_reset_starts_each_plant_tag_over_at_its_default(reset: Any) -> None:
    plc = _mid_dwell(PLC(_coupling_program(), dt=0.01))

    reset(plc)

    assert {k: v for k, v in plc.state.tags.items() if k in _PLANT_AT_DEFAULTS} == (
        _PLANT_AT_DEFAULTS
    )
    assert plc._causal_initial_tags["__cpl_on__Fb"] == 0


@pytest.mark.parametrize("exact_anchor", [False, True], ids=["preview", "anchor"])
@pytest.mark.parametrize("reset", _RESETS)
def test_a_kernel_holds_the_plant_tags_stepping_does_after_a_reset(
    reset: Any, exact_anchor: bool
) -> None:
    plc = _mid_dwell(PLC(_coupling_program(), dt=0.01))
    reset(plc)
    twin = plc.fork()
    kernel = plc._compiled_replay_supported_kernel()
    assert kernel is not None
    replay = CompiledPLC(
        plc._soft_exec_program(),
        initial_state=plc.state,
        dt=plc._dt,
        compiled=kernel,
        exact_anchor=exact_anchor,
    )
    replay._set_rtc_internal(plc._system_runtime._rtc_now(plc.state), plc.state.timestamp)

    for _ in range(40):
        replay.step_replay()
        twin.step()
        assert _exact(replay._materialize_replay_state().tags) == _exact(twin.state.tags)


# ── Replay runs a scan under the plant that ran it ────────────────────────────


def _late_ramp_recording() -> tuple[PLC, dict[int, Any]]:
    Temp = Real("Temp", physical=Physical("Therm", ramp_on=1.0, ramp_off=-0.5), link="Enable")
    Enable, Lamp, Hit = Bool("Enable"), Bool("Lamp"), Bool("Hit")
    with Program(strict=False) as prog:
        with Rung(Enable):
            out(Lamp)
        with Rung(Temp >= 3.0):
            latch(Hit)
    plc = PLC(prog, dt=0.01)
    plc.patch({"Enable": True})
    ran = _run_recording(plc, 700)
    Harness(plc).install()
    ran.update(_run_recording(plc, 400))
    assert plc.state.tags["Hit"] is True and plc.state.tags["Temp"] > 3.0
    plc._reset_cache(plc.state)  # Anything older now takes a replay.
    return plc, ran


@pytest.mark.parametrize("scan_id", [5, 199, 201, 401, 650, 699, 700, 701, 900, 1099])
def test_replay_runs_each_scan_under_the_plant_that_ran_it(scan_id: int) -> None:
    plc, ran = _late_ramp_recording()

    if scan_id != 700:  # The install edited this one.
        assert _exact(plc.history.at(scan_id).tags) == _exact(ran[scan_id].tags)
    assert _exact(plc.replay_to(scan_id).state.tags) == _exact(plc.history.at(scan_id).tags)


@pytest.mark.parametrize("path", ["replay_to", "interpreted"])
def test_a_replay_of_the_install_scan_steps_on_as_the_run_did(path: str) -> None:
    plc, ran = _late_ramp_recording()

    replay = plc.replay_to(700) if path == "replay_to" else plc._replay_to_interpreted(700)

    assert replay._harness is not None
    assert replay.rung_firings(700) == plc.rung_firings(700)
    for scan in range(701, 801):
        replay.step()
        assert _exact(replay.state.tags) == _exact(ran[scan].tags), scan


def test_a_replay_of_a_scan_a_reload_edited_starts_from_the_edit() -> None:
    """A reload drops coil memory no coupling keeps, in place, from the scan it reloads at."""
    fb = Bool("ReloadFb", physical=Physical("S", on_delay="300ms"), link="ReloadEn")
    enable, lamp, toggle = Bool("ReloadEn"), Bool("ReloadLamp"), Bool("ReloadTog")
    with Program(strict=False) as prog:
        with Rung(~toggle):
            out(toggle)
        with Rung(toggle):
            out(enable)
        with Rung(fb):
            out(lamp)
    plc = PLC(prog, dt=0.01, checkpoint_interval=100)
    Harness(plc).install()
    plc.run(600)
    dropped = [name for name in plc.state.memory if ":plant:fb." in name]
    assert dropped
    plc._edit_tip_tags({}, drop_memory=dropped)
    ran = _run_recording(plc, 100)
    plc._reset_cache(plc.state)

    for replay in (plc.replay_to(600), plc._replay_to_interpreted(600)):
        assert dict(replay.state.memory) == dict(plc.history.at(600).memory)
        for scan in range(601, 701):
            replay.step()
            assert _exact(replay.state.tags) == _exact(ran[scan].tags), scan
            assert dict(replay.state.memory) == dict(ran[scan].memory), scan


def test_a_replayed_range_across_a_late_install_keeps_each_side() -> None:
    plc, ran = _late_ramp_recording()

    across = plc.history.range(690, 711)

    assert [state.scan_id for state in across] == list(range(690, 711))
    for state in across:
        if state.scan_id != 700:  # The install edited this one.
            assert _exact(state.tags) == _exact(ran[state.scan_id].tags), state.scan_id
    assert across[0].tags["Temp"] == 0.0
    assert _exact(plc.history.at(700).tags) == _exact(across[10].tags)


def test_a_replay_before_a_coupling_change_has_no_harness() -> None:
    """The plant it runs is not the one today's harness describes: it runs each scan."""
    enable, over, shown = Bool("Enable"), Bool("Over"), Real("Shown")
    timer = Timer.clone("Tm")
    temp = Real(
        "Temp", physical=Physical("TempSensor", approach_to=100.0, approach_rate=5.0), link="Enable"
    )
    with Program(strict=False) as program:
        with Rung(enable):
            on_delay(timer, preset=800_000)
        with Rung():
            copy(temp, shown)
        with Rung(shown >= 100.0):
            out(over)
    plc = PLC(program, dt=0.01)
    harness = Harness(plc)
    harness.install()
    plc.patch({"Enable": True})
    ran = {}
    for _ in range(4_000):
        plc.step()
        ran[plc.state.scan_id] = plc.state
    harness.set_overrides({"Temp": None})
    plc.run(20)

    replay = plc.replay_to(1_000)
    assert replay._harness is None
    counter = ScansOutsideLoop(replay)
    replay.run_for(30.0)
    assert counter["n"] == 3_000, "every scan of the run ran"
    replay._recent_state_cache.clear()
    replay._replay_slabs.clear()
    for scan in (2_000, 3_000, 3_999, 4_000):
        again = replay.history.at(scan).tags
        assert repr(again["Temp"]) == repr(ran[scan].tags["Temp"]), scan
        assert again["Over"] is ran[scan].tags["Over"], scan

    assert plc._harness is harness
    plc._recent_state_cache.clear()
    plc._replay_slabs.clear()
    assert repr(plc.history.at(3_999).tags["Temp"]) == repr(ran[3_999].tags["Temp"])


def test_a_replay_with_no_change_keeps_its_harness() -> None:
    enable = Bool("Enable")
    temp = Real(
        "Temp", physical=Physical("TempSensor", approach_to=100.0, approach_rate=5.0), link="Enable"
    )
    with Program(strict=False) as program:
        with Rung():
            copy(temp, Real("Shown"))
        with Rung(enable):
            out(Bool("Lamp"))
    plc = PLC(program, dt=0.01)
    Harness(plc).install()
    plc.patch({"Enable": True})
    plc.run(50)
    assert plc._replay_to_interpreted(20)._harness is not None


def test_a_reset_forgets_the_earlier_plants() -> None:
    plc, _ran = _late_ramp_recording()
    assert plc._plant_changes

    plc.reboot()

    assert plc._plant_changes == []
