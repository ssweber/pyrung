"""Tests for PLCRunner monitor registrations."""

from __future__ import annotations

import pytest

from pyrung.core import Bool, PLCRunner


def test_monitor_fires_only_on_committed_value_change() -> None:
    runner = PLCRunner(logic=[])
    events: list[tuple[object, object]] = []
    runner.monitor("A", lambda current, previous: events.append((current, previous)))

    runner.patch({"A": 1})
    runner.step()
    runner.patch({"A": 1})
    runner.step()
    runner.patch({"A": 2})
    runner.step()

    assert events == [(1, None), (2, 1)]


def test_monitor_accepts_tag_objects() -> None:
    button = Bool("Button")
    runner = PLCRunner(logic=[])
    events: list[tuple[object, object]] = []
    runner.monitor(button, lambda current, previous: events.append((current, previous)))

    runner.patch({"Button": True})
    runner.step()

    assert events == [(True, None)]


def test_multiple_monitors_on_same_tag_fire_in_registration_order() -> None:
    runner = PLCRunner(logic=[])
    fired: list[str] = []
    first = runner.monitor("A", lambda _current, _previous: fired.append("first"))
    second = runner.monitor("A", lambda _current, _previous: fired.append("second"))

    assert first.id < second.id

    runner.patch({"A": 1})
    runner.step()

    assert fired == ["first", "second"]


def test_monitor_handle_disable_enable_and_remove() -> None:
    runner = PLCRunner(logic=[])
    events: list[tuple[object, object]] = []
    handle = runner.monitor("A", lambda current, previous: events.append((current, previous)))

    handle.disable()
    runner.patch({"A": 1})
    runner.step()
    assert events == []

    handle.enable()
    runner.patch({"A": 2})
    runner.step()
    assert events == [(2, 1)]

    handle.remove()
    runner.patch({"A": 3})
    runner.step()
    assert events == [(2, 1)]

    # Removed handles are inert; these calls should remain no-op.
    handle.remove()
    handle.enable()
    handle.disable()


def test_monitor_callback_exceptions_propagate() -> None:
    runner = PLCRunner(logic=[])

    def _boom(_current: object, _previous: object) -> None:
        raise RuntimeError("monitor boom")

    runner.monitor("A", _boom)
    runner.patch({"A": 1})

    with pytest.raises(RuntimeError, match="monitor boom"):
        runner.step()


def test_monitor_callback_receives_current_then_previous() -> None:
    runner = PLCRunner(logic=[])
    captured: list[tuple[object, object]] = []
    runner.monitor("A", lambda current, previous: captured.append((current, previous)))

    runner.patch({"A": 10})
    runner.step()
    runner.patch({"A": 42})
    runner.step()

    assert captured[-1] == (42, 10)
