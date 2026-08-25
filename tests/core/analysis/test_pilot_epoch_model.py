"""First-class causal epoch ownership and lifecycle contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from pyrung import PLC
from pyrung.core.analysis.pilot.overlay import _set_synth_holds
from pyrung.core.rung import Rung
from pyrung.core.runner import Epoch, EpochQuery, EpochRef


def test_epoch_is_frozen_and_lineage_owns_records_not_plcs() -> None:
    parent = PLC(logic=[])
    parent.run(2)
    child = parent.fork()

    (epoch,) = child._causal_lineage.sealed_epochs
    owner = child._causal_lineage.owner_at(2)

    assert isinstance(epoch, Epoch)
    assert isinstance(owner, EpochQuery)
    assert owner is not parent
    assert owner.epoch is epoch
    with pytest.raises(FrozenInstanceError):
        epoch.last_scan = 3  # ty: ignore[invalid-assignment]


def test_fork_boundary_belongs_to_sealed_epoch_and_child_executes_after_it() -> None:
    parent = PLC(logic=[])
    parent.run(2)
    child = parent.fork()

    assert child._causal_lineage.current_epoch() is None
    assert child._causal_lineage.owner_at(2).epoch.last_scan == 2

    child.step()

    current = child._causal_lineage.current_epoch()
    assert current is not None
    assert (current.first_scan, current.last_scan) == (3, 3)
    assert child._causal_lineage.owner_at(2).epoch is not current
    current_owner = child._causal_lineage.owner_at(3)
    assert (current_owner.epoch.first_scan, current_owner.epoch.last_scan) == (3, 3)


def test_epoch_has_no_snapshot_cache_or_frozen_plc_ancestry() -> None:
    plc = PLC(logic=[])
    plc.step()
    child = plc.fork()

    assert not hasattr(child, "_causal_parent")
    assert not hasattr(child, "_causal_epoch_snapshots")
    assert not hasattr(child, "_is_frozen_causal_owner")


def test_trim_inside_ancestor_slices_sealed_lineage() -> None:
    root = PLC(logic=[])
    root.run(2)
    child = root.fork()
    child.run(2)
    grandchild = child.fork()
    grandchild.step()

    grandchild._trim_history_before(1)

    assert grandchild.history.oldest_scan_id == 1
    assert [
        (epoch.first_scan, epoch.last_scan) for epoch in grandchild._causal_lineage.sealed_epochs
    ] == [
        (1, 2),
        (3, 4),
    ]
    assert tuple(grandchild.history.scan_ids()) == (1, 2, 3, 4, 5)


def test_reboot_discards_sealed_lineage() -> None:
    parent = PLC(logic=[])
    parent.step()
    child = parent.fork()
    assert child._causal_lineage.sealed_epochs

    child.reboot()

    assert not child._causal_lineage.sealed_epochs
    assert child._causal_lineage.current_epoch().first_scan == 0


def test_current_epoch_record_is_reused_at_one_tip_and_replaced_after_scan() -> None:
    plc = PLC(logic=[])
    plc.step()

    current = plc._causal_lineage.current_epoch()

    assert plc._causal_lineage.current_epoch() is current
    assert plc._causal_lineage.owner_at(plc.state.scan_id).epoch is current

    plc.step()

    replacement = plc._causal_lineage.current_epoch()
    assert replacement is not current
    assert replacement.last_scan == 2


def test_epoch_reference_survives_live_reseal_and_fork_inheritance() -> None:
    parent = PLC(logic=[])
    parent.step()
    first = parent._causal_lineage.current_epoch()
    assert first is not None
    assert isinstance(first.reference, EpochRef)

    parent.step()
    extended = parent._causal_lineage.current_epoch()
    assert extended is not None
    assert extended is not first
    assert extended.reference == first.reference

    child = parent.fork()
    (inherited,) = child._causal_lineage.sealed_epochs
    assert inherited.reference == first.reference

    child.step()
    child_epoch = child._causal_lineage.current_epoch()
    assert child_epoch is not None
    assert child_epoch.reference != inherited.reference


def test_hold_change_replaces_live_query_memo_but_preserves_sealed_memo() -> None:
    parent = PLC(logic=[])
    parent.step()
    child = parent.fork()
    child.step()

    lineage = child._causal_lineage
    sealed_epoch = lineage.sealed_epochs[0]
    sealed_owner = lineage.owner_at(sealed_epoch.last_scan)
    live_epoch = lineage.current_epoch()
    live_owner = lineage.owner_at(child.state.scan_id)
    assert sealed_owner is not None
    assert live_epoch is not None
    assert live_owner is not None
    sealed_owner.cause_memo["sealed"] = sealed_epoch
    live_owner.cause_memo["live"] = live_epoch

    _set_synth_holds(child, [Rung()])

    refreshed_live_epoch = lineage.current_epoch()
    refreshed_live_owner = lineage.owner_at(child.state.scan_id)
    assert refreshed_live_epoch is not live_epoch
    assert refreshed_live_owner is not None
    assert refreshed_live_owner is not live_owner
    assert refreshed_live_owner.cause_memo == {}
    assert lineage.owner_at(sealed_epoch.last_scan) is sealed_owner
    assert sealed_owner.cause_memo == {"sealed": sealed_epoch}
