"""CLICK drops return() from the main program and end() from a subroutine, so the importer refuses them."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from pyrung.click.codegen import ladder_to_pyrung

_HEADER = ["marker", *(chr(65 + i) for i in range(26)), "AA", "AB", "AC", "AD", "AE", "AF"]


def _row(cells: list[str], af: str) -> list[str]:
    return ["R", *cells, *["-"] * (31 - len(cells)), af]


def _write(path: Path, rows: list[list[str]]) -> None:
    path.parent.mkdir(exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows([_HEADER, *rows])


_OUT = _row(["X001"], "out(Y001)")
_END = _row([], "end()")
_RETURN = _row([], "return()")
_CONDITIONAL_RETURN = _row(["X002"], "return()")
_CONDITIONAL_END = _row(["X002"], "end()")
_CALL = _row(["X001"], 'call("sub")')

_MAIN_REFUSAL = r"return\(\) is in the main program\. CLICK drops it there\. Use end\(\)"
_SUBROUTINE_REFUSAL = r"end\(\) is in a subroutine\. CLICK drops it there\. Use return\(\)"


@pytest.mark.parametrize(
    ("rows", "rung_number"),
    [
        ([_OUT, _RETURN, _END], 2),
        ([_OUT, _RETURN], 2),
        ([_RETURN, _OUT, _END], 1),
        ([_CONDITIONAL_RETURN, _OUT, _END], 1),
    ],
    ids=["last", "last-without-end", "unconditional-mid", "conditional-mid"],
)
def test_import_refuses_return_in_the_main_program(
    tmp_path: Path, rows: list[list[str]], rung_number: int
):
    _write(tmp_path / "main.csv", rows)
    with pytest.raises(ValueError, match=_MAIN_REFUSAL) as raised:
        ladder_to_pyrung(tmp_path)
    assert f"rung {rung_number}:" in str(raised.value)


@pytest.mark.parametrize(
    ("rows", "rung_number"),
    [
        ([_OUT, _END], 2),
        ([_END, _OUT, _RETURN], 1),
        ([_CONDITIONAL_END, _OUT, _RETURN], 1),
    ],
    ids=["last", "unconditional-mid", "conditional-mid"],
)
def test_import_refuses_end_in_a_subroutine(
    tmp_path: Path, rows: list[list[str]], rung_number: int
):
    _write(tmp_path / "main.csv", [_CALL, _END])
    _write(tmp_path / "subroutines" / "sub.csv", rows)
    with pytest.raises(ValueError, match=_SUBROUTINE_REFUSAL) as raised:
        ladder_to_pyrung(tmp_path)
    assert f'Subroutine "sub" (sub.csv), rung {rung_number}:' in str(raised.value)


def test_import_accepts_end_in_main_and_return_in_a_subroutine(tmp_path: Path):
    _write(tmp_path / "main.csv", [_CALL, _END])
    _write(tmp_path / "subroutines" / "sub.csv", [_CONDITIONAL_RETURN, _OUT, _RETURN])
    assert "return_early()" in ladder_to_pyrung(tmp_path)
