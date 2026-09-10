"""Untrusted CSV is instruction data, not an extension of Python syntax."""

from __future__ import annotations

import ast
import csv
from pathlib import Path

import pytest

from pyrung.click import ladder_to_pyrung, ladder_to_pyrung_project
from pyrung.click.ladder.types import LadderBundle


def _rows(token: str, condition: str = "-") -> tuple[tuple[str, ...], ...]:
    return (
        tuple(["R"] + [str(i) for i in range(1, 32)] + ["AF"]),
        tuple(["R", condition] + ["-"] * 30 + [token]),
    )


def _bundle(token: str, condition: str = "-") -> LadderBundle:
    return LadderBundle(main_rows=_rows(token, condition), subroutine_rows=())


@pytest.mark.parametrize(
    "token",
    [
        "math(len([]),DS1)",
        "math(__import__('os'),DS1)",
        "math((lambda: 1)(),DS1)",
        "math([x for x in []],DS1)",
        "math((x := 1),DS1)",
        "math(DS1.__class__,DS2)",
        'copy("a"+str(1)+"b",TXT1)',
        'copy(f"{len([])}",TXT1)',
        """copy('"' + len([]) + '"',TXT1)""",
        'math(1 # "quoted comment",DS1)',
        "copy(1,DS1,convert=len([]))",
        "copy(ModbusAddress(offset=len([])),DS1)",
        "print(1)",
    ],
)
@pytest.mark.parametrize("project", [False, True])
def test_csv_rejects_python_syntax(token: str, project: bool, tmp_path: Path) -> None:
    source = tmp_path / "main.csv"
    with source.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(_rows(token))
    generate = ladder_to_pyrung_project if project else ladder_to_pyrung
    with pytest.raises(ValueError):
        generate(source)


def test_condition_cannot_introduce_python_calls() -> None:
    with pytest.raises(ValueError, match="Unsupported CSV operand"):
        ladder_to_pyrung(_bundle("out(Y001)", "len([])==0"))


@pytest.mark.parametrize(
    "value",
    [
        'a "quoted" value, with (parentheses)',
        "C:\\new\\test\\",
        '";len([]);#',
        "Unicode: \u03bb",
    ],
)
def test_click_string_values_remain_literal_data(value: str) -> None:
    token = 'copy("' + value.replace('"', '""') + '",TXT1)'
    tree = ast.parse(ladder_to_pyrung(_bundle(token)))
    copy_call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "copy"
    )
    assert isinstance(copy_call.args[0], ast.Constant)
    assert copy_call.args[0].value == value


def test_raw_instruction_fields_are_literal_data() -> None:
    fields = "field=';len([]);#"
    tree = ast.parse(ladder_to_pyrung(_bundle(f"raw(Future,{fields})")))
    raw_call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "raw"
    )
    assert isinstance(raw_call.args[1], ast.Constant)
    assert raw_call.args[1].value == fields


@pytest.mark.parametrize("nickname", ['a"+str(1)+"b', "name\nlen([])\r#", "C:\\new"])
def test_range_nickname_is_a_literal(nickname: str, tmp_path: Path) -> None:
    nickname_csv = tmp_path / "nicknames.csv"
    with nickname_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["Address", "Data Type", "Nickname", "Initial Value", "Retentive", "Address Comment"]
        )
        writer.writerow(["DS1", "INT", nickname, "", "Yes", ""])
    code = ladder_to_pyrung(_bundle("fill(0,DS1..DS2)"), nickname_csv=nickname_csv)
    tree = ast.parse(code)
    names = [
        keyword.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "name"
    ]
    assert any(isinstance(name, ast.Constant) and name.value == nickname for name in names)
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"str", "len"}
        for node in ast.walk(tree)
    )


def test_subroutine_name_is_data_in_both_output_formats() -> None:
    name = 'Pump" + str(1) + "'
    token = 'call("' + name.replace('"', '""') + '")'
    bundle = LadderBundle(main_rows=_rows(token), subroutine_rows=((name, _rows("out(Y001)")),))
    sources = [ladder_to_pyrung(bundle)]
    sources.extend(
        source for path, source in ladder_to_pyrung_project(bundle).items() if path.endswith(".py")
    )
    declarations = []
    for source in sources:
        tree = ast.parse(source)
        declarations.extend(
            node.args[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "subroutine"
        )
    assert len(declarations) == 2
    assert all(isinstance(node, ast.Constant) and node.value == name for node in declarations)


@pytest.mark.parametrize(
    "expression",
    [
        "DS1 + 2 * (DS2 - 1)",
        "DS1 ^ 2",
        "DS1 MOD 3",
        "DH1 AND FFFFh",
        "DH1 XOR DH2",
        "SUM(DS1:DS4)",
        "SQRT(DS1)",
        "SIN(PI)",
        "DH[DS1] + 1",
        "SQRT(DS1) + SIN(DS2)",
        "DH[DS1] + DH[DS2]",
    ],
)
def test_supported_click_expressions_still_generate(expression: str) -> None:
    code = ladder_to_pyrung(_bundle(f"math({expression},DS10)"))
    ast.parse(code)
