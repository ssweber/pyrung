"""The ClickNick workspace export: unchanged rungs must not show up in Preview Changes.

ClickNick diffs CLICK's ladder snapshot (``csv/``) against what the workspace's
``src/plc/`` exports to.  So the source ``make conformance-workspace`` writes
has to export back to the very CSVs the suite exports, byte for byte, or every
rung would look changed.  No network.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest

from pyrung.click import pyrung_to_ladder
from tests.conformance import suite


def _load_plc_package(source: Path, name: str):
    """Import a generated ``src/plc`` package under a unique *name*; return (main, tags)."""
    spec = importlib.util.spec_from_file_location(
        name, source / "__init__.py", submodule_search_locations=[str(source)]
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    return importlib.import_module(f"{name}.main"), importlib.import_module(f"{name}.tags")


@pytest.fixture
def plc_package():
    """Load generated packages, dropping them from ``sys.modules`` afterwards."""
    names: list[str] = []

    def load(source: Path):
        name = f"_conformance_workspace_plc_{len(names)}"
        names.append(name)
        return _load_plc_package(source, name)

    yield load
    for module in [m for m in sys.modules if any(m.split(".")[0] == n for n in names)]:
        sys.modules.pop(module, None)


def _csv_files(directory: Path) -> dict[str, bytes]:
    return {p.relative_to(directory).as_posix(): p.read_bytes() for p in directory.rglob("*.csv")}


# The whole suite is over CLICK's step limit; the export warns and writes it all.
@pytest.mark.filterwarnings("ignore:CLK_STEP_LIMIT")
def test_workspace_source_exports_byte_identical_ladder(tmp_path: Path, plc_package):
    program = suite.build()
    expected = _csv_files(program.export(tmp_path / "export", drawn=False))

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = program.export_workspace(workspace)
    main, tags = plc_package(source)

    # What ClickNick's project_to_csv.py does with the workspace source.
    out = tmp_path / "roundtrip"
    pyrung_to_ladder(main.logic, tags.mapping).write(out)
    tags.mapping.to_nickname_file(out / "nicknames.csv", blocks=tags.blocks)

    actual = _csv_files(out)
    assert sorted(actual) == sorted(expected)
    for name, content in expected.items():
        assert actual[name] == content, f"{name} changed in the round trip"


# The whole suite is over CLICK's step limit; the export warns and writes it all.
@pytest.mark.filterwarnings("ignore:CLK_STEP_LIMIT")
def test_workspace_export_replaces_only_src_plc(tmp_path: Path):
    workspace = tmp_path / "workspace"
    stale = workspace / "src" / "plc" / "subroutines" / "_sub_gone.py"
    stale.parent.mkdir(parents=True)
    stale.write_text("# a subroutine the suite no longer has\n", encoding="utf-8")
    kept = {
        "csv/main.csv": "CLICK's snapshot\n",
        "nicknames.csv": "ClickNick's\n",
        "README.md": "notes\n",
        "src/other.txt": "beside the package\n",
    }
    for rel, text in kept.items():
        path = workspace / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    source = suite.build().export_workspace(workspace)

    assert source == workspace / "src" / "plc"
    assert not stale.exists()
    assert (source / "main.py").is_file()
    assert (source / "tags.py").is_file()
    for rel, text in kept.items():
        assert (workspace / rel).read_text(encoding="utf-8") == text
    leftovers = {p.name for p in workspace.iterdir()} | {
        p.name for p in (workspace / "src").iterdir()
    }
    assert not [n for n in leftovers if n.startswith(".")], leftovers


def test_workspace_export_needs_an_existing_folder(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        suite.build().export_workspace(tmp_path / "missing")


def test_committed_workspace_links_the_conformance_plc():
    sidecar = suite.WORKSPACE_PATH / ".clicknick.toml"
    data = tomllib.loads(sidecar.read_text(encoding="utf-8"))
    assert data == {"version": 1, "plc": {"name": "PYRUNG_CONFORMANCE"}}
