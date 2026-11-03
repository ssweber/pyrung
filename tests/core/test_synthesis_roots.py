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

from pyrung import Bool, Int, Program, Rung, copy
from pyrung.core.analysis.prove import Counterexample, Intractable, always
from pyrung.core.compiled_plc import CompiledPLC
from pyrung.core.harness import Harness
from pyrung.core.physical import Physical
from pyrung.core.runner import PLC

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
