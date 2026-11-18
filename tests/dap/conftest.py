from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def session_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write each test's live-session port files to its own folder, not the real temp folder."""
    import pyrung.dap.live as live_mod

    directory = tmp_path / "sessions"
    monkeypatch.setattr(live_mod, "_SESSION_DIR", directory)
    return directory
