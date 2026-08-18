"""Exact, fail-closed matching of delayed expectation receipts."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest

from pyrung.core.analysis.causal._rung_writes import RungWrite
from pyrung.core.analysis.causal.models import Transition
from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
    EffectObligation,
    obligation_snapshot,
    occurrence_snapshot,
)
from pyrung.core.analysis.pilot.investigation_replay import (
    CausalOccurrence,
    RegressionWitness,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActPolicy,
    ActSource,
    Bearing,
    BearingObjective,
    Pulse,
    TargetSpec,
    act_identity,
)
from pyrung.core.analysis.pilot.progress import _match_regression_expectation_receipt
from pyrung.core.analysis.pilot.requirements import ExpectationReceipt
from pyrung.core.analysis.pilot.types import ChannelMotion, ExecutionReceipt, ExecutionSpan
from pyrung.core.context import RungId
from pyrung.core.executor import WriteOccurrence


class _ReplayOwner:
    def __init__(self, epoch: object) -> None:
        self.epoch = epoch
        self.writes: list[RungWrite] = []
        self._query_runner = SimpleNamespace(
            _replay_rung_write_projection_at=lambda _scan: SimpleNamespace(
                writes=tuple(self.writes)
            )
        )

    def _runner(self):
        return self._query_runner


def _write(
    *,
    call_invocation: int,
    run_order: int = 3,
    ordinal: int = 17,
    rung: object | None = None,
) -> RungWrite:
    run = SimpleNamespace(
        rung=rung if rung is not None else object(),
        kind="call",
        caller_rung=5,
        call_stack=("ReceiptUnit",),
        depth=1,
        enabled=True,
    )
    occurrence = WriteOccurrence(ordinal, "tag", "ReceiptEffect", False, True)
    return RungWrite(
        scan_id=8,
        ordinal=ordinal,
        run_order=run_order,
        call_invocation=call_invocation,
        rung_id=RungId("ReceiptUnit", 2),
        run=cast(Any, run),
        instruction=None,
        occurrence=occurrence,
        transition=Transition("ReceiptEffect", 8, False, True, ordinal),
    )


def _receipt(
    write: RungWrite,
    *,
    epoch: object,
    owner: object,
    source: str,
) -> ExpectationReceipt:
    assert isinstance(owner, _ReplayOwner)
    owner.writes.append(write)
    obligation = EffectObligation(
        tag="ReceiptEffect",
        value=True,
        producer=("ReceiptUnit", 2, ()),
        consumer=(None, 6, (0,)),
        required_shape=(("ReceiptEffect", True), ("ReceiptLatch", True)),
        boundary=("ReceiptEffect", True),
        producer_rung=write.run.rung,
    )
    expectation = EffectExpectation((obligation,))
    act = Pulse(
        ActPolicy(
            source=ActSource.TRACE,
            action_pairs=(("ReceiptCommand", True),),
            applied=(("ReceiptCommand", True),),
            expectation=expectation,
        )
    )
    bearing = Bearing(
        world_key=(source,),
        act=act,
        objective=BearingObjective(TargetSpec("ReceiptTarget", True)),
    )
    checkpoint_owner = object()
    checkpoint = SimpleNamespace(
        owner=checkpoint_owner,
        key=(source,),
        world=SimpleNamespace(work=SimpleNamespace(state=SimpleNamespace(scan_id=7))),
    )
    execution = ExecutionReceipt(
        before_snap={},
        after_snap={},
        channel_motion=ChannelMotion(),
        coast_receipt=None,
        timeline=(),
        spans=(ExecutionSpan(owner=owner, kernel_scan_ids=(write.scan_id,)),),
    )
    receipt = ExpectationReceipt(
        source_world_key=(source,),
        checkpoint_owner=checkpoint_owner,
        act_identity=act_identity(act),
        active_rung_identities=(),
        obligations=(obligation_snapshot(obligation),),
        producer_occurrences=(occurrence_snapshot(write),),
        consumer_occurrences=(),
        execution=execution,
        source_checkpoint=checkpoint,
        local_act=act,
        local_bearing=bearing,
        expectation=expectation,
    )
    return receipt


def _match(receipts, *, occurrence, epoch, owner):
    from pyrung.core.analysis.pilot.requirements import match_expectation_receipt

    return match_expectation_receipt(
        receipts,
        occurrence=occurrence,
        execution_epoch=epoch,
        execution_owner=owner,
    )


def _epoch() -> SimpleNamespace:
    return SimpleNamespace(first_scan=8, last_scan=8)


def test_same_tag_and_rung_with_a_different_dynamic_address_does_not_match() -> None:
    epoch = _epoch()
    owner = _ReplayOwner(epoch)
    recorded = _write(call_invocation=1)
    later_invocation = _write(call_invocation=2)
    receipt = _receipt(recorded, epoch=epoch, owner=owner, source="first")

    assert _match((receipt,), occurrence=later_invocation, epoch=epoch, owner=owner) is None


@pytest.mark.parametrize("foreign_dimension", ["epoch", "owner"])
def test_foreign_epoch_or_execution_owner_does_not_match(foreign_dimension: str) -> None:
    epoch = _epoch()
    owner = _ReplayOwner(epoch)
    write = _write(call_invocation=1)
    receipt = _receipt(write, epoch=epoch, owner=owner, source="first")

    assert (
        _match(
            (receipt,),
            occurrence=write,
            epoch=object() if foreign_dimension == "epoch" else epoch,
            owner=object() if foreign_dimension == "owner" else owner,
        )
        is None
    )


def test_unique_exact_dynamic_occurrence_selects_its_source_receipt() -> None:
    epoch = _epoch()
    owner = _ReplayOwner(epoch)
    selected_write = _write(call_invocation=1)
    sibling_write = _write(call_invocation=2)
    selected = _receipt(selected_write, epoch=epoch, owner=owner, source="selected")
    sibling = _receipt(sibling_write, epoch=epoch, owner=owner, source="sibling")

    matched = _match(
        (sibling, selected),
        occurrence=selected_write,
        epoch=epoch,
        owner=owner,
    )

    assert matched is selected
    assert matched.source_checkpoint is selected.source_checkpoint
    assert matched.local_act is selected.local_act
    assert matched.local_bearing is selected.local_bearing
    assert matched.expectation is selected.expectation


def test_corrected_overlay_bearing_key_may_differ_from_source_checkpoint() -> None:
    epoch = _epoch()
    owner = _ReplayOwner(epoch)
    write = _write(call_invocation=1)
    receipt = _receipt(write, epoch=epoch, owner=owner, source="source")
    corrected_bearing = replace(
        receipt.local_bearing,
        world_key=("corrected-overlay",),
    )
    corrected = replace(receipt, local_bearing=corrected_bearing)

    matched = _match(
        (corrected,),
        occurrence=write,
        epoch=epoch,
        owner=owner,
    )

    assert corrected_bearing.world_key != corrected.source_world_key
    assert corrected.source_checkpoint.key == corrected.source_world_key
    assert matched is corrected
    assert matched.local_bearing is corrected_bearing


def test_equal_epoch_reconstruction_of_the_exact_occurrence_still_matches() -> None:
    epoch = _epoch()
    owner = _ReplayOwner(epoch)
    recorded = _write(call_invocation=1)
    reconstructed = _write(call_invocation=1, rung=recorded.run.rung)
    receipt = _receipt(recorded, epoch=epoch, owner=owner, source="source")

    assert (
        _match(
            (receipt,),
            occurrence=reconstructed,
            epoch=epoch,
            owner=owner,
        )
        is receipt
    )


def test_duplicate_exact_receipts_are_ambiguous() -> None:
    epoch = _epoch()
    owner = _ReplayOwner(epoch)
    write = _write(call_invocation=1)
    first = _receipt(write, epoch=epoch, owner=owner, source="same")
    second = _receipt(write, epoch=epoch, owner=owner, source="same")

    assert _match((first, second), occurrence=write, epoch=epoch, owner=owner) is None


@pytest.mark.parametrize("corruption", ["checkpoint_owner", "source_world"])
def test_receipt_source_identity_must_remain_intact(corruption: str) -> None:
    epoch = _epoch()
    owner = _ReplayOwner(epoch)
    write = _write(call_invocation=1)
    receipt = _receipt(write, epoch=epoch, owner=owner, source="source")
    corrupted = (
        replace(receipt, checkpoint_owner=object())
        if corruption == "checkpoint_owner"
        else replace(receipt, source_world_key=("foreign-source",))
    )

    assert _match((corrupted,), occurrence=write, epoch=epoch, owner=owner) is None


def test_malformed_source_checkpoint_fails_closed() -> None:
    epoch = _epoch()
    owner = _ReplayOwner(epoch)
    write = _write(call_invocation=1)
    receipt = _receipt(write, epoch=epoch, owner=owner, source="source")
    malformed = replace(
        receipt,
        source_checkpoint=SimpleNamespace(owner=receipt.checkpoint_owner),
    )

    assert _match((malformed,), occurrence=write, epoch=epoch, owner=owner) is None


def test_receipt_obligation_shape_must_match_its_local_expectation() -> None:
    epoch = _epoch()
    owner = _ReplayOwner(epoch)
    write = _write(call_invocation=1)
    receipt = _receipt(write, epoch=epoch, owner=owner, source="source")
    obligation = replace(
        receipt.obligations[0],
        consumer=(None, 99, ()),
        required_shape=(("ReceiptEffect", True),),
    )
    corrupted = replace(receipt, obligations=(obligation,))

    assert _match((corrupted,), occurrence=write, epoch=epoch, owner=owner) is None


def test_receipt_static_producer_must_own_the_dynamic_occurrence() -> None:
    epoch = _epoch()
    owner = _ReplayOwner(epoch)
    write = _write(call_invocation=1)
    receipt = _receipt(write, epoch=epoch, owner=owner, source="source")
    wrong_obligation = replace(
        receipt.expectation.obligations[0],
        producer=("DifferentSubroutine", 99, ()),
    )
    wrong_expectation = EffectExpectation((wrong_obligation,))
    wrong_act = replace(
        receipt.local_act,
        policy=replace(receipt.local_act.policy, expectation=wrong_expectation),
    )
    wrong_bearing = replace(receipt.local_bearing, act=wrong_act)
    corrupted = replace(
        receipt,
        act_identity=act_identity(wrong_act),
        obligations=(obligation_snapshot(wrong_obligation),),
        local_act=wrong_act,
        local_bearing=wrong_bearing,
        expectation=wrong_expectation,
    )

    assert _match((corrupted,), occurrence=write, epoch=epoch, owner=owner) is None


def test_progress_handoff_uses_unfiltered_exact_link_without_reading_a_future_suffix() -> None:
    epoch = _epoch()
    owner = _ReplayOwner(epoch)
    write = _write(call_invocation=1)
    receipt = _receipt(write, epoch=epoch, owner=owner, source="source")
    source_link = CausalOccurrence(
        rung=write.rung_id,
        tag=write.transition.tag_name,
        value=write.transition.to_value,
        scan_id=write.scan_id,
        occurrence_ordinal=write.ordinal,
        exact_write=write,
        execution_epoch=epoch,
        execution_owner=owner,
    )
    later_cause = CausalOccurrence(
        rung=RungId(None, 9),
        tag="ReceiptEffect",
        value=False,
        scan_id=12,
        occurrence_ordinal=40,
    )
    witness = RegressionWitness(
        channel_tag="ReceiptEffect",
        source=True,
        departed=False,
        landing=False,
        departure_scan=12,
        cause=(later_cause,),
        causal_spine=frozenset({"ReceiptEffect"}),
        receipt_links=(source_link,),
    )

    class _ReceiptOnlyState:
        expectation_receipts = [receipt]

        def __getattr__(self, name: str):
            raise AssertionError(f"receipt matching read historical future state {name!r}")

    assert _match_regression_expectation_receipt(_ReceiptOnlyState(), witness) is receipt
