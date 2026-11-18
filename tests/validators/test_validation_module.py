"""The public ``pyrung.validation`` module: grouped findings and check settings."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from pyrung import Block, Bool, Program, Rung, TagType, latch, out, reset
from pyrung.validation import (
    CheckConfig,
    FindingDisplay,
    ValidationReport,
    find_check_config,
    load_check_config,
    save_check_config,
)


def _clicknick_group_validation_findings(
    report: ValidationReport | None,
) -> dict[str, list[FindingDisplay]]:
    """Group findings as ClickNick's Check Program window did before ``grouped()``.

    Copied from ``group_validation_findings`` in ClickNick's
    ``src/clicknick/services/program_check.py``.
    """
    from pyrung.core.validation.stuck_bits import StuckBitFinding, StuckBitReport

    grouped: dict[str, list[FindingDisplay]] = {}
    if report is None:
        return grouped

    stuck: list[StuckBitFinding] = []
    for finding in report:
        if isinstance(finding, StuckBitFinding):
            stuck.append(finding)
        else:
            grouped.setdefault(finding.code, []).append(finding.display)
    for group in StuckBitReport(findings=tuple(stuck)).grouped():
        grouped.setdefault(group.code, []).append(group.display)
    return grouped


def _mixed_program() -> Program:
    button_a = Bool("ButtonA")
    button_b = Bool("ButtonB")
    light = Bool("Light")
    motor = Bool("Motor")
    lamp = Bool("Lamp")
    coils = Block("C", TagType.BOOL, 1, 5)
    with Program() as prog:
        with Rung(button_a):
            reset(coils.select(1, 5))
        with Rung(button_b):
            latch(light)
        with Rung(button_a):
            reset(motor)
        with Rung(button_a):
            out(lamp)
        with Rung(button_b):
            out(lamp)
    return prog


def test_grouped_matches_clicknick_grouping() -> None:
    report = _mixed_program().check(select={"ALL"})

    grouped = report.grouped()

    assert grouped == _clicknick_group_validation_findings(report)
    assert list(grouped) == list(_clicknick_group_validation_findings(report))


def test_grouped_merges_one_block_reset_into_one_display() -> None:
    grouped = _mixed_program().check(select={"ALL"}).grouped()

    stuck_low = grouped["COIL_STUCK_LOW"]
    assert len(stuck_low) == 2
    assert "5 coils are reset here, latched nowhere: C1, C2, C3, C4, C5." in stuck_low[0].problem
    assert len(grouped["COIL_STUCK_HIGH"]) == 1
    assert len(grouped["COIL_CONFLICTING_OUTPUT"]) == 1


def test_grouped_of_an_empty_report_is_empty() -> None:
    assert ValidationReport(findings=()).grouped() == {}


def test_load_names_the_unknown_rule_and_the_file(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text('[tool.pyrung.check]\nignore = ["COIL", "OLD_RULE_NAME"]\n', encoding="utf-8")

    with pytest.raises(ValueError, match="OLD_RULE_NAME") as raised:
        load_check_config(path)
    assert str(path) in str(raised.value)

    with pytest.raises(ValueError, match="OLD_RULE_NAME"):
        find_check_config(tmp_path)


def test_load_accepts_known_rules_and_categories(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    save_check_config(path, CheckConfig(select=("ALL",), ignore=("COIL", "PTR_UNGUARDED_ACCESS")))

    assert load_check_config(path) == CheckConfig(
        select=("ALL",), ignore=("COIL", "PTR_UNGUARDED_ACCESS")
    )


def test_validation_module_loads_no_click_code() -> None:
    # pyrung itself imports pyclickplc for send() and receive(), so the check is that
    # pyrung.validation adds no CLICK module to what ``import pyrung`` loads.
    script = (
        "import sys\n"
        "import pyrung\n"
        "before = set(sys.modules)\n"
        "import pyrung.validation\n"
        "added = set(sys.modules) - before\n"
        "print(sorted(m for m in added if m.startswith(('pyclickplc', 'pyrung.click'))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, timeout=120
    )
    assert result.stdout.strip() == "[]"
