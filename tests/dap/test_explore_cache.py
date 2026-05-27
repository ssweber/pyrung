"""Tests for explore cache persistence across DAP restarts."""

from __future__ import annotations

import pickle
from pathlib import Path

import pytest

from pyrung.core import PLC, Bool, Program, Rung, latch, out


def _simple_program() -> Program:
    start = Bool("Start", external=True)
    running = Bool("Running")
    done = Bool("Done")
    with Program() as prog:
        with Rung(start):
            latch(running)
        with Rung(running):
            out(done)
    return prog


def _different_program() -> Program:
    a = Bool("A", external=True)
    b = Bool("B")
    with Program() as prog:
        with Rung(a):
            out(b)
    return prog


@pytest.fixture(autouse=True)
def _session_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    session_dir = tmp_path / "pyrung"
    session_dir.mkdir()
    monkeypatch.setenv("PYRUNG_SESSION_DIR", str(session_dir))
    import pyrung.dap.explore_cache as mod

    monkeypatch.setattr(mod, "_SESSION_DIR", session_dir)
    return session_dir


class TestRoundTrip:
    def test_save_and_restore(self, _session_dir: Path) -> None:
        from pyrung.dap.explore_cache import try_restore, try_save

        prog = _simple_program()
        runner = PLC(prog, dt=0.010)
        graph = runner.explore()

        try_save(graph, prog)
        restored = try_restore(prog)

        assert restored is not None
        assert restored.state_count == graph.state_count
        assert restored.edge_count == graph.edge_count
        assert restored.initial_key == graph.initial_key

    def test_how_works_on_restored_graph(self, _session_dir: Path) -> None:
        from pyrung.dap.explore_cache import try_restore, try_save

        running = Bool("Running")
        prog = _simple_program()
        runner = PLC(prog, dt=0.010)
        graph = runner.explore()
        try_save(graph, prog)

        runner2 = PLC(prog, dt=0.010)
        restored = try_restore(prog)
        assert restored is not None
        runner2._transition_graph = restored
        path = runner2.how(running)
        assert path.reachable


class TestInvalidation:
    def test_different_program_misses_cache(self, _session_dir: Path) -> None:
        from pyrung.dap.explore_cache import try_restore, try_save

        prog1 = _simple_program()
        runner1 = PLC(prog1, dt=0.010)
        graph = runner1.explore()
        try_save(graph, prog1)

        prog2 = _different_program()
        assert try_restore(prog2) is None

    def test_parameter_mismatch_misses(self, _session_dir: Path) -> None:
        from pyrung.dap.explore_cache import try_restore, try_save

        prog = _simple_program()
        runner = PLC(prog, dt=0.010)
        graph = runner.explore()
        try_save(graph, prog, depth_budget=50, max_states=100_000)

        assert try_restore(prog, depth_budget=100) is None

    def test_missing_file_returns_none(self, _session_dir: Path) -> None:
        from pyrung.dap.explore_cache import try_restore

        prog = _simple_program()
        assert try_restore(prog) is None


class TestCorruption:
    def test_corrupt_file_returns_none(self, _session_dir: Path) -> None:
        from pyrung.dap.explore_cache import _cache_path, _compute_hash, try_restore, try_save

        prog = _simple_program()
        runner = PLC(prog, dt=0.010)
        graph = runner.explore()
        try_save(graph, prog)

        phash = _compute_hash(prog)
        path = _cache_path(phash)
        path.write_bytes(b"garbage data")

        assert try_restore(prog) is None

    def test_corrupt_file_is_cleaned_up(self, _session_dir: Path) -> None:
        from pyrung.dap.explore_cache import _cache_path, _compute_hash, try_restore, try_save

        prog = _simple_program()
        runner = PLC(prog, dt=0.010)
        graph = runner.explore()
        try_save(graph, prog)

        phash = _compute_hash(prog)
        path = _cache_path(phash)
        path.write_bytes(b"garbage data")

        try_restore(prog)
        assert not path.exists()

    def test_wrong_cache_version_returns_none(self, _session_dir: Path) -> None:
        from pyrung.dap.explore_cache import _cache_path, _compute_hash, try_restore

        prog = _simple_program()
        runner = PLC(prog, dt=0.010)
        graph = runner.explore()

        phash = _compute_hash(prog)
        envelope = {
            "cache_version": 999,
            "program_hash": phash,
            "depth_budget": 50,
            "max_states": 100_000,
            "graph": graph,
        }
        path = _cache_path(phash)
        path.write_bytes(pickle.dumps(envelope))

        assert try_restore(prog) is None


class TestCleanup:
    def test_stale_files_removed(self, _session_dir: Path) -> None:
        from pyrung.dap.explore_cache import _cleanup_stale

        stale1 = _session_dir / "pyrung-explore-deadbeef00000000.cache"
        stale2 = _session_dir / "pyrung-explore-cafebabe11111111.cache"
        keep = _session_dir / "pyrung-explore-keepthisone00000.cache"
        for f in (stale1, stale2, keep):
            f.write_bytes(b"x")

        _cleanup_stale("keepthisone00000")

        assert not stale1.exists()
        assert not stale2.exists()
        assert keep.exists()
