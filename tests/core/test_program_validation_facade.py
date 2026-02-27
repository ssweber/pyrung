"""Tests for Program.validate dialect facade."""

from __future__ import annotations

import importlib

import pytest

from pyrung.core.program import Program


def test_register_and_dispatch_custom_dialect():
    calls: list[tuple[str, dict[str, object]]] = []

    def validator(program: Program, *, mode: str = "warn", **kwargs: object) -> str:
        calls.append((mode, kwargs))
        assert isinstance(program, Program)
        return "ok"

    Program.register_dialect("unit_test", validator)

    prog = Program(strict=False)
    result = prog.validate("unit_test", mode="strict", answer=42)

    assert result == "ok"
    assert calls == [("strict", {"answer": 42})]
    assert "unit_test" in Program.registered_dialects()


def test_conflicting_dialect_registration_rejected():
    def v1(program: Program, *, mode: str = "warn", **kwargs: object) -> None:  # noqa: ARG001
        return None

    def v2(program: Program, *, mode: str = "warn", **kwargs: object) -> None:  # noqa: ARG001
        return None

    Program.register_dialect("collision_test", v1)
    Program.register_dialect("collision_test", v1)

    with pytest.raises(ValueError, match="already registered"):
        Program.register_dialect("collision_test", v2)


def test_unknown_dialect_error_includes_hint():
    prog = Program(strict=False)
    with pytest.raises(KeyError, match="Unknown validation dialect"):
        prog.validate("does_not_exist")


def test_click_registers_on_import():
    importlib.import_module("pyrung.click")
    assert "click" in Program.registered_dialects()


def test_click_validate_requires_tag_map_kwarg():
    importlib.import_module("pyrung.click")

    prog = Program(strict=False)
    with pytest.raises(TypeError, match="tag_map"):
        prog.validate("click")
