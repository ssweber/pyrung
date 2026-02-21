"""Direct unit tests for InputOverrideManager."""

from __future__ import annotations

import pytest

from pyrung.core import Bool
from pyrung.core.input_overrides import InputOverrideManager


class _CtxProbe:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def set_tags(self, updates: dict[str, object]) -> None:
        self.calls.append(dict(updates))


def _manager(*, read_only: set[str] | None = None) -> InputOverrideManager:
    locked = read_only or set()
    return InputOverrideManager(is_read_only=lambda name: name in locked)


def test_patch_normalizes_tag_keys_and_rejects_read_only() -> None:
    manager = _manager(read_only={"Locked"})
    button = Bool("Button")

    manager.patch({button: True})
    assert manager.pending_patches == {"Button": True}

    with pytest.raises(ValueError, match="read-only system point"):
        manager.patch({"Locked": False})


def test_patch_rejects_non_string_non_tag_key() -> None:
    manager = _manager()

    with pytest.raises(TypeError, match="patch\\(\\) keys must be str or Tag"):
        manager.patch({1: True})  # type: ignore[arg-type]


def test_force_lifecycle_add_remove_clear_and_read_only_guard() -> None:
    manager = _manager(read_only={"Locked"})
    flag = Bool("Flag")

    manager.add_force(flag, True)
    assert manager.forces_mutable == {"Flag": True}

    with pytest.raises(ValueError, match="read-only system point"):
        manager.add_force("Locked", True)

    manager.remove_force("Flag")
    assert manager.forces_mutable == {}

    with pytest.raises(KeyError, match="Missing"):
        manager.remove_force("Missing")

    manager.add_force("A", True)
    manager.add_force("B", False)
    manager.clear_forces()
    assert manager.forces_mutable == {}


def test_force_context_manager_restores_snapshot() -> None:
    manager = _manager()
    manager.add_force("Outer", True)

    with manager.force({"Outer": False, "Inner": True}):
        assert dict(manager.forces) == {"Outer": False, "Inner": True}

    assert dict(manager.forces) == {"Outer": True}


def test_force_context_manager_is_exception_safe() -> None:
    manager = _manager()
    manager.add_force("Baseline", True)

    with pytest.raises(RuntimeError, match="boom"):
        with manager.force({"Temp": True}):
            raise RuntimeError("boom")

    assert dict(manager.forces) == {"Baseline": True}


def test_forces_property_is_read_only_view() -> None:
    manager = _manager()
    manager.add_force("A", True)

    active = manager.forces
    assert dict(active) == {"A": True}

    with pytest.raises(TypeError):
        active["A"] = False  # type: ignore[index]


def test_get_live_override_prefers_pending_patch_over_force() -> None:
    manager = _manager()
    manager.add_force("A", True)
    manager.patch({"A": False})

    found, value = manager.get_live_override("A")
    assert found is True
    assert value is False

    missing_found, missing_value = manager.get_live_override("Missing")
    assert missing_found is False
    assert missing_value is None


def test_apply_pre_scan_applies_patch_then_force_and_clears_patches() -> None:
    manager = _manager()
    ctx = _CtxProbe()
    manager.patch({"A": 1})
    manager.add_force("B", True)

    manager.apply_pre_scan(ctx)  # type: ignore[arg-type]

    assert ctx.calls == [{"A": 1}, {"B": True}]
    assert manager.pending_patches == {}
    assert dict(manager.forces) == {"B": True}


def test_apply_post_logic_applies_forces_only() -> None:
    manager = _manager()
    ctx = _CtxProbe()
    manager.patch({"A": 1})
    manager.add_force("B", True)

    manager.apply_post_logic(ctx)  # type: ignore[arg-type]

    assert ctx.calls == [{"B": True}]
    assert manager.pending_patches == {"A": 1}
