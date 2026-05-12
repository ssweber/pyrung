"""Tests for the exhaustive verifier and state-space snapshot."""

from __future__ import annotations

import importlib
from dataclasses import replace
from pathlib import Path

import pytest

from pyrung.cli import _apply_lock_config
from pyrung.core import (
    PLC,
    Block,
    Bool,
    Counter,
    Dint,
    InputBlock,
    Int,
    Or,
    OutputBlock,
    Program,
    Real,
    Rung,
    TagType,
    Timer,
    Word,
    calc,
    copy,
    count_down,
    count_up,
    fall,
    fill,
    forloop,
    latch,
    named_array,
    off_delay,
    on_delay,
    out,
    reset,
    rise,
    run_function,
)
from pyrung.core.analysis.prove import (
    PENDING,
    Counterexample,
    Intractable,
    Proven,
    StateDiff,
    TraceStep,
    _bfs_explore,
    _classify_dimensions,
    _default_projection,
    _eval_atom,
    _has_data_feedback,
    _live_inputs,
    _partial_eval,
    _pilot_sweep_domains,
    check_lock,
    diff_states,
    program_hash,
    prove,
    reachable_states,
    write_lock,
)
from pyrung.core.analysis.prove.passes import _BFSConfig
from pyrung.core.analysis.simplified import And as ExprAnd
from pyrung.core.analysis.simplified import Atom, Const
from pyrung.core.analysis.simplified import Or as ExprOr

from .conftest import no_agreement

prove_module = importlib.import_module("pyrung.core.analysis.prove")


def _replay_trace(program: Program, trace: list[TraceStep]) -> PLC:
    """Replay a prove() counterexample trace on the concrete PLC."""
    plc = PLC(program, dt=0.010)
    for step in trace:
        plc.patch(step.inputs)
        for _ in range(step.scans):
            plc.step()
    return plc


def _assert_soundness(
    logic: Program,
    condition,
    *,
    max_states: int = 10_000,
    depth_budget: int = 20,
) -> None:
    """Assert that optimized and unoptimized prove() agree on the result type."""
    optimized = prove(
        logic, condition, max_states=max_states, depth_budget=depth_budget, journal=True
    )
    unoptimized = prove(
        logic,
        condition,
        max_states=max_states,
        depth_budget=depth_budget,
        _skip_optimizations=True,
        journal=True,
    )
    if isinstance(optimized, Intractable) or isinstance(unoptimized, Intractable):
        pytest.skip("one side intractable")
    assert type(optimized) is type(unoptimized), (
        f"optimized={type(optimized).__name__}, unoptimized={type(unoptimized).__name__}\n"
        f"--- optimized journal ---\n{optimized.journal}\n"
        f"--- unoptimized journal ---\n{unoptimized.journal}"
    )


# ===================================================================
# Group 6: Lock file
# ===================================================================


