"""Smoke test for Part 2: subroutine writer identity + at-fire-time classification."""

from __future__ import annotations

from pyrung.core import PLC, Bool, Program, Rung, call, latch, out, reset, subroutine
from pyrung.core.program import rung


def build():
    Cmd = Bool("Cmd", external=True)
    CallGate = Bool("CallGate", external=True)
    Target = Bool("Target")
    Echo = Bool("Echo")

    @subroutine("cmd_sub")
    def cmd_sub():
        with rung(Cmd):  # sub rung 0: the writer, gated on the command
            latch(Target)
        with rung(Cmd):  # sub rung 1: consume the command inside the sub (after writer)
            reset(Cmd)

    with Program() as prog:
        with Rung(CallGate):
            call(cmd_sub)
        with Rung(Target):
            out(Echo)

    return prog


def main() -> int:
    prog = build()
    plc = PLC(prog)
    plc.force("CallGate", True)
    for _ in range(5):
        plc.step()
    print(f"before: Target={plc.state.tags.get('Target')} Cmd={plc.state.tags.get('Cmd')}")
    plc.patch({"Cmd": True})
    plc.step()
    leaving = plc.history.newest_scan_id
    print(f"after pulse: Target={plc.state.tags.get('Target')} Cmd={plc.state.tags.get('Cmd')} (scan {leaving})")

    chain = plc.cause("Target")
    print("\n=== cause(Target) ===")
    if chain is None:
        print("None")
        return 1
    print(f"effect: {chain.effect.tag_name} {chain.effect.from_value!r}->{chain.effect.to_value!r} @ {chain.effect.scan_id}")
    for i, step in enumerate(chain.steps):
        print(f"  step{i}: rung={step.rung_index} sub={step.subroutine} caller={step.caller_rung_index}")
        for t in step.triggers:
            print(f"    trigger: {t.tag_name} {t.from_value!r}->{t.to_value!r}")
        for e in step.enablers:
            print(f"    enabler: {e.tag_name}={e.value!r} held_since={e.held_since_scan}")
    print(f"conjunctive_roots: {[(t.tag_name, t.to_value) for t in chain.conjunctive_roots]}")
    print(f"ambiguous_roots: {[(t.tag_name, t.to_value) for t in chain.ambiguous_roots]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
