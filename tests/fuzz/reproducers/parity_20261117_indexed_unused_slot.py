"""Reproducer: PLC state holds a slot the program writes as block[n] but no rung uses. CompiledPLC state does not."""

from pyrung.core import PLC, Block, CompiledPLC, Dint, Program, Rung, TagType, search, Bool


def test_reproducer():
    DS = Block("DS", TagType.INT, 1, 7)
    D0 = Dint("D0")
    B1 = Bool("B1")
    DS[1]

    with Program(strict=False) as logic:
        with Rung():
            search(DS.select(6, 7) != -66, result=D0, found=B1)

    interpreted = PLC(logic, dt=0.010)
    compiled = CompiledPLC(logic, dt=0.010)
    interpreted.step()
    compiled.step()

    assert dict(interpreted.current_state.tags) == dict(compiled.current_state.tags)
