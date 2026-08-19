"""Own the lifecycle of replay-confirmed corrective overlays.

This module installs, promotes, contradicts, causally revokes, and symmetrically
rebases correction-owned PilotRungs across checkpoints. It does not investigate
an incident, choose a correction, or restore the recovery origin.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from pyrsistent import pvector

from pyrung.core.analysis.pilot.investigate import (
    InvestigationResult,
    RegressionWitness,
    correction_identity,
)
from pyrung.core.analysis.pilot.overlay import (
    PilotRung,
    _merged_pilot_rungs,
    _pilot_rung_execution_receipt,
    fork_with_pilot_rungs,
)
from pyrung.core.analysis.pilot.recovery import assert_recovery_disposable_state
from pyrung.core.analysis.pilot.types import (
    CorrectionStatus,
    _ConfirmedCorrection,
    _CorrectionReceipt,
    _HoldLogEntry,
    _PilotState,
)
from pyrung.core.analysis.pilot.world import _Checkpoint
from pyrung.core.analysis.pilot.world_key import (
    _pilot_world_key,
    _rung_identity,
    _semantic_key,
)
from pyrung.core.analysis.sp_values import _values_match


def _install_confirmed_correction(
    state: _PilotState,
    correction: _ConfirmedCorrection,
    *,
    origin_key: tuple[Any, ...],
    scan: int,
    source: str,
    adopt_existing: bool = False,
) -> _CorrectionReceipt:
    """Install one locally replay-proven correction on probation.

    A correction is installed only in the exact guarded form that survived
    replay, and only one competing explanation is installed for an incident.
    The checks below reject forged identities and already-owned rungs;
    prerequisite installation reuses an identical rung without claiming it.
    Installation banks active corrections into every revert anchor, and
    revocation removes them symmetrically.
    """
    assert_recovery_disposable_state(state, "install a correction")
    if not correction.pilot_rungs:
        raise ValueError("a confirmed correction must own at least one rung")
    if any(not isinstance(rung, PilotRung) for rung in correction.pilot_rungs):
        raise TypeError("a confirmed correction may contain only executable PilotRungs")
    if correction.identity != correction_identity(correction.pilot_rungs):
        raise ValueError("confirmed correction identity does not match its replayed rungs")
    correction_rung_ids = tuple(_rung_identity(rung) for rung in correction.pilot_rungs)
    if len(set(correction_rung_ids)) != len(correction_rung_ids):
        raise ValueError("a confirmed correction cannot contain duplicate rungs")
    exact_owner = next(
        (
            receipt
            for receipt in state.correction_receipts
            if receipt.status.effective and receipt.identity == correction.identity
        ),
        None,
    )
    if exact_owner is not None:
        # Re-observing the incident can reconfirm the correction before its
        # first corrected continuation has made progress.  That is evidence
        # for the existing owner, not authority to append another owner (or
        # another hold-log installation) for the same executable rungs.
        if not all(rung in state.pilot_rungs for rung in exact_owner.pilot_rungs):
            raise RuntimeError("effective correction receipt has lost its owned rung(s)")
        admitted_origins = exact_owner.admitted_origins or frozenset((exact_owner.origin_key,))
        if origin_key not in admitted_origins:
            exact_owner = replace(
                exact_owner,
                admitted_origins=admitted_origins | frozenset((origin_key,)),
            )
            state.correction_receipts = [
                exact_owner if receipt.receipt_id == exact_owner.receipt_id else receipt
                for receipt in state.correction_receipts
            ]
        return exact_owner
    existing = {_rung_identity(rung) for rung in state.pilot_rungs}
    duplicate = tuple(rung for rung in correction.pilot_rungs if _rung_identity(rung) in existing)
    if duplicate and not (adopt_existing and len(duplicate) == len(correction.pilot_rungs)):
        raise ValueError(
            "confirmed correction cannot claim already-owned rung(s): "
            f"{tuple((rung.dest, rung.value) for rung in duplicate)!r}"
        )
    if not duplicate:
        state.pilot_rungs = _merged_pilot_rungs(correction.pilot_rungs, state.pilot_rungs)
    state.hold_log.append(
        _HoldLogEntry(
            scan=scan,
            source=source,
            pilot_rungs=correction.pilot_rungs,
        )
    )
    receipt = _CorrectionReceipt(
        receipt_id=(
            max(
                (existing.receipt_id for existing in state.correction_receipts),
                default=0,
            )
            + 1
        ),
        origin_key=origin_key,
        correction=correction,
        admitted_origins=frozenset((origin_key,)),
    )
    state.correction_receipts.append(receipt)
    key_config = state.key_config
    banked: list[_Checkpoint] = []
    for checkpoint in state.checkpoints:
        existing_ids = {_rung_identity(rung) for rung in checkpoint.world.pilot_rungs}
        checkpoint_pilot_rungs = [*checkpoint.world.pilot_rungs]
        checkpoint_pilot_rungs.extend(
            rung for rung in correction.pilot_rungs if _rung_identity(rung) not in existing_ids
        )
        banked.append(
            _checkpoint_with_pilot_rungs(
                checkpoint,
                checkpoint_pilot_rungs,
                key_config,
                state.active_requirements,
            )
        )
    state.checkpoints = banked
    return receipt


def _promote_probationary_corrections(state: _PilotState) -> tuple[int, ...]:
    """Promote installed hypotheses after the live run banks real progress."""
    promoted = tuple(
        receipt.receipt_id
        for receipt in state.correction_receipts
        if receipt.status is CorrectionStatus.PROBATIONARY
    )
    if promoted:
        promoted_ids = set(promoted)
        state.correction_receipts = [
            replace(receipt, status=CorrectionStatus.ACTIVE)
            if receipt.receipt_id in promoted_ids
            else receipt
            for receipt in state.correction_receipts
        ]
    return promoted


def _checkpoint_with_pilot_rungs(
    checkpoint: _Checkpoint,
    pilot_rungs: list[PilotRung],
    key_config: Any,
    active_requirements: Any = (),
) -> _Checkpoint:
    """Return one checkpoint re-keyed around an exact executable overlay."""
    if tuple(pilot_rungs) == tuple(checkpoint.world.pilot_rungs):
        return checkpoint
    work = fork_with_pilot_rungs(checkpoint.world.work, pilot_rungs)
    world = checkpoint.world.set(work=work, pilot_rungs=pvector(pilot_rungs))
    key = (
        _pilot_world_key(
            dict(work.state.tags),
            key_config,
            pilot_rungs,
            active_requirements,
        )
        if key_config is not None
        else checkpoint.key
    )
    return replace(checkpoint, key=key, world=world)


def _contradicted_corrections(
    state: _PilotState,
    investigation: InvestigationResult,
    snapshot: Mapping[str, Any],
) -> tuple[_CorrectionReceipt, ...]:
    """Overlay-effective corrections contradicted by the exact new remedy.

    A later hypothesis that causally names an installed destination and needs a
    value outside the correction's admitted values is evidence that the prior
    correction caused this regression.  Treating the opposite value as another
    durable hold would leave two tools arguing in the overlay.
    """
    correction = investigation.correction
    if correction is None:
        return ()
    sources = set(correction.sources)
    remedy_rungs: dict[str, list[tuple[Any, Any]]] = {}
    for rung in correction.pilot_rungs:
        remedy_rungs.setdefault(rung.dest, []).append((rung.value, rung.operation))

    def _compatible_phases(new_operation: Any, old: PilotRung) -> bool:
        """Opposite values with distinct owner boundaries are temporal phases."""
        return (
            new_operation is not None
            and old.operation is not None
            and _semantic_key(new_operation.until) != _semantic_key(old.operation.until)
        )

    effective_ids = {
        _rung_identity(rung)
        for rung in _pilot_rung_execution_receipt(state.pilot_rungs, snapshot).effective
    }
    contradicted: list[_CorrectionReceipt] = []
    for receipt in state.correction_receipts:
        if not receipt.status.effective:
            continue
        admitted: dict[str, list[PilotRung]] = {}
        for rung in receipt.pilot_rungs:
            if _rung_identity(rung) in effective_ids:
                admitted.setdefault(rung.dest, []).append(rung)
        if any(
            tag in sources
            and all(
                not any(
                    _values_match(remedy_value, old.value)
                    or _compatible_phases(remedy_operation, old)
                    for old in admitted.get(tag, ())
                )
                for remedy_value, remedy_operation in rungs
            )
            for tag, rungs in remedy_rungs.items()
            if tag in admitted
        ):
            contradicted.append(receipt)
    return tuple(contradicted)


def _causally_harmful_corrections(
    state: _PilotState,
    witness: RegressionWitness | None,
    snapshot: Mapping[str, Any],
) -> tuple[_CorrectionReceipt, ...]:
    """Effective corrections whose exact active PILOT write caused this incident.

    Banking progress promotes confidence; it does not make a synthetic rung
    immune to later causal testimony. If a later incident's recorded
    ``cause()`` chain contains the exact active write owned by any effective
    correction, the live machine has supplied a counterexample and the rung
    must be removed even when investigation cannot yet name a replacement.

    Match the exact PILOT write (destination and value), then consume the
    execution layer's effective-owner receipt. This avoids blaming a dormant,
    eligible, continuing-but-overridden, or shadowed correction that merely
    mentions the same tag. Guard ownership is evaluated in the
    witness's pre-departure world, not the earlier incident anchor: a delayed
    correction may become active only shortly before it causes harm.
    """
    if witness is None:
        return ()
    causal_values = (
        tuple(
            (occurrence.tag, occurrence.value)
            for occurrence in witness.cause
            if occurrence.rung.subroutine == "PILOT"
        )
        + witness.causal_roots
    )
    if not causal_values:
        return ()

    overlay = _pilot_rung_execution_receipt(
        state.pilot_rungs,
        dict(witness.owner_snapshot or snapshot),
    )
    active_owner = {rung.dest: rung for rung in overlay.effective}

    harmful: list[_CorrectionReceipt] = []
    for receipt in state.correction_receipts:
        if not receipt.status.effective:
            continue
        owns_cause = False
        for rung in receipt.pilot_rungs:
            owner = active_owner.get(rung.dest)
            if owner is None or _rung_identity(owner) != _rung_identity(rung):
                continue
            if any(
                tag == rung.dest and _values_match(value, rung.value)
                for tag, value in causal_values
            ):
                owns_cause = True
                break
        if owns_cause:
            harmful.append(receipt)
    return tuple(harmful)


def _revoke_corrections(
    state: _PilotState,
    receipts: tuple[_CorrectionReceipt, ...],
) -> tuple[int, ...]:
    """Revoke causally harmful receipts and rebuild the checkpoint without them."""
    if not receipts:
        return ()
    receipt_ids = {receipt.receipt_id for receipt in receipts}
    revoked_rung_ids = {
        _rung_identity(rung) for receipt in receipts for rung in receipt.pilot_rungs
    }
    state.correction_receipts = [
        replace(receipt, status=CorrectionStatus.REVOKED)
        if receipt.receipt_id in receipt_ids
        else receipt
        for receipt in state.correction_receipts
    ]
    for receipt in receipts:
        for origin_key in receipt.admitted_origins or frozenset((receipt.origin_key,)):
            state.correction_nogoods.setdefault(origin_key, set()).add(receipt.identity)
        state.hold_log.append(
            _HoldLogEntry(
                scan=state.work.state.scan_id,
                source="revocation",
                pilot_rungs=receipt.pilot_rungs,
            )
        )

    remaining_pilot_rungs = [
        rung for rung in state.pilot_rungs if _rung_identity(rung) not in revoked_rung_ids
    ]
    state.pilot_rungs = remaining_pilot_rungs
    key_config = state.key_config
    cleaned_checkpoints: list[_Checkpoint] = []
    for saved in state.checkpoints:
        saved_pilot_rungs = [
            rung for rung in saved.world.pilot_rungs if _rung_identity(rung) not in revoked_rung_ids
        ]
        cleaned_checkpoints.append(
            _checkpoint_with_pilot_rungs(
                saved,
                saved_pilot_rungs,
                key_config,
                state.active_requirements,
            )
        )
    state.checkpoints = cleaned_checkpoints
    return tuple(sorted(receipt_ids))
