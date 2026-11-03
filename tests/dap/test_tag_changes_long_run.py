"""The history panel after a long compiled run: the same entries as stepping, or a window."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from pyrung.core import PLC, Bool, Program, Rung, TimeMode, out
from pyrung.dap.adapter import DAPAdapter
from pyrung.dap.handlers import history_seek
from pyrung.dap.protocol import read_message

WATCHED = ["Lamp", "Stage", "T2_Done", "Mirror", "Patched"]

_SCRIPT = """
from pyrung.core import PLC, Bool, Int, Program, Rung, Timer, copy, latch, on_delay, out

Enable = Bool("Enable")
Lamp = Bool("Lamp")
Held = Bool("Held")
Mirror = Bool("Mirror")
Stage = Int("Stage")
Patched = Int("Patched")
T1 = Timer.clone("T1")
T2 = Timer.clone("T2")

with Program(strict=False) as prog:
    with Rung(Enable):
        on_delay(T1, preset=30050)
    with Rung(T1.Done):
        latch(Lamp)
        copy(5, Stage)
    with Rung(Enable, ~T2.Done):
        on_delay(T2, preset=1000)
    with Rung(Held):
        out(Mirror)

runner = PLC(prog, dt=0.01)
runner.patch({"Enable": True})
runner.force(Held, True)
runner.run_for(20.0, jit=JIT)
runner.patch({"Patched": 7})
runner.run_for(20.0, jit=JIT)
"""


class Session:
    def __init__(self, tmp_path: Path, *, jit: bool) -> None:
        self._out = io.BytesIO()
        self.adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=self._out)
        self._seq = 0
        name = "compiled.py" if jit else "stepped.py"
        script = tmp_path / name
        script.write_text(f"JIT = {jit}\n{_SCRIPT}", encoding="utf-8")
        self._request("launch", {"program": str(script)})

    @property
    def runner(self) -> Any:
        return self.adapter._runner

    def _request(self, command: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        self.adapter.handle_request(
            {"seq": self._seq, "type": "request", "command": command, "arguments": arguments}
        )
        reader = io.BytesIO(self._out.getvalue())
        self._out.seek(0)
        self._out.truncate(0)
        messages = []
        while (message := read_message(reader)) is not None:
            messages.append(message)
        [response] = [message for message in messages if message.get("type") == "response"]
        assert response["success"] is True, response
        return response

    def changes(self, **arguments: Any) -> list[dict[str, Any]]:
        arguments.setdefault("tags", WATCHED)
        arguments.setdefault("count", 500)
        return self._request("pyrungTagChanges", arguments)["body"]["entries"]


@pytest.fixture(scope="module")
def stepped(tmp_path_factory: pytest.TempPathFactory) -> Session:
    return Session(tmp_path_factory.mktemp("stepped"), jit=False)


@pytest.fixture
def compiled(tmp_path: Path) -> Session:
    return Session(tmp_path, jit=True)


def _scan_by_scan(session: Session, names: list[str]) -> list[dict[str, Any]]:
    """The panel's entries as the definition has them: each scan's state against the one before."""
    history = session.runner.history
    entries = []
    previous = history.at(history.oldest_scan_id)
    for scan_id in range(history.oldest_scan_id + 1, history.newest_scan_id + 1):
        state = history.at(scan_id)
        changes = {
            name: [
                session.adapter._format_value(previous.tags.get(name)),
                session.adapter._format_value(state.tags.get(name)),
            ]
            for name in names
            if previous.tags.get(name) != state.tags.get(name)
        }
        if changes:
            entries.append(
                {
                    "scanId": scan_id,
                    "prevScanId": scan_id - 1,
                    "timestamp": state.timestamp,
                    "changes": changes,
                }
            )
        previous = state
    entries.reverse()
    return entries


def test_compiled_entries_are_the_stepped_entries(stepped: Session, compiled: Session) -> None:
    assert compiled.runner._unrecorded_scans, "the run should have left scans to read"
    expected = stepped.changes()
    assert compiled.changes() == expected
    assert expected == _scan_by_scan(stepped, WATCHED)


def test_a_transition_keeps_its_own_scan_and_a_pulse_is_not_lost(compiled: Session) -> None:
    by_scan = {entry["scanId"]: entry for entry in compiled.changes()}
    lamp = [scan for scan, entry in by_scan.items() if "Lamp" in entry["changes"]]
    assert lamp == [3005]
    assert by_scan[3005]["prevScanId"] == 3004
    assert by_scan[3005]["changes"]["Lamp"] == ["False", "True"]

    rises = sorted(
        scan for scan, entry in by_scan.items() if entry["changes"].get("T2_Done", [0, 0])[1] == "True"
    )
    # One scan up, one scan down, every time round: the timer holds its Acc for neither.
    assert len(rises) >= 39
    assert all(by_scan[scan + 1]["changes"]["T2_Done"] == ["True", "False"] for scan in rises)


def test_a_moving_tag_reads_the_same(stepped: Session, compiled: Session) -> None:
    assert compiled.changes(tags=["T2_Acc"], count=30) == stepped.changes(tags=["T2_Acc"], count=30)


def test_pages_add_up_to_the_stepped_entries(stepped: Session, compiled: Session) -> None:
    expected = stepped.changes()
    pages: list[dict[str, Any]] = []
    while True:
        arguments = {"beforeScan": pages[-1]["scanId"]} if pages else {}
        page = compiled.changes(count=5, **arguments)
        assert not any(entry.get("window") for entry in page)
        pages.extend(page)
        if len(page) < 5:
            break
    assert pages == expected


def test_a_page_between_two_scans(stepped: Session, compiled: Session) -> None:
    bounds = {"afterScan": 1500, "beforeScan": 3006}
    assert compiled.changes(**bounds) == stepped.changes(**bounds)


def test_a_request_out_of_allowance_claims_no_scan(
    stepped: Session, compiled: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = {entry["scanId"]: entry for entry in stepped.changes()}
    monkeypatch.setattr(history_seek, "_SETTLE_CLOCK_S", 0.0)

    entries = compiled.changes()

    *read, window = entries
    assert window["window"] is True
    assert all(not entry.get("window") for entry in read)
    # What it has listed is true, and is what the scans after the window hold.
    assert all(entry == expected[entry["scanId"]] for entry in read)
    assert {entry["scanId"] for entry in read} == {
        scan for scan in expected if scan > window["scanId"]
    }
    # A window says how the tags stand at its ends, which stepping agrees with.
    history = stepped.runner.history
    start, end = history.at(window["prevScanId"]), history.at(window["scanId"])
    assert window["timestamp"] == end.timestamp
    assert window["changes"] == {
        name: [
            stepped.adapter._format_value(start.tags.get(name)),
            stepped.adapter._format_value(end.tags.get(name)),
        ]
        for name in WATCHED
        if start.tags.get(name) != end.tags.get(name)
    }


def test_loading_a_window_reaches_the_stepped_entries(
    stepped: Session, compiled: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(history_seek, "_SETTLE_CLOCK_S", 0.0)
    *entries, window = compiled.changes()
    monkeypatch.undo()

    entries += compiled.changes(beforeScan=window["scanId"] + 1)

    assert entries == stepped.changes()


def test_a_refresh_does_not_pay_again_after_it_ran_out(
    compiled: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(history_seek, "_SETTLE_CLOCK_S", 0.0)
    first = compiled.changes()
    monkeypatch.undo()
    left = list(compiled.runner._unrecorded_scans)

    assert compiled.changes() == first
    assert compiled.runner._unrecorded_scans == left

    # Asked for by name, the page goes on, and the next refresh has it.
    window = first[-1]
    older = compiled.changes(beforeScan=window["scanId"] + 1)
    assert compiled.changes() == first[:-1] + older


def test_stepping_on_after_a_long_run(tmp_path: Path, compiled: Session) -> None:
    other = Session(tmp_path, jit=False)
    for session in (compiled, other):
        session.changes()
        for _ in range(3):
            session._request("pyrungStepScan", {})
    assert compiled.changes() == other.changes()


@pytest.mark.parametrize("realtime", [False, True])
def test_a_timestamp_added_up_is_the_scans_own(realtime: bool) -> None:
    Tick = Bool("Tick")
    with Program(strict=False) as program:
        with Rung(~Tick):
            out(Tick)
    runner = PLC(program, realtime=True) if realtime else PLC(program, dt=0.01)
    assert runner.time_mode == (TimeMode.REALTIME if realtime else TimeMode.FIXED_STEP)
    runner.run(cycles=450)

    newest = runner.history.newest_scan_id
    # The added-up answer first: history.at may leave the state in the cache it reads from.
    added = {scan: runner._timestamp_at(scan) for scan in range(1, newest + 1, 7)}
    added[newest] = runner._timestamp_at(newest)
    assert added == {scan: runner.history.at(scan).timestamp for scan in added}


def test_a_fork_reads_its_parents_timestamps() -> None:
    Tick = Bool("Tick")
    with Program(strict=False) as program:
        with Rung(~Tick):
            out(Tick)
    runner = PLC(program, dt=0.01)
    runner.run(cycles=450)
    fork = runner.fork_from(300)
    fork.run(cycles=5)
    assert fork._timestamp_at(250) == runner.history.at(250).timestamp
    assert fork._timestamp_at(305) == fork.history.at(305).timestamp
