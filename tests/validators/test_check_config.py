"""Selection semantics and project TOML round trips."""

import pytest

from pyrung.core import Program
from pyrung.core.validation.config import (
    CheckConfig,
    find_check_config,
    load_check_config,
    save_check_config,
)
from pyrung.core.validation.registry import RULES, default_on_rules


def test_core_defaults_and_report_execution_metadata():
    defaults = CheckConfig().resolve()
    assert defaults == {
        "TAG_READONLY_WRITE",
        "TAG_CHOICES_VIOLATION",
        "TAG_RANGE_VIOLATION",
        "TAG_FINAL_MULTIPLE_WRITERS",
        "TAG_DEAD_WRITE",
        "COIL_CONFLICTING_OUTPUT",
        "PTR_DEFAULT_BEFORE_BLOCK_START",
        "PTR_MAY_ESCAPE_BLOCK",
        "RUNG_CONTRADICTION",
        "RUNG_TAUTOLOGY",
        "CMP_ALWAYS_FALSE",
        "CMP_EQ_ON_MONOTONE",
        "CALL_RECURSION",
        "MATH_DIV_ZERO",
    }
    with Program() as program:
        pass
    assert program.check().checked_rules == defaults
    assert program.check(select=set()).checked_rules == frozenset()
    assert program.check(select={"ALL"}).checked_rules == frozenset(RULES)


def test_selection_prefix_specificity_and_ties():
    assert CheckConfig(select=("PTR",), ignore=("PTR_UNGUARDED_ACCESS",)).resolve() == {
        "PTR_DEFAULT_BEFORE_BLOCK_START",
        "PTR_MAY_ESCAPE_BLOCK",
    }
    assert CheckConfig(select=("ALL", "CMP_ALWAYS_FALSE"), ignore=("CMP",)).resolve() & {
        code for code in RULES if code.startswith("CMP")
    } == {"CMP_ALWAYS_FALSE"}
    assert not CheckConfig(select=("PTR",), ignore=("PTR",)).resolve()
    assert not (CheckConfig(ignore=("PTR",)).resolve() & {c for c in RULES if c.startswith("PTR")})
    assert CheckConfig(extend_select=("CALL_NEVER_CALLED",)).resolve() == default_on_rules() | {
        "CALL_NEVER_CALLED"
    }


@pytest.mark.parametrize("mapping", [{"select": "ALL"}, {"ignore": [1]}, {"typo": []}])
def test_invalid_config_is_an_error(mapping):
    with pytest.raises(ValueError):
        CheckConfig.from_mapping(mapping)


def test_unknown_rule_is_not_silently_ignored():
    with pytest.raises(ValueError, match="Unknown rule"):
        CheckConfig(select=("TYPO",)).resolve()


def test_toml_roundtrip_preserves_comments_and_other_tools(tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '# project comment\n[project]\nname = "demo"\n\n[tool.ruff.lint]\nselect = ["E"] # keep me\n',
        encoding="utf-8",
    )
    config = CheckConfig(select=(), ignore=("COIL",))
    save_check_config(path, config)
    assert load_check_config(path) == config
    assert "# keep me" in path.read_text()
    assert 'name = "demo"' in path.read_text()
    save_check_config(path, CheckConfig())
    assert load_check_config(path) == CheckConfig()
    assert 'select = ["E"]' in path.read_text()


def test_discovery_skips_pyproject_without_check_section(tmp_path):
    save_check_config(tmp_path / "pyproject.toml", CheckConfig(select=("PTR",)))
    child = tmp_path / "src"
    child.mkdir()
    (child / "pyproject.toml").write_text('[project]\nname="child"', encoding="utf-8")
    assert find_check_config(child) == (CheckConfig(select=("PTR",)), tmp_path / "pyproject.toml")
    save_check_config(child / "pyproject.toml", CheckConfig(select=()))
    assert find_check_config(child)[0].resolve() == frozenset()


@pytest.mark.parametrize(
    "content",
    ['tool = "invalid"', '[tool]\npyrung = "invalid"', '[tool.pyrung]\ncheck = "invalid"'],
)
def test_invalid_tables_are_not_overwritten(tmp_path, content):
    path = tmp_path / "pyproject.toml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="must be a table"):
        save_check_config(path, CheckConfig())
    assert path.read_text(encoding="utf-8") == content
