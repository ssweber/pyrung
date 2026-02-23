"""Tests for dataclass-driven DAP argument parsing helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pytest

from pyrung.dap.args import coerce_int, parse_args, parse_args_list


class _ParseError(ValueError):
    pass


@dataclass
class _LaunchArgs:
    program: str


@dataclass
class _VariablesArgs:
    variablesReference: int
    optionalName: str | None = None


def test_parse_args_parses_required_field() -> None:
    parsed = parse_args(_LaunchArgs, {"program": "x.py"}, error=_ParseError)
    assert parsed.program == "x.py"


def test_parse_args_rejects_missing_required_field() -> None:
    with pytest.raises(_ParseError, match="Missing required field: program"):
        parse_args(_LaunchArgs, {}, error=_ParseError)


def test_parse_args_rejects_invalid_type() -> None:
    with pytest.raises(_ParseError, match="Invalid type for field: program"):
        parse_args(_LaunchArgs, {"program": 123}, error=_ParseError)


def test_parse_args_applies_field_coercer() -> None:
    parsed = parse_args(
        _VariablesArgs,
        {"variablesReference": "4"},
        error=_ParseError,
        coercers={"variablesReference": coerce_int},
    )
    assert parsed.variablesReference == 4


def test_parse_args_rejects_failed_coercion() -> None:
    with pytest.raises(_ParseError, match="Invalid value for field: variablesReference"):
        parse_args(
            _VariablesArgs,
            {"variablesReference": "nope"},
            error=_ParseError,
            coercers={"variablesReference": coerce_int},
        )


def test_parse_args_allows_optional_field_none() -> None:
    parsed = parse_args(
        _VariablesArgs,
        {"variablesReference": 1, "optionalName": None},
        error=_ParseError,
    )
    assert parsed.optionalName is None


def test_parse_args_list_parses_each_object() -> None:
    parsed = parse_args_list(
        _LaunchArgs,
        [{"program": "a.py"}, {"program": "b.py"}],
        error=_ParseError,
    )
    assert [entry.program for entry in parsed] == ["a.py", "b.py"]


def test_parse_args_list_rejects_non_object_entries() -> None:
    with pytest.raises(_ParseError, match="Breakpoint entry must be an object"):
        parse_args_list(_LaunchArgs, [{"program": "a.py"}, 1], error=_ParseError)  # type: ignore[list-item]


def test_parse_args_requires_mapping_inputs() -> None:
    with pytest.raises(_ParseError, match="Arguments must be an object"):
        parse_args(_LaunchArgs, cast(object, 1), error=_ParseError)
