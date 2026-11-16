"""Incoming values remain open through ladder resets and sampled copies."""

import pytest

from pyrung import Bool, Dint, Int, Program, Real, Rung, blockcopy, copy, math, out, rung
from pyrung.core import Block, TagType
from pyrung.core.instruction.send_receive import ModbusTcpTarget, receive
from pyrung.core.validation.context import ValidationContext


def _program_tags(program):
    from pyrung.core.analysis.pdg import build_program_graph

    return build_program_graph(program).tags


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
            math(sampled + 1, derived)
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


def test_a_wide_math_result_has_no_listed_domain():
    # One Bool times 100,000,000 spans 0..100,000,000; listing it took gigabytes.
    flag, big = Bool("Flag", external=True), Dint("Big")
    with Program(strict=False) as program:
        with rung(Bool("Go", external=True)):
            math(flag * 100_000_000, big)
        with rung(big == 5):
            out(Bool("Result"))
    assert big.name not in ValidationContext(program).produced_domains
    assert not program.check(select={"CMP_ALWAYS_FALSE"})


def test_a_math_result_that_wraps_keeps_every_value():
    # 32760..32770 into an Int stores 32760..32767 and -32768..-32766.
    small, wrapped = Int("Small", min=0, max=10, external=True), Int("Wrapped")
    with Program(strict=False) as program:
        with rung(Bool("Go", external=True)):
            math(small + 32760, wrapped)
        with rung(wrapped == -32767):
            out(Bool("Result"))
    assert ValidationContext(program).produced_domains.get(wrapped.name) != ()
    assert not program.check(select={"CMP_ALWAYS_FALSE"})


def test_a_real_input_between_whole_number_bounds_stays_open():
    pressure = Real("Pressure", external=True, min=0.0, max=10.0)
    with Program() as program:
        with Rung(pressure == 7.5):
            out(Bool("AtSetpoint"))
    assert pressure.name not in ValidationContext(program).produced_domains
    assert not program.check(select={"CMP_ALWAYS_FALSE"})


def test_a_copy_stores_the_converted_value():
    big, clamped = Dint("Big"), Int("Clamped")
    with Program() as program:
        with Rung():
            copy(40000, big)
            copy(big, clamped)
    assert ValidationContext(program).produced_domains[clamped.name] == (0, 32767)


def test_a_block_copy_stores_the_converted_value():
    wide = Block("Wide", TagType.DINT, 1, 2)
    narrow = Block("Narrow", TagType.INT, 1, 2)
    with Program() as program:
        with Rung():
            copy(70000, wide[1])
            copy(-70000, wide[2])
            blockcopy(wide.select(1, 2), narrow.select(1, 2))
    domains = ValidationContext(program).produced_domains
    assert domains[narrow[1].name] == (0, 32767)
    assert domains[narrow[2].name] == (-32768, 0)


def test_a_text_literal_into_a_char_claims_no_value():
    from pyrung.core.analysis.prove.classify import _collect_literal_write_domains

    text = Block("Text", TagType.CHAR, 1, 3)
    with Program() as program:
        with Rung():
            copy("ABC", text[1])
    # The Copy writes one character into each of Text1..Text3, not "ABC" into Text1.
    assert text[1].name not in _collect_literal_write_domains(program, _program_tags(program))


def test_system_tags_have_no_produced_domain():
    from pyrung.core.system_points import system

    with Program() as program:
        with Rung(system.sys.scan_counter > 3):
            out(Bool("Running"))
    assert system.sys.scan_counter.name not in ValidationContext(program).produced_domains


def test_a_copy_through_a_pointer_stores_the_converted_value():
    wide = Block("WideByPointer", TagType.DINT, 1, 2)
    pointer = Int("Pointer", external=True, min=1, max=2)
    narrow = Int("NarrowByPointer")
    with Program() as program:
        with Rung():
            copy(40000, wide[1])
            copy(wide[pointer], narrow)
        with Rung(narrow == 32767):
            out(Bool("AtMax"))
    assert not program.check(select={"CMP_ALWAYS_FALSE"})
    assert ValidationContext(program).produced_domains[narrow.name] == (0, 32767)
