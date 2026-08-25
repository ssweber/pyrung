"""Concrete one-scan oracle for projected crossings.

The crossing registry is static analysis.  Its independent oracle is the
interpreter: execute one isolated rung, then inspect that occurrence's entry
view and attempted-write journal.  This module deliberately does not import
``pilot.program_step``, ``trace_back``, or any projected reader.

The helpers are intentionally small and live beside the tests while the
crossing contract settles.  They can move to shared test infrastructure once
the destination/mode matrix grows beyond this first regression set.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from operator import eq, ge, gt, le, lt, ne
from typing import Any

import pytest

from pyrung import PLC, Bool, Char, Dint, Int, Program, Real
from pyrung.core import CompiledPLC
from pyrung.core.analysis import crossings
from pyrung.core.context import ConditionView
from pyrung.core.crossing import (
    UNKNOWN,
    Affine,
    AffineCmp,
    Aggregate,
    Cmp,
    CondAttr,
    Constraint,
    CrossingContext,
    Eq,
    External,
    Literal,
    Mask,
    Prior,
    Quant,
    ReverseResult,
    eq_target,
    evaluate_forward,
)
from pyrung.core.executor import ConditionViewCapture, RungRun, execute_program
from pyrung.core.instruction.advanced import SearchInstruction, ShiftInstruction
from pyrung.core.instruction.calc import CalcInstruction
from pyrung.core.instruction.coils import LatchInstruction, OutInstruction, ResetInstruction
from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
from pyrung.core.instruction.data_transfer import (
    BlockCopyInstruction,
    CopyInstruction,
    FillInstruction,
)
from pyrung.core.instruction.drums import EventDrumInstruction, TimeDrumInstruction
from pyrung.core.instruction.packing import (
    PackBitsInstruction,
    PackTextInstruction,
    PackWordsInstruction,
    UnpackToBitsInstruction,
    UnpackToWordsInstruction,
)
from pyrung.core.instruction.send_receive._core import ModbusReceiveInstruction
from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction
from pyrung.core.memory_block import Block
from pyrung.core.rung import Rung
from pyrung.core.scan_log import IoResultRecord, IoSubmitRecord
from pyrung.core.state import SystemState
from pyrung.core.tag import TagType

_CMP = {"==": eq, "!=": ne, "<": lt, "<=": le, ">": gt, ">=": ge}

_DIRECT_ORACLE_CLASSES = frozenset(
    {
        BlockCopyInstruction,
        CalcInstruction,
        CopyInstruction,
        CountDownInstruction,
        CountUpInstruction,
        EventDrumInstruction,
        FillInstruction,
        LatchInstruction,
        ModbusReceiveInstruction,
        OffDelayInstruction,
        OnDelayInstruction,
        OutInstruction,
        PackBitsInstruction,
        PackTextInstruction,
        PackWordsInstruction,
        ResetInstruction,
        SearchInstruction,
        ShiftInstruction,
        UnpackToBitsInstruction,
        UnpackToWordsInstruction,
    }
)
_CONCRETE_ORACLE_FRONTIERS = {
    TimeDrumInstruction: "needs elapsed-time and hidden-step state rows",
}


@dataclass(frozen=True)
class ConcreteCrossingRun:
    """One isolated interpreted occurrence and its concrete transition."""

    occurrence: RungRun
    before: dict[str, Any]
    after: dict[str, Any]

    @property
    def writes(self) -> dict[str, Any]:
        return dict(self.occurrence.writes)


def _one_scan(
    instruction: Any,
    seed: dict[str, Any],
    *,
    conditions: tuple[Any, ...] = (),
    instruction_memory: dict[str, Any] | None = None,
    io_submits: dict[str, IoSubmitRecord] | None = None,
    io_drains: dict[str, IoResultRecord] | None = None,
    verify_compiled: bool = False,
) -> ConcreteCrossingRun:
    """Capture one interpreted occurrence and optionally prove backend parity."""
    rung = Rung(*conditions)
    rung.add_instruction(instruction)
    with Program(strict=False) as program:
        program._add_rung(rung)

    memory = {
        instruction.memory_key(prefix): value
        for prefix, value in (instruction_memory or {}).items()
    }
    initial_state = SystemState().with_memory(memory)
    plc = PLC(
        program,
        initial_state=initial_state,
        record_all_tags=True,
    )
    compiled = CompiledPLC(program, initial_state=initial_state) if verify_compiled else None

    before = dict(plc.current_state.tags)
    before.update(seed)
    plc.patch(seed)
    if compiled is not None:
        compiled.patch(seed)
    if io_submits or io_drains:
        plc._next_scan_replay_io = (io_submits or {}, io_drains or {})

    capture = ConditionViewCapture()
    scan_context, dt = plc._prepare_scan(synthesis_observer=capture)
    execute_program(program, scan_context, capture_rungs=True, observer=capture)
    plc._commit_scan(scan_context, dt)
    if compiled is not None:
        compiled.step()
        for record in (io_submits or {}).values():
            for tag_name, value in record.tag_writes:
                compiled.apply_replay_io_write(tag_name, value)
        for record in (io_drains or {}).values():
            for tag_name, value in record.tag_writes:
                compiled.apply_replay_io_write(tag_name, value)
        if io_submits or io_drains:
            compiled._materialize_replay_state()
        _assert_states_match(plc, compiled)

    assert len(capture.runs) == 1
    return ConcreteCrossingRun(
        occurrence=capture.runs[0],
        before=before,
        after=dict(plc.current_state.tags),
    )


def _assert_states_match(interpreted: PLC, compiled: CompiledPLC) -> None:
    """Apply the same complete-state parity contract as ``runner_factory``."""
    left = interpreted.current_state
    right = compiled.current_state
    assert left.scan_id == right.scan_id
    assert left.timestamp == pytest.approx(right.timestamp)
    assert dict(left.tags) == dict(right.tags)
    assert dict(left.memory) == dict(right.memory)


@pytest.fixture
def one_scan(runner_backend: str) -> Callable[..., ConcreteCrossingRun]:
    """Build the crossing oracle selected by ``--runner-backend``.

    The occurrence journal is an interpreter capability, so every mode captures
    it there. ``compiled`` and ``both`` additionally require the compiled
    runtime to land in the identical complete state.
    """

    def run(
        instruction: Any,
        seed: dict[str, Any],
        *,
        conditions: tuple[Any, ...] = (),
        instruction_memory: dict[str, Any] | None = None,
        io_submits: dict[str, IoSubmitRecord] | None = None,
        io_drains: dict[str, IoResultRecord] | None = None,
    ) -> ConcreteCrossingRun:
        return _one_scan(
            instruction,
            seed,
            conditions=conditions,
            instruction_memory=instruction_memory,
            io_submits=io_submits,
            io_drains=io_drains,
            verify_compiled=runner_backend != "interpreted",
        )

    return run


def _value(view: ConditionView, tag: str) -> Any:
    return view.get_tag(tag)


def _values_match(left: Any, right: Any) -> bool:
    if isinstance(left, float) or isinstance(right, float):
        try:
            return math.isclose(float(left), float(right), rel_tol=1e-7, abs_tol=1e-7)
        except (TypeError, ValueError):
            return False
    return left == right


def _target_value(target: Constraint, tag: str) -> Any:
    if isinstance(target, Eq) and target.tag == tag and len(target.values) == 1:
        return next(iter(target.values))
    raise AssertionError(f"Prior needs a single equality target for {tag}")


def _constraint_holds(
    constraint: Constraint,
    *,
    run: ConcreteCrossingRun,
    target: Constraint,
) -> bool:
    """Evaluate one crossing constraint against the occurrence input state."""
    view = run.occurrence.view
    if isinstance(constraint, Eq):
        return any(_values_match(_value(view, constraint.tag), v) for v in constraint.values)
    if isinstance(constraint, Cmp):
        bound = _value(view, constraint.bound) if constraint.bound_is_tag else constraint.bound
        return bool(_CMP[constraint.op](_value(view, constraint.tag), bound))
    if isinstance(constraint, AffineCmp):
        bound = constraint.scale * _value(view, constraint.bound_tag) + constraint.offset
        return bool(_CMP[constraint.op](_value(view, constraint.tag), bound))
    if isinstance(constraint, Mask):
        return int(_value(view, constraint.tag)) & constraint.mask == constraint.bits
    if isinstance(constraint, Prior):
        wanted = _target_value(target, constraint.tag)
        prior = run.before[constraint.source]
        return _values_match(wanted, constraint.scale * prior + constraint.offset)
    if isinstance(constraint, CondAttr):
        return run.occurrence.enabled is constraint.expected
    if isinstance(constraint, Quant):
        bound = _value(view, constraint.value) if constraint.value_is_tag else constraint.value
        matches = [bool(_CMP[constraint.op](_value(view, tag), bound)) for tag in constraint.block]
        return any(matches) if constraint.kind == "exists" else all(matches)
    if isinstance(constraint, External):
        # An external leaf deliberately imposes no local predecessor constraint.
        return True
    raise AssertionError(f"unsupported oracle constraint: {constraint!r}")


def _reverse_contains(
    result: ReverseResult,
    *,
    run: ConcreteCrossingRun,
    target: Constraint,
) -> bool:
    assert not result.fallthrough
    return any(
        all(_constraint_holds(c, run=run, target=target) for c in branch)
        for branch in result.branches
    )


def _assert_forward_sound(
    instruction: Any,
    target_tag: str,
    run: ConcreteCrossingRun,
    ctx: CrossingContext,
) -> None:
    """Assert every non-UNKNOWN forward claim matches the attempted write."""
    actual = run.writes[target_tag]
    claim = crossings.forward(instruction, target_tag, ctx)
    if claim is UNKNOWN:
        return
    if not isinstance(claim, (Literal, Affine, Aggregate)):
        raise AssertionError(f"unsupported forward claim: {claim!r}")
    source_names = (
        (claim.source,)
        if isinstance(claim, Affine)
        else claim.tags
        if isinstance(claim, Aggregate)
        else ()
    )
    predicted = evaluate_forward(
        claim,
        {name: _value(run.occurrence.view, name) for name in source_names},
    )
    assert predicted is not UNKNOWN
    assert _values_match(actual, predicted), (
        f"{type(instruction).__name__} claimed {target_tag}={predicted!r}, "
        f"but the interpreter wrote {actual!r}"
    )


def _assert_reverse_sound(
    instruction: Any,
    target: Constraint,
    run: ConcreteCrossingRun,
    ctx: CrossingContext,
) -> None:
    """Check exact equivalence or inexact-preimage containment."""
    assert isinstance(target, (Eq, Cmp))
    # Stateful writers represent both their active write and inactive hold
    # transition. The hold path has no attempted write, so its concrete output
    # is the post-scan value.
    actual = run.writes.get(target.tag, run.after[target.tag])
    if isinstance(target, Eq):
        produced_target = any(_values_match(actual, value) for value in target.values)
    else:
        bound = _value(run.occurrence.view, target.bound) if target.bound_is_tag else target.bound
        produced_target = bool(_CMP[target.op](actual, bound))

    result = crossings.reverse(instruction, run.occurrence.rung, target, ctx)
    if result.fallthrough:
        return
    contains = _reverse_contains(result, run=run, target=target)
    if result.exact:
        assert produced_target == contains
    else:
        assert not produced_target or contains


def test_concrete_oracle_catalog_covers_crossing_registry() -> None:
    """Force every registered crossing into a runtime case or named frontier."""
    frontiers = frozenset(_CONCRETE_ORACLE_FRONTIERS)
    assert _DIRECT_ORACLE_CLASSES.isdisjoint(frontiers)
    assert _DIRECT_ORACLE_CLASSES | frontiers == crossings.registered_classes()


def _receive_instruction() -> ModbusReceiveInstruction:
    return ModbusReceiveInstruction(
        target_name="deterministic-peer",
        bank="DS",
        start=1,
        addresses=(1,),
        dest=Int("InboundValue"),
        receiving=Bool("RequestActive"),
        success=Bool("RequestSucceeded"),
        error=Bool("RequestFailed"),
        exception_response=Int("RequestCode"),
    )


def test_receive_replay_payload_is_external_but_status_writes_are_local(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    instruction = _receive_instruction()
    drain = IoResultRecord(
        ok=True,
        exception_code=0,
        values=(321,),
        tag_writes=(
            ("InboundValue", 321),
            ("RequestActive", False),
            ("RequestSucceeded", True),
            ("RequestFailed", False),
            ("RequestCode", 0),
        ),
    )
    run = one_scan(
        instruction,
        {
            "InboundValue": 0,
            "RequestActive": True,
            "RequestSucceeded": False,
            "RequestFailed": False,
            "RequestCode": 0,
        },
        io_drains={instruction._io_key: drain},
    )

    assert run.writes == dict(drain.tag_writes)
    assert instruction.external_payload_names == frozenset({"InboundValue"})
    payload = crossings.reverse(
        instruction,
        run.occurrence.rung,
        eq_target("InboundValue", 321),
        CrossingContext(),
    )
    assert payload == ReverseResult(
        branches=((External("InboundValue"),),),
        exact=True,
    )
    for status_name, status_value in drain.tag_writes[1:]:
        assert crossings.reverse(
            instruction,
            run.occurrence.rung,
            eq_target(status_name, status_value),
            CrossingContext(),
        ).fallthrough


def test_receive_replay_submit_locally_sets_request_status(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    instruction = _receive_instruction()
    submit = IoSubmitRecord(
        tag_writes=(
            ("RequestActive", True),
            ("RequestSucceeded", False),
            ("RequestFailed", False),
            ("RequestCode", 0),
        )
    )
    run = one_scan(
        instruction,
        {
            "InboundValue": 17,
            "RequestActive": False,
            "RequestSucceeded": True,
            "RequestFailed": True,
            "RequestCode": 9,
        },
        io_submits={instruction._io_key: submit},
    )

    assert "InboundValue" not in run.writes
    assert run.writes == dict(submit.tag_writes)
    assert crossings.reverse(
        instruction,
        run.occurrence.rung,
        eq_target("RequestActive", True),
        CrossingContext(),
    ).fallthrough


def test_literal_copy_forward_uses_stored_clamp_value(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    dest = Int("Output")
    instruction = CopyInstruction(40_000, dest)
    run = one_scan(instruction, {"Output": 0})

    assert run.writes["Output"] == 32_767
    _assert_forward_sound(
        instruction,
        "Output",
        run,
        CrossingContext(tags_by_name={"Output": dest}),
    )


def test_literal_copy_reverse_uses_stored_clamp_value(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    dest = Int("Output")
    instruction = CopyInstruction(40_000, dest)
    run = one_scan(instruction, {"Output": 0})
    ctx = CrossingContext(tags_by_name={"Output": dest})

    _assert_reverse_sound(instruction, eq_target("Output", 32_767), run, ctx)
    _assert_reverse_sound(instruction, eq_target("Output", 40_000), run, ctx)


def test_copy_clamp_inequality_reverse_contains_over_rail_source(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    source = Dint("Input")
    dest = Int("Output")
    instruction = CopyInstruction(source, dest)
    run = one_scan(instruction, {"Input": 40_000, "Output": 0})

    assert run.writes["Output"] == 32_767
    _assert_forward_sound(
        instruction,
        "Output",
        run,
        CrossingContext(tags_by_name={"Input": source, "Output": dest}),
    )
    _assert_reverse_sound(
        instruction,
        Cmp("Output", "<=", 32_767),
        run,
        CrossingContext(tags_by_name={"Input": source, "Output": dest}),
    )
    _assert_reverse_sound(
        instruction,
        eq_target("Output", 40_000),
        run,
        CrossingContext(tags_by_name={"Input": source, "Output": dest}),
    )


@pytest.mark.parametrize(
    "source_value",
    [-40_000, -32_768, -32_767, -1, 0, 1, 32_766, 32_767, 40_000],
)
def test_narrowing_copy_matches_both_runtimes_across_clamp_boundaries(
    one_scan: Callable[..., ConcreteCrossingRun],
    source_value: int,
) -> None:
    source = Dint("Input")
    dest = Int("Output")
    instruction = CopyInstruction(source, dest)
    run = one_scan(instruction, {"Input": source_value, "Output": 17})
    ctx = CrossingContext(tags_by_name={"Input": source, "Output": dest})

    _assert_forward_sound(instruction, "Output", run, ctx)
    _assert_reverse_sound(instruction, eq_target("Output", run.writes["Output"]), run, ctx)


def test_copy_affine_forward_models_expression_clamp(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    source = Int("Input")
    dest = Int("Output")
    instruction = CopyInstruction(source + 100, dest)
    run = one_scan(instruction, {"Input": 32_767, "Output": 0})
    ctx = CrossingContext(tags_by_name={"Input": source, "Output": dest})

    assert run.writes["Output"] == 32_767
    claim = crossings.forward(instruction, "Output", ctx)
    assert isinstance(claim, Affine)
    assert claim.storage.kind == "clamp"
    _assert_forward_sound(instruction, "Output", run, ctx)


def test_named_char_and_bool_copy_forward_preserves_raw_source(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    char_source, char_dest = Char("CharInput"), Char("CharOutput")
    char_copy = CopyInstruction(char_source, char_dest)
    char_run = one_scan(char_copy, {"CharInput": "Q", "CharOutput": "\x00"})
    char_ctx = CrossingContext(tags_by_name={"CharInput": char_source, "CharOutput": char_dest})
    assert char_run.writes["CharOutput"] == "Q"
    _assert_forward_sound(char_copy, "CharOutput", char_run, char_ctx)

    bool_source, bool_dest = Bool("BoolInput"), Bool("BoolOutput")
    bool_copy = CopyInstruction(bool_source, bool_dest)
    bool_run = one_scan(bool_copy, {"BoolInput": True, "BoolOutput": False})
    bool_ctx = CrossingContext(tags_by_name={"BoolInput": bool_source, "BoolOutput": bool_dest})
    assert bool_run.writes["BoolOutput"] is True
    _assert_forward_sound(bool_copy, "BoolOutput", bool_run, bool_ctx)


def test_fill_middle_cell_matches_runtime_and_reverse_preimage(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    source = Dint("Input")
    outputs = Block("Outputs", TagType.INT, 1, 3)
    instruction = FillInstruction(source, outputs.select(1, 3))
    tags = {tag.name: tag for tag in outputs.select(1, 3).tags()}
    run = one_scan(
        instruction,
        {"Input": 40_000, "Outputs1": 0, "Outputs2": 0, "Outputs3": 0},
    )
    ctx = CrossingContext(tags_by_name={"Input": source, **tags})

    assert run.writes["Outputs2"] == 32_767
    _assert_forward_sound(instruction, "Outputs2", run, ctx)
    _assert_reverse_sound(instruction, eq_target("Outputs2", 32_767), run, ctx)


def test_block_copy_middle_cell_matches_runtime_and_reverse_preimage(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    inputs = Block("Inputs", TagType.DINT, 1, 3)
    outputs = Block("Outputs", TagType.INT, 1, 3)
    instruction = BlockCopyInstruction(inputs.select(1, 3), outputs.select(1, 3))
    tags = {tag.name: tag for tag in (*inputs.select(1, 3).tags(), *outputs.select(1, 3).tags())}
    run = one_scan(
        instruction,
        {
            "Inputs1": 1,
            "Inputs2": 40_000,
            "Inputs3": 3,
            "Outputs1": 0,
            "Outputs2": 0,
            "Outputs3": 0,
        },
    )
    ctx = CrossingContext(tags_by_name=tags)

    assert run.writes["Outputs2"] == 32_767
    _assert_reverse_sound(instruction, eq_target("Outputs2", 32_767), run, ctx)


def test_pack_bits_exact_reverse_matches_runtime(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    bits = Block("Bits", TagType.BOOL, 1, 3)
    output = Int("Output")
    instruction = PackBitsInstruction(bits.select(1, 3), output)
    run = one_scan(
        instruction,
        {"Bits1": True, "Bits2": False, "Bits3": True, "Output": 0},
    )

    assert run.writes["Output"] == 5
    _assert_reverse_sound(
        instruction,
        eq_target("Output", 5),
        run,
        CrossingContext(
            tags_by_name={
                **{tag.name: tag for tag in bits.select(1, 3).tags()},
                "Output": output,
            }
        ),
    )


def test_pack_words_exact_reverse_matches_runtime(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    words = Block("Words", TagType.INT, 1, 2)
    output = Dint("Output")
    instruction = PackWordsInstruction(words.select(1, 2), output)
    run = one_scan(
        instruction,
        {"Words1": 0x1234, "Words2": -1, "Output": 0},
    )

    assert run.writes["Output"] == -60_876
    _assert_reverse_sound(
        instruction,
        eq_target("Output", -60_876),
        run,
        CrossingContext(
            tags_by_name={
                **{tag.name: tag for tag in words.select(1, 2).tags()},
                "Output": output,
            }
        ),
    )


def test_unpack_bit_exact_reverse_matches_runtime(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    source = Int("Input")
    bits = Block("Bits", TagType.BOOL, 1, 8)
    instruction = UnpackToBitsInstruction(source, bits.select(1, 8))
    run = one_scan(
        instruction,
        {"Input": 0x25, **{f"Bits{i}": False for i in range(1, 9)}},
    )

    assert run.writes["Bits6"] is True
    _assert_reverse_sound(
        instruction,
        eq_target("Bits6", True),
        run,
        CrossingContext(
            tags_by_name={
                "Input": source,
                **{tag.name: tag for tag in bits.select(1, 8).tags()},
            }
        ),
    )


def test_unpack_word_exact_reverse_matches_runtime(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    source = Dint("Input")
    words = Block("Words", TagType.WORD, 1, 2)
    instruction = UnpackToWordsInstruction(source, words.select(1, 2))
    run = one_scan(
        instruction,
        {"Input": 0x56781234, "Words1": 0, "Words2": 0},
    )

    assert run.writes["Words2"] == 0x5678
    _assert_reverse_sound(
        instruction,
        eq_target("Words2", 0x5678),
        run,
        CrossingContext(
            tags_by_name={
                "Input": source,
                **{tag.name: tag for tag in words.select(1, 2).tags()},
            }
        ),
    )


def test_pack_text_named_fallthrough_matches_both_runtimes(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    chars = Block("Chars", TagType.CHAR, 1, 2)
    output = Int("Output")
    instruction = PackTextInstruction(chars.select(1, 2), output)
    run = one_scan(
        instruction,
        {"Chars1": "4", "Chars2": "2", "Output": 0},
    )

    assert run.writes["Output"] == 42
    assert crossings.reverse(
        instruction,
        run.occurrence.rung,
        eq_target("Output", 42),
        CrossingContext(
            tags_by_name={
                **{tag.name: tag for tag in chars.select(1, 2).tags()},
                "Output": output,
            }
        ),
    ).fallthrough


def test_real_copy_to_integer_interior_has_no_singleton_reverse(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    source = Real("Input")
    dest = Int("Output")
    instruction = CopyInstruction(source, dest)
    run = one_scan(instruction, {"Input": 7.75, "Output": 0})
    ctx = CrossingContext(tags_by_name={"Input": source, "Output": dest})

    assert run.writes["Output"] == 7
    result = crossings.reverse(instruction, run.occurrence.rung, eq_target("Output", 7), ctx)
    assert result.fallthrough


def test_wrapped_calc_reverse_does_not_drop_alias(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    source = Int("Input")
    dest = Int("Output")
    instruction = CalcInstruction(source * 2, dest)
    run = one_scan(instruction, {"Input": -32_768, "Output": 1})

    assert run.writes["Output"] == 0
    _assert_reverse_sound(
        instruction,
        eq_target("Output", 0),
        run,
        CrossingContext(tags_by_name={"Input": source, "Output": dest}),
    )
    _assert_reverse_sound(
        instruction,
        eq_target("Output", 40_000),
        run,
        CrossingContext(tags_by_name={"Input": source, "Output": dest}),
    )


def test_wrapped_calc_forward_models_expression_wrap(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    source = Int("Input")
    dest = Int("Output")
    instruction = CalcInstruction(source + 1, dest)
    run = one_scan(instruction, {"Input": 32_767, "Output": 0})
    ctx = CrossingContext(tags_by_name={"Input": source, "Output": dest})

    assert run.writes["Output"] == -32_768
    claim = crossings.forward(instruction, "Output", ctx)
    assert isinstance(claim, Affine)
    assert claim.storage.kind == "wrap"
    _assert_forward_sound(instruction, "Output", run, ctx)


@pytest.mark.parametrize(
    "source_value",
    [-32_768, -32_767, -1, 0, 1, 32_766, 32_767],
)
def test_affine_calc_matches_both_runtimes_across_wrap_boundaries(
    one_scan: Callable[..., ConcreteCrossingRun],
    source_value: int,
) -> None:
    source = Int("Input")
    dest = Int("Output")
    instruction = CalcInstruction(source + 1, dest)
    run = one_scan(instruction, {"Input": source_value, "Output": 17})
    ctx = CrossingContext(tags_by_name={"Input": source, "Output": dest})

    _assert_forward_sound(instruction, "Output", run, ctx)
    _assert_reverse_sound(instruction, eq_target("Output", run.writes["Output"]), run, ctx)


def test_aggregate_calc_forward_models_sum_wrap(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    values = Block("Values", TagType.INT, 1, 3)
    dest = Int("Output")
    instruction = CalcInstruction(values.select(1, 3).sum(), dest)
    tags = {tag.name: tag for tag in values.select(1, 3).tags()}
    run = one_scan(
        instruction,
        {"Values1": 20_000, "Values2": 20_000, "Values3": 20_000, "Output": 0},
    )
    ctx = CrossingContext(tags_by_name={**tags, "Output": dest})

    assert run.writes["Output"] == -5_536
    claim = crossings.forward(instruction, "Output", ctx)
    assert isinstance(claim, Aggregate)
    assert claim.storage.kind == "wrap"
    _assert_forward_sound(instruction, "Output", run, ctx)


def test_wrapped_calc_inequality_reverse_does_not_drop_wrap_interval(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    source = Int("Input")
    dest = Int("Output")
    instruction = CalcInstruction(source + 1, dest)
    run = one_scan(instruction, {"Input": 32_767, "Output": 0})

    assert run.writes["Output"] == -32_768
    _assert_reverse_sound(
        instruction,
        Cmp("Output", "<", 0),
        run,
        CrossingContext(tags_by_name={"Input": source, "Output": dest}),
    )


def test_two_tag_calc_inequality_does_not_freeze_stale_partners(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    left = Dint("Left")
    right = Dint("Right")
    dest = Real("Output")
    instruction = CalcInstruction(left + right, dest)
    run = one_scan(
        instruction,
        {"Left": 6, "Right": 6, "Output": 0.0},
    )

    assert run.writes["Output"] == 12.0
    _assert_reverse_sound(
        instruction,
        Cmp("Output", ">", 10),
        run,
        CrossingContext(
            snapshot={"Left": 0, "Right": 0},
            tags_by_name={"Left": left, "Right": right, "Output": dest},
        ),
    )


def test_multichar_text_search_reverse_does_not_use_element_quantifier(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    chars = Block("Chars", TagType.CHAR, 1, 6)
    result = Int("MatchAddress")
    found = Bool("MatchFound")
    instruction = SearchInstruction(
        chars.select(1, 6) == "BC",
        result=result,
        found=found,
    )
    run = one_scan(
        instruction,
        {
            "Chars1": "A",
            "Chars2": "B",
            "Chars3": "C",
            "Chars4": "D",
            "Chars5": "E",
            "Chars6": "F",
            "MatchAddress": 0,
            "MatchFound": False,
        },
    )

    assert run.writes["MatchFound"] is True
    _assert_reverse_sound(
        instruction,
        eq_target("MatchFound", True),
        run,
        CrossingContext(
            tags_by_name={
                **{tag.name: tag for tag in chars.select(1, 6).tags()},
                "MatchAddress": result,
                "MatchFound": found,
            }
        ),
    )


def test_scalar_search_quantifier_contains_concrete_match(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    values = Block("Values", TagType.INT, 1, 3)
    result = Int("MatchAddress")
    found = Bool("MatchFound")
    instruction = SearchInstruction(
        values.select(1, 3) >= 10,
        result=result,
        found=found,
    )
    run = one_scan(
        instruction,
        {
            "Values1": 2,
            "Values2": 11,
            "Values3": 3,
            "MatchAddress": 0,
            "MatchFound": False,
        },
    )

    assert run.writes["MatchFound"] is True
    _assert_reverse_sound(
        instruction,
        eq_target("MatchFound", True),
        run,
        CrossingContext(
            tags_by_name={
                **{tag.name: tag for tag in values.select(1, 3).tags()},
                "MatchAddress": result,
                "MatchFound": found,
            }
        ),
    )


def test_latch_false_requires_inactive_rung_as_well_as_prior_false(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    enable = Bool("Enable")
    target = Bool("State")
    instruction = LatchInstruction(target)
    run = one_scan(
        instruction,
        {"Enable": True, "State": False},
        conditions=(enable,),
    )

    assert run.writes["State"] is True
    _assert_reverse_sound(
        instruction,
        eq_target("State", False),
        run,
        CrossingContext(tags_by_name={"Enable": enable, "State": target}),
    )


def test_reset_nonreset_requires_inactive_rung_as_well_as_prior_value(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    enable = Bool("Enable")
    target = Bool("State")
    instruction = ResetInstruction(target)
    run = one_scan(
        instruction,
        {"Enable": True, "State": True},
        conditions=(enable,),
    )

    assert run.writes["State"] is False
    _assert_reverse_sound(
        instruction,
        eq_target("State", True),
        run,
        CrossingContext(tags_by_name={"Enable": enable, "State": target}),
    )


@pytest.mark.parametrize("kind", ["out", "latch", "reset"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("prior", [False, True])
def test_boolean_crossings_are_exhaustive_over_enable_and_prior_state(
    one_scan: Callable[..., ConcreteCrossingRun],
    kind: str,
    enabled: bool,
    prior: bool,
) -> None:
    enable = Bool("Enable")
    target = Bool("State")
    instruction = {
        "out": OutInstruction,
        "latch": LatchInstruction,
        "reset": ResetInstruction,
    }[kind](target)
    run = one_scan(
        instruction,
        {"Enable": enabled, "State": prior},
        conditions=(enable,),
    )
    ctx = CrossingContext(tags_by_name={"Enable": enable, "State": target})

    for target_value in (False, True):
        _assert_reverse_sound(instruction, eq_target("State", target_value), run, ctx)


def test_count_up_reverse_contains_same_scan_completion_frontier(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    enable = Bool("Enable")
    reset = Bool("Clear")
    done = Bool("Complete")
    accumulator = Dint("Accumulator")
    instruction = CountUpInstruction(done, accumulator, 10, enable, reset)
    run = one_scan(
        instruction,
        {"Enable": True, "Clear": False, "Complete": False, "Accumulator": 9},
    )

    assert run.writes["Accumulator"] == 10
    assert run.writes["Complete"] is True
    _assert_reverse_sound(
        instruction,
        eq_target("Complete", True),
        run,
        CrossingContext(
            tags_by_name={
                "Enable": enable,
                "Clear": reset,
                "Complete": done,
                "Accumulator": accumulator,
            }
        ),
    )


def test_count_down_reverse_contains_same_scan_completion_frontier(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    enable = Bool("Enable")
    reset = Bool("Clear")
    done = Bool("Complete")
    accumulator = Dint("Accumulator")
    instruction = CountDownInstruction(done, accumulator, 5, enable, reset)
    run = one_scan(
        instruction,
        {"Enable": True, "Clear": False, "Complete": False, "Accumulator": -4},
    )

    assert run.writes["Accumulator"] == -5
    assert run.writes["Complete"] is True
    _assert_reverse_sound(
        instruction,
        eq_target("Complete", True),
        run,
        CrossingContext(
            tags_by_name={
                "Enable": enable,
                "Clear": reset,
                "Complete": done,
                "Accumulator": accumulator,
            }
        ),
    )


def test_dynamic_counter_preset_preserves_affine_bound_preimage(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    enable = Bool("Enable")
    reset = Bool("Clear")
    done = Bool("Complete")
    accumulator = Dint("Accumulator")
    preset = Dint("Preset")
    instruction = CountUpInstruction(done, accumulator, preset, enable, reset)
    run = one_scan(
        instruction,
        {
            "Enable": True,
            "Clear": False,
            "Complete": False,
            "Accumulator": 9,
            "Preset": 10,
        },
    )

    assert run.writes["Complete"] is True
    _assert_reverse_sound(
        instruction,
        eq_target("Complete", True),
        run,
        CrossingContext(
            tags_by_name={
                "Enable": enable,
                "Clear": reset,
                "Complete": done,
                "Accumulator": accumulator,
                "Preset": preset,
            }
        ),
    )


def test_on_delay_completion_falls_through_without_elapsed_time_constraint(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    enable = Bool("Enable")
    done = Bool("Complete")
    accumulator = Int("Accumulator")
    instruction = OnDelayInstruction(done, accumulator, 10, enable, unit="ms")
    run = one_scan(
        instruction,
        {"Enable": True, "Complete": False, "Accumulator": 0},
    )

    assert run.writes["Accumulator"] == 10
    assert run.writes["Complete"] is True
    result = crossings.reverse(
        instruction,
        run.occurrence.rung,
        eq_target("Complete", True),
        CrossingContext(
            tags_by_name={
                "Enable": enable,
                "Complete": done,
                "Accumulator": accumulator,
            }
        ),
    )
    assert result.fallthrough


def test_off_delay_named_fallthrough_matches_both_runtimes(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    enable = Bool("Enable")
    done = Bool("Complete")
    accumulator = Int("Accumulator")
    instruction = OffDelayInstruction(done, accumulator, 10, enable, unit="ms")
    run = one_scan(
        instruction,
        {"Enable": True, "Complete": False, "Accumulator": 0},
    )

    assert run.writes["Accumulator"] == 0
    assert run.writes["Complete"] is True
    assert crossings.reverse(
        instruction,
        run.occurrence.rung,
        eq_target("Complete", True),
        CrossingContext(
            tags_by_name={
                "Enable": enable,
                "Complete": done,
                "Accumulator": accumulator,
            }
        ),
    ).fallthrough


def test_shift_held_branch_is_not_exact_during_clock_edge(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    bits = Block("Bits", TagType.BOOL, 1, 3)
    data = Bool("Data")
    clock = Bool("Clock")
    reset = Bool("Clear")
    instruction = ShiftInstruction(
        bits.select(1, 3),
        data,
        clock,
        reset,
    )
    run = one_scan(
        instruction,
        {
            "Bits1": False,
            "Bits2": True,
            "Bits3": False,
            "Data": False,
            "Clock": True,
            "Clear": False,
        },
    )

    assert run.writes["Bits2"] is False
    _assert_reverse_sound(
        instruction,
        eq_target("Bits2", True),
        run,
        CrossingContext(
            tags_by_name={
                **{tag.name: tag for tag in bits.select(1, 3).tags()},
                "Data": data,
                "Clock": clock,
                "Clear": reset,
            }
        ),
    )


def test_drum_reverse_does_not_drop_same_scan_step_advance(
    one_scan: Callable[..., ConcreteCrossingRun],
) -> None:
    output = Bool("Output")
    event_one = Bool("EventOne")
    event_two = Bool("EventTwo")
    current_step = Int("CurrentStep")
    complete = Bool("Complete")
    automatic = Bool("Automatic")
    reset = Bool("Clear")
    instruction = EventDrumInstruction(
        [output],
        [event_one, event_two],
        [[False], [True]],
        current_step,
        complete,
        automatic,
        reset,
    )
    run = one_scan(
        instruction,
        {
            "Output": False,
            "EventOne": True,
            "EventTwo": False,
            "CurrentStep": 1,
            "Complete": False,
            "Automatic": True,
            "Clear": False,
        },
        instruction_memory={
            "_drum_last_step": 1,
            "_drum_event_ready": True,
            "_drum_event_prev": False,
        },
    )

    assert run.writes["CurrentStep"] == 2
    assert run.writes["Output"] is True
    _assert_reverse_sound(
        instruction,
        eq_target("Output", True),
        run,
        CrossingContext(
            tags_by_name={
                "Output": output,
                "EventOne": event_one,
                "EventTwo": event_two,
                "CurrentStep": current_step,
                "Complete": complete,
                "Automatic": automatic,
                "Clear": reset,
            }
        ),
    )
