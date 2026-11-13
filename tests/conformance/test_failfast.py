"""``suite.build_for`` fails at once when GROUP= names groups the loaded part lacks."""

from __future__ import annotations

import time

import pytest

from pyrung.conformance import BuildMismatchError, ConformanceProgram, Endpoint, SimServer
from tests.conformance import suite

pytestmark = pytest.mark.conformance


def test_a_group_the_loaded_part_lacks_fails_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """GROUP= names another part than the PLC runs: one message, no wait, one build."""
    built: list[str | None] = []
    real = suite.build

    def counting(part: str | None = None) -> ConformanceProgram:
        built.append(part)
        return real(part)

    monkeypatch.setattr(suite, "build", counting)
    running = real("coils")
    with SimServer(running) as sim:
        assert sim.endpoint is not None
        started = time.monotonic()
        with pytest.raises(BuildMismatchError) as caught:
            suite.build_for(sim.endpoint, ["system", "faults", "flag_probe"])
        took = time.monotonic() - started
        # The SD9 wrap alone takes minutes; the check waits for nothing.
        assert took < 30.0
        assert built == ["system"]
        # A group the loaded part holds still returns that part, built alone.
        built.clear()
        assert suite.build_for(sim.endpoint, ["out"]).program_hash == running.program_hash
        assert built == ["coils"]
    holder = next(p for p, held in suite.PARTS.items() if "system" in held)
    text = str(caught.value)
    print(f"{took:.2f}s: {text}")
    assert f"layout {running.program_hash}; system, faults and flag_probe are in part" in text
    assert f"part {holder} (layout {real(holder).program_hash})" in text
    assert text.isascii()


def test_an_unknown_group_fails_before_any_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(suite, "read_program_hash", lambda endpoint: 0)
    monkeypatch.setattr(suite, "build", lambda part=None: pytest.fail("built a part"))
    with pytest.raises(ValueError, match="Unknown conformance group.*nope"):
        suite.build_for(Endpoint("127.0.0.1", 1), ["nope"])


def test_groups_from_different_parts_name_the_whole_suite() -> None:
    with SimServer(suite.build("coils")) as sim:
        assert sim.endpoint is not None
        with pytest.raises(BuildMismatchError) as caught:
            suite.build_for(sim.endpoint, ["out", "system"])
    text = str(caught.value)
    assert "are in the whole suite (layout " in text
    assert text.endswith("Load the whole suite (make conformance-export).")
