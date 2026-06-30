"""Two-roots discipline + replay model for the synthesis overlay.

Synthesis (harness couplings, PILOT holds) is injected only on the soft-exec
PLC; it must never reach the *deploy* root (Click ladder / CircuitPython codegen)
or the ``prove`` verifier — both of which walk the user ``Program``.  And because
synthesis is *logic* (rungs), its writes are **recomputed** on replay, not
recorded as nondeterministic input.
"""

from __future__ import annotations

from pyrung import Bool, Int, Program, Rung, copy
from pyrung.core.analysis.prove import Counterexample, Intractable, always
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
    # (no recent-state-cache hits); over a 200-scan run with checkpoints every
    # 50, most reads replay forward from a checkpoint — the path that *froze*
    # feedback before the synthesis was compiled into the replay kernel.
    plc = PLC(prog, dt=0.1, cache=0, history=10_000, checkpoint_interval=50)
    Harness(plc).install()
    en = True
    live: dict[int, bool] = {}
    for i in range(200):
        if i % 40 == 0:
            en = not en  # toggle so the feedback genuinely evolves over the run
        plc.patch({"Enable": en})
        plc.step()
        live[plc.state.scan_id] = plc.state.tags["Fb"]

    # The compiled replay kernel includes the synthesis brackets, so feedback is
    # re-derived deterministically — never frozen at the checkpoint value.
    for scan_id in (5, 23, 47, 88, 150, 199):
        assert plc.history.at(scan_id).tags["Fb"] == live[scan_id]


def test_bool_replay_compiled_and_interpreted_agree() -> None:
    # The compiled replay surface (synthesis compiled into the kernel) must agree
    # scan-for-scan with the interpreted surface (synthesis re-installed via
    # fork_onto) — compiled-surface parity for the feedback overlay.
    prog = _coupling_program()
    plc = PLC(prog, dt=0.1, cache=0, history=10_000, checkpoint_interval=50)
    Harness(plc).install()
    en = True
    for i in range(120):
        if i % 30 == 0:
            en = not en
        plc.patch({"Enable": en})
        plc.step()

    kernel = plc._compiled_replay_supported_kernel()
    assert kernel is not None  # bool feedback ⇒ compilable (no io-gaps)
    for s in (10, 60, 119):
        compiled = plc._replay_to_compiled(s, kernel)
        interpreted = plc._replay_to_interpreted(s)
        assert compiled.current_state.tags["Fb"] == interpreted.current_state.tags["Fb"]
        assert compiled.current_state.tags["Stage"] == interpreted.current_state.tags["Stage"]
