"""Pytest plugin for pyrung coverage collection and CI gating.

Provides a ``pyrung_coverage`` fixture that collects per-test
``CoverageReport`` objects and merges them at session end.  Optionally
emits ``pyrung_coverage.json`` and gates CI on a TOML whitelist.

Usage
-----
Enable the plugin by adding ``-p pyrung.pytest_plugin`` to pytest options,
or by registering it as a plugin in ``conftest.py``::

    pytest_plugins = ["pyrung.pytest_plugin"]

Then use the ``pyrung_coverage`` fixture in your conftest to register
reports::

    @pytest.fixture
    def program(pyrung_coverage):
        p = PLC(logic)
        yield p
        pyrung_coverage.collect(p)

Command-line options
--------------------
``--pyrung-coverage-json=PATH``
    Write merged coverage report to PATH (default: ``pyrung_coverage.json``).
    Set to empty string to disable output.

``--pyrung-whitelist=PATH``
    TOML whitelist file.  New findings not in the whitelist cause a test
    failure.  See :class:`Whitelist` for the file format.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import reduce
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pyrung.core.analysis.query import CoverageReport
    from pyrung.core.runner import PLC


# ---------------------------------------------------------------------------
# Whitelist (TOML)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Whitelist:
    """Known-acceptable coverage findings.

    Loaded from a TOML file with this shape::

        [cold_rungs]
        allow = [22, 91, 104]

        [stranded_chains]
        # Each entry is the effect-tag name.  The blocker fingerprint is
        # intentionally *not* part of the whitelist — if the blocker
        # reason changes, the entry should be re-evaluated.
        allow = ["Sts_SpecialFault"]
    """

    cold_rungs: frozenset[int] = field(default_factory=frozenset)
    stranded_tags: frozenset[str] = field(default_factory=frozenset)


def load_whitelist(path: Path) -> Whitelist:
    """Load a TOML whitelist file.  Returns empty whitelist on missing file."""
    if not path.exists():
        return Whitelist()

    try:
        import tomllib
    except ModuleNotFoundError:  # Python < 3.11
        import tomli as tomllib  # type: ignore[no-redef]

    text = path.read_text(encoding="utf-8")
    data = tomllib.loads(text)

    cold = frozenset(data.get("cold_rungs", {}).get("allow", []))
    stranded = frozenset(data.get("stranded_chains", {}).get("allow", []))
    return Whitelist(cold_rungs=cold, stranded_tags=stranded)


def check_whitelist(report: CoverageReport, whitelist: Whitelist) -> tuple[set[int], set[str]]:
    """Return (new_cold, new_stranded) not covered by the whitelist."""
    new_cold = set(report.cold_rungs) - set(whitelist.cold_rungs)
    stranded_tags = {tag for tag, _blockers in report.stranded_chains}
    new_stranded = stranded_tags - set(whitelist.stranded_tags)
    return new_cold, new_stranded


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class CoverageCollector:
    """Accumulates per-test ``CoverageReport`` objects for session-end merge."""

    def __init__(self) -> None:
        self._reports: list[CoverageReport] = []

    def collect(self, plc: PLC) -> None:
        """Collect a coverage report from a PLC instance after a test run."""
        self._reports.append(plc.query.report())

    def collect_report(self, report: CoverageReport) -> None:
        """Collect a pre-built coverage report directly."""
        self._reports.append(report)

    @property
    def reports(self) -> list[CoverageReport]:
        return list(self._reports)

    def merge(self) -> CoverageReport | None:
        """Merge all collected reports.  Returns None if no reports."""
        if not self._reports:
            return None
        return reduce(lambda a, b: a.merge(b), self._reports)


# ---------------------------------------------------------------------------
# Pytest hooks & fixtures
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("pyrung", "pyrung coverage")
    group.addoption(
        "--pyrung-coverage-json",
        default="pyrung_coverage.json",
        help="Path for merged coverage JSON (empty string to disable).",
    )
    group.addoption(
        "--pyrung-whitelist",
        default="",
        help="Path to TOML whitelist file for CI gating.",
    )


@pytest.fixture(scope="session")
def pyrung_coverage(request: pytest.FixtureRequest) -> CoverageCollector:
    """Session-scoped collector for pyrung coverage reports.

    Use ``pyrung_coverage.collect(plc)`` in a fixture teardown to register
    a PLC instance's coverage, or ``pyrung_coverage.collect_report(report)``
    to register a pre-built report.
    """
    collector = CoverageCollector()
    request.config._pyrung_collector = collector  # type: ignore[attr-defined]
    return collector


def pytest_configure(config: pytest.Config) -> None:
    config._pyrung_collector = None  # type: ignore[attr-defined]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    collector: CoverageCollector | None = session.config._pyrung_collector  # type: ignore[attr-defined]
    if collector is None:
        return

    merged = collector.merge()
    if merged is None:
        return

    # Write JSON report
    json_path = session.config.getoption("pyrung_coverage_json")
    if json_path:
        path = Path(json_path)
        path.write_text(json.dumps(merged.to_dict(), indent=2) + "\n", encoding="utf-8")

    # CI gating against whitelist
    whitelist_path = session.config.getoption("pyrung_whitelist")
    if whitelist_path:
        whitelist = load_whitelist(Path(whitelist_path))
        new_cold, new_stranded = check_whitelist(merged, whitelist)
        if new_cold or new_stranded:
            parts: list[str] = []
            if new_cold:
                parts.append(f"New cold rungs not in whitelist: {sorted(new_cold)}")
            if new_stranded:
                parts.append(f"New stranded bits not in whitelist: {sorted(new_stranded)}")
            session.exitstatus = 1
            session.config._pyrung_failures = parts  # type: ignore[attr-defined]


@pytest.hookimpl(trylast=True)
def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    failures: list[str] | None = getattr(config, "_pyrung_failures", None)
    if failures:
        terminalreporter.section("pyrung coverage")
        for line in failures:
            terminalreporter.line(line, red=True)