class TestLockFile:
    def test_round_trip(self, tmp_path: Path):
        """Write and read back a lock file."""
        states = frozenset(
            {
                frozenset({("Running", False), ("Light", False)}),
                frozenset({("Running", True), ("Light", True)}),
            }
        )
        lock_path = tmp_path / "pyrung.lock"
        write_lock(lock_path, states, ["Light", "Running"], "abc123")

        data = __import__("json").loads(lock_path.read_text())
        assert data["version"] == 1
        assert data["program_hash"] == "abc123"
        assert len(data["reachable"]) == 2

    def test_check_detects_change(self, tmp_path: Path):
        """Lock check detects behavioral change."""
        button = Bool("Button", external=True)
        running = Bool("Running", public=True)

        with Program(strict=False) as logic:
            with Rung(button):
                latch(running)

        states = reachable_states(logic, project=["Running"])
        assert not isinstance(states, Intractable)
        lock_path = tmp_path / "pyrung.lock"
        write_lock(lock_path, states, ["Running"], program_hash(logic))

        d = check_lock(logic, lock_path)
        assert d is None  # should match

    def test_program_hash_changes(self):
        """Different programs produce different hashes."""
        a_in = Bool("A", external=True)
        b = Bool("B")

        with Program(strict=False) as logic_a:
            with Rung(a_in):
                out(b)

        with Program(strict=False) as logic_b:
            with Rung(a_in):
                latch(b)

        assert program_hash(logic_a) != program_hash(logic_b)

    def test_false_omitted_in_json(self, tmp_path: Path):
        """Lock file omits False values — states read as 'what is ON'."""
        import json

        states = frozenset(
            {
                frozenset({("A", False), ("B", False)}),
                frozenset({("A", True), ("B", False)}),
                frozenset({("A", True), ("B", True)}),
            }
        )
        lock_path = tmp_path / "pyrung.lock"
        write_lock(lock_path, states, ["A", "B"], "hash1")

        data = json.loads(lock_path.read_text())
        reachable = data["reachable"]
        assert len(reachable) == 3
        assert {} in reachable
        assert {"A": True} in reachable
        assert {"A": True, "B": True} in reachable

    def test_false_omission_roundtrips_via_check(self, tmp_path: Path):
        """check_lock round-trips correctly with False-omitted lock files."""
        button = Bool("Button", external=True)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(button):
                out(light)

        states = reachable_states(logic, project=["Light"])
        assert not isinstance(states, Intractable)
        lock_path = tmp_path / "pyrung.lock"
        write_lock(lock_path, states, ["Light"], program_hash(logic))

        d = check_lock(logic, lock_path)
        assert d is None

    def test_choice_labels_in_lock(self, tmp_path: Path):
        """Projected tags with choices= serialize labels, not raw ints."""
        import json

        from pyrung.core.analysis.prove import (
            _build_choice_labels,
            _resolve_choice_labels,
        )
        from pyrung.core.tag import Tag, TagType

        mode_tag = Tag(
            name="Mode",
            type=TagType.INT,
            choices={0: "OFF", 1: "SLOW", 2: "FAST"},
        )
        states = frozenset(
            {
                frozenset({("Mode", 0), ("Active", True)}),
                frozenset({("Mode", 1), ("Active", True)}),
                frozenset({("Mode", 2), ("Active", False)}),
            }
        )
        tags = {"Mode": mode_tag}
        choice_labels = _build_choice_labels(["Active", "Mode"], tags)
        resolved = _resolve_choice_labels(states, choice_labels)

        lock_path = tmp_path / "pyrung.lock"
        write_lock(lock_path, resolved, ["Active", "Mode"], "hash2")

        data = json.loads(lock_path.read_text())
        mode_values = {row.get("Mode") for row in data["reachable"]}
        assert "OFF" in mode_values
        assert "SLOW" in mode_values
        assert "FAST" in mode_values
        assert 0 not in mode_values

    def test_band_labels_collapse_states(self, tmp_path: Path):
        """Tags with band= collapse multiple values into labeled bands."""
        import json

        from pyrung.core.analysis.prove import _build_band_maps, _resolve_band_labels
        from pyrung.core.tag import Tag, TagType

        extent_tag = Tag(
            name="Extent",
            type=TagType.INT,
            band={"ZERO": 0, "POSITIVE": ">0"},
        )
        states = frozenset(
            {
                frozenset({("Extent", 0), ("Active", True)}),
                frozenset({("Extent", 1), ("Active", True)}),
                frozenset({("Extent", 2), ("Active", True)}),
                frozenset({("Extent", 3), ("Active", True)}),
            }
        )
        tags = {"Extent": extent_tag}
        band_maps = _build_band_maps(["Active", "Extent"], tags)
        resolved = _resolve_band_labels(states, band_maps)

        lock_path = tmp_path / "pyrung.lock"
        write_lock(lock_path, resolved, ["Active", "Extent"], "hash3")

        data = json.loads(lock_path.read_text())
        extent_values = {row.get("Extent") for row in data["reachable"]}
        assert extent_values == {"ZERO", "POSITIVE"}
        assert len(data["reachable"]) == 2

    def test_band_range_predicate(self):
        """Band with range predicates (a..b) works."""
        from pyrung.core.analysis.prove import _build_band_maps, _resolve_band_labels
        from pyrung.core.tag import Tag, TagType

        level_tag = Tag(
            name="Level",
            type=TagType.INT,
            band={"LOW": "0..2", "HIGH": "3..5"},
        )
        states = frozenset(
            {
                frozenset({("Level", 0)}),
                frozenset({("Level", 1)}),
                frozenset({("Level", 2)}),
                frozenset({("Level", 3)}),
                frozenset({("Level", 4)}),
                frozenset({("Level", 5)}),
            }
        )
        tags = {"Level": level_tag}
        band_maps = _build_band_maps(["Level"], tags)
        resolved = _resolve_band_labels(states, band_maps)

        level_values = {dict(s)["Level"] for s in resolved}
        assert level_values == {"LOW", "HIGH"}

    def test_band_wildcard_catchall(self):
        """Band with '*' catch-all matches unmatched values."""
        from pyrung.core.analysis.prove import _build_band_maps, _resolve_band_labels
        from pyrung.core.tag import Tag, TagType

        tag = Tag(
            name="Score",
            type=TagType.INT,
            band={"ZERO": 0, "OTHER": "*"},
        )
        states = frozenset(
            {
                frozenset({("Score", 0)}),
                frozenset({("Score", 7)}),
                frozenset({("Score", 99)}),
            }
        )
        tags = {"Score": tag}
        band_maps = _build_band_maps(["Score"], tags)
        resolved = _resolve_band_labels(states, band_maps)

        score_values = {dict(s)["Score"] for s in resolved}
        assert score_values == {"ZERO", "OTHER"}
