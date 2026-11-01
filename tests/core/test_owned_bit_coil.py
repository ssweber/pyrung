"""Coils on a bit a timer or counter owns are refused when the Program closes."""

from types import SimpleNamespace

import pytest

from pyrung.core import (
    Block,
    Bool,
    Int,
    Program,
    Rung,
    TagType,
    calc,
    copy,
    count_down,
    count_up,
    latch,
    off_delay,
    on_delay,
    out,
    reset,
    toggle,
)
from pyrung.core.validation.owned_bit_coil import OwnedBitCoilError

En = Bool("En")
Rst = Bool("Rst")
Acc = Int("Acc")


def _u(done, acc=Acc):
    """A structural timer/counter: any object with Done and Acc fields."""
    return SimpleNamespace(Done=done, Acc=acc)


Flag = Bool("Flag")
C = Block("OC", TagType.BOOL, 1, 16)


def _build(owner, coil):
    with Program(strict=False):
        with Rung(En):
            owner()
        with Rung(En):
            coil()


OWNERS = {
    "on_delay": lambda d: on_delay(_u(d), 100),
    "off_delay": lambda d: off_delay(_u(d), 100),
    "retentive": lambda d: on_delay(_u(d), 100).reset(Rst),
    "count_up": lambda d: count_up(_u(d), 5).reset(Rst),
    "count_down": lambda d: count_down(_u(d), 5).reset(Rst),
    "up_down": lambda d: count_up(_u(d), 5).down(Flag).reset(Rst),
}
COILS = {"out": out, "latch": latch, "reset": reset, "toggle": toggle}


@pytest.mark.parametrize("coil", sorted(COILS))
@pytest.mark.parametrize("owner", sorted(OWNERS))
def test_coil_on_done_refused(owner, coil):
    done = Bool("PlainDone")
    with pytest.raises(OwnedBitCoilError) as exc:
        _build(lambda: OWNERS[owner](done), lambda: COILS[coil](done))
    msg = str(exc.value)
    assert "PlainDone" in msg and f"{coil}()" in msg and ".reset()" in msg
    assert owner.split("_")[0] in msg or owner in ("retentive", "up_down")
    msg.encode("ascii")


def test_coil_before_owner_refused():
    done = Bool("PlainDone")
    with pytest.raises(OwnedBitCoilError):
        with Program(strict=False):
            with Rung(En):
                out(done)
            with Rung(En):
                on_delay(_u(done), 100)


def test_coil_in_branch_and_subroutine_refused():
    done = Bool("PlainDone")
    with pytest.raises(OwnedBitCoilError):
        with Program(strict=False):
            with Rung(En):
                on_delay(_u(done), 100)
                with Rung(Rst):
                    latch(done)


def test_block_range_covering_owned_bit_refused():
    with pytest.raises(OwnedBitCoilError):
        _build(lambda: count_up(_u(C[3]), 5).reset(Rst), lambda: reset(C.select(1, 8)))


def test_block_range_not_covering_owned_bit_legal():
    _build(lambda: count_up(_u(C[12]), 5).reset(Rst), lambda: reset(C.select(1, 8)))


def test_legal_forms():
    done = Bool("PlainDone")
    with Program(strict=False):
        with Rung(En):
            on_delay(_u(done), 100).reset(Rst)
        with Rung(done):
            out(Flag)
        with Rung(Rst):
            copy(0, Acc)
            calc(Acc + 1, Acc)
