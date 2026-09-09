"""Incoming values remain open through ladder resets and sampled copies."""

import pytest

from pyrung import Bool, Int, Program, Real, Rung, calc, copy, out
from pyrung.core import Block, TagType
from pyrung.core.instruction.send_receive import ModbusTcpTarget, receive
from pyrung.core.validation.context import ValidationContext


def _receive(dest):
    receive(
        target=ModbusTcpTarget("peer", "127.0.0.1", port=502, device_id=1),
        remote_start="DS501",
        dest=dest,
        receiving=Bool("Receiving"),
        success=Bool("Success"),
        error=Bool("Error"),
        exception_response=Int("Exception"),
    )


@pytest.mark.parametrize("range_dest", [False, True])
@pytest.mark.parametrize("reset", [False, True])
@pytest.mark.parametrize("external", [False, True])
def test_receive_payload_stays_open_through_resets_and_copy_chains(range_dest, reset, external):
    if range_dest:
        registers = Block("Registers", TagType.INT, 501, 514)
        registers.slot(512, name="RemoteMoldStep", external=external)
        incoming, dest = registers[512], registers.select(501, 514)
    else:
        incoming = Int("RemoteMoldStep", external=external)
        dest = incoming
    sampled, derived = Int("Sampled"), Int("Derived")
    with Program(strict=False) as program:
        with Rung(Bool("Poll", external=True)):
            _receive(dest)
        with Rung(Bool("Sample", external=True)):
            copy(incoming, sampled)
            calc(sampled + 1, derived)
        if reset:
            with Rung(incoming == 1):
                copy(0, incoming)
        with Rung(incoming == 1):
            out(Bool("IncomingResult"))
        with Rung(sampled == 1):
            out(Bool("SampledResult"))
        with Rung(derived == 2):
            out(Bool("DerivedResult"))

    domains = ValidationContext(program).produced_domains
    assert {incoming.name, sampled.name, derived.name}.isdisjoint(domains)
    assert not program.check(select={"CMP_ALWAYS_FALSE", "CMP_ALWAYS_TRUE"})
    # Derived storage holds the last sample; it does not become an external tag.
    assert not sampled.external
    assert not derived.external


@pytest.mark.parametrize("factory", [Int, Real])
@pytest.mark.parametrize("reset", [False, True])
def test_external_input_uncertainty_reaches_sampled_copy(factory, reset):
    incoming = factory("LaserMeasurement", external=True)
    delayed = factory("DelayedLaser")
    with Program(strict=False) as program:
        with Rung(Bool("Sample", external=True)):
            copy(incoming, delayed)
        if reset:
            with Rung(Bool("Reset", external=True)):
                copy(0, delayed)
        with Rung(delayed >= 95):
            out(Bool("Result"))
    assert delayed.name not in ValidationContext(program).produced_domains
    assert not program.check(select={"CMP_ALWAYS_FALSE"})


def test_receive_does_not_erase_explicit_value_contract():
    incoming = Int("RemoteMoldStep", choices={0: "Idle", 1: "Run"})
    with Program() as program:
        with Rung():
            _receive(incoming)
        with Rung(incoming == 2):
            out(Bool("Result"))
    (finding,) = program.check(select={"CMP_ALWAYS_FALSE"})
    assert finding.code == "CMP_ALWAYS_FALSE"


def test_ladder_only_zero_writer_still_proves_comparison_false():
    source, sampled = Int("Source"), Int("Sampled")
    with Program() as program:
        with Rung():
            copy(0, source)
            copy(source, sampled)
        with Rung(sampled == 1):
            out(Bool("Result"))
    (finding,) = program.check(select={"CMP_ALWAYS_FALSE"})
    assert finding.code == "CMP_ALWAYS_FALSE"
