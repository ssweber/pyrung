"""Explore cache: persist TransitionGraph across DAP session restarts.

Writes a pickle file keyed by ``program_hash`` (compiled kernel digest)
to the session directory.  On the next launch of the same program the
cached graph is restored onto the runner, avoiding a re-exploration.
"""

from __future__ import annotations

import logging
import os
import pickle
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyrung.core.analysis.graph import TransitionGraph
    from pyrung.core.program import Program

_log = logging.getLogger(__name__)

_CACHE_VERSION = 1

_SESSION_DIR = Path(
    os.environ.get("PYRUNG_SESSION_DIR", str(Path(tempfile.gettempdir()) / "pyrung"))
)

_GLOB = "pyrung-explore-*.cache"


def _cache_path(phash: str) -> Path:
    return _SESSION_DIR / f"pyrung-explore-{phash}.cache"


def _compute_hash(program: Program) -> str:
    from pyrung.core.analysis.prove.lockfile import program_hash

    return program_hash(program)


def try_save(
    graph: TransitionGraph,
    program: Program,
    depth_budget: int = 50,
    max_states: int = 100_000,
) -> None:
    """Persist *graph* to disk.  Silently swallows errors."""
    try:
        phash = _compute_hash(program)
        envelope = {
            "cache_version": _CACHE_VERSION,
            "program_hash": phash,
            "depth_budget": depth_budget,
            "max_states": max_states,
            "graph": graph,
        }
        _SESSION_DIR.mkdir(parents=True, exist_ok=True)
        target = _cache_path(phash)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(pickle.dumps(envelope, protocol=pickle.HIGHEST_PROTOCOL))
        os.replace(tmp, target)
        _cleanup_stale(phash)
    except Exception:
        _log.debug("explore cache: save failed", exc_info=True)


def try_restore(
    program: Program,
    depth_budget: int = 50,
    max_states: int = 100_000,
) -> TransitionGraph | None:
    """Load a cached graph if one exists and is still valid."""
    try:
        phash = _compute_hash(program)
        path = _cache_path(phash)
        if not path.is_file():
            return None
        envelope: dict[str, Any] = pickle.loads(path.read_bytes())  # noqa: S301
        if envelope.get("cache_version") != _CACHE_VERSION:
            path.unlink(missing_ok=True)
            return None
        if envelope.get("program_hash") != phash:
            path.unlink(missing_ok=True)
            return None
        if envelope.get("depth_budget") != depth_budget:
            return None
        if envelope.get("max_states") != max_states:
            return None
        return envelope["graph"]  # type: ignore[no-any-return]
    except Exception:
        _log.debug("explore cache: restore failed", exc_info=True)
        try:
            phash_local = _compute_hash(program)
            _cache_path(phash_local).unlink(missing_ok=True)
        except Exception:
            pass
        return None


def _cleanup_stale(keep_hash: str) -> None:
    """Remove cache files that don't match *keep_hash*."""
    keep_name = _cache_path(keep_hash).name
    for p in _SESSION_DIR.glob(_GLOB):
        if p.name != keep_name:
            try:
                p.unlink()
            except Exception:
                pass
