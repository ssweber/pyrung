"""A case's ``formulas=`` types its Math formulas into the CLICK export without brackets."""

from __future__ import annotations

import pytest

from pyrung.conformance._formula import draw_formulas

_HEADER = tuple(["R"] + [str(i) for i in range(1, 32)] + ["AF"])


def _marker(case_id: str) -> tuple[str, ...]:
    return ("#", f"Case g/{case_id}")


def _math(token: str) -> tuple[str, ...]:
    return tuple(["R", "X001"] + ["-"] * 30 + [token])


def _rows(*rows: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    return (_HEADER, *rows)


def test_each_math_of_the_case_gets_its_typed_formula() -> None:
    rows = _rows(
        _marker("a"),
        _math("math(( DH1 OR DH2 ) AND DH3,DH4,mode=hex)"),
        _math("math(( 0h - DH5 ) OR DH6,DH7,mode=hex)"),
        _marker("b"),
        _math("math(( DH1 OR DH2 ) AND DH3,DH4,mode=hex)"),
    )
    drawn = draw_formulas(rows, "g/a", ["DH1 OR DH2 AND DH3", "- DH5 OR DH6"])
    assert drawn[2][-1] == "math(DH1 OR DH2 AND DH3,DH4,mode=hex)"
    assert drawn[3][-1] == "math(- DH5 OR DH6,DH7,mode=hex)"
    # The next case keeps the exporter's formula.
    assert drawn[5] == rows[5]


def test_a_typed_formula_must_compute_what_the_case_runs() -> None:
    rows = _rows(_marker("a"), _math("math(( DH1 OR DH2 ) AND DH3,DH4,mode=hex)"))
    with pytest.raises(ValueError, match="does not compute what the case's math"):
        draw_formulas(rows, "g/a", ["DH1 OR ( DH2 AND DH3 )"])


def test_each_math_needs_a_formula() -> None:
    rows = _rows(_marker("a"), _math("math(DH1 + DH2,DH4,mode=hex)"))
    with pytest.raises(ValueError, match="1 math\\(\\) rows but 2 formulas"):
        draw_formulas(rows, "g/a", ["DH1 + DH2", "DH1 + DH2"])
    with pytest.raises(ValueError, match="No case 'g/b'"):
        draw_formulas(rows, "g/b", ["DH1 + DH2"])
