"""The history panel after a long compiled run: the same entries as stepping, or a window."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from pyrung.core import PLC, Bool, Program, Rung, TimeMode, out
from pyrung.core import runner as runner_module
from pyrung.dap.adapter import DAPAdapter
from pyrung.dap.handlers import history_seek
from pyrung.dap.protocol import read_message

WATCHED = ["Lamp", "Stage", "T2_Done", "Held", "Mirror", "Patched"]

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


# Alarm latches inside the stretch and resets on the first scan after it, and Count moves on
# both sides of every seam the panel's settling leaves.
SEAM_WATCHED = ["Alarm", "Count", "T1_Done", "T2_Done"]

_SEAM_SCRIPT = """
from pyrung.core import PLC, Bool, Int, Program, Rung, Timer, latch, math, on_delay, reset, rise

Enable = Bool("Enable")
Alarm = Bool("Alarm")
Ack = Bool("Ack")
Count = Int("Count")
T1 = Timer.clone("T1")
T2 = Timer.clone("T2")

with Program(strict=False) as prog:
    with Rung(Enable, ~T2.Done):
        on_delay(T2, preset=1000)
    with Rung(rise(T2.Done)):
        math(Count + 1, Count)
    with Rung(Enable):
        on_delay(T1, preset=15000)
    with Rung(T1.Done):
        latch(Alarm)
    with Rung(Ack):
        reset(Alarm)

runner = PLC(prog, dt=0.01)
runner.patch({"Enable": True})
runner.run_for(30.0, jit=JIT)
runner.patch({"Ack": True})
runner.step()
runner.step()
runner.patch({"Ack": False})
runner.step()
"""


class Session:
    def __init__(self, tmp_path: Path, *, jit: bool, script: str = _SCRIPT) -> None:
        self._out = io.BytesIO()
        self.adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=self._out)
        self._seq = 0
        name = "compiled.py" if jit else "stepped.py"
        path = tmp_path / name
        path.write_text(f"JIT = {jit}\n{script}", encoding="utf-8")
        self._request("launch", {"program": str(path)})

    @property
    def runner(self) -> Any:
        return self.adapter._runner

    def _request(
        self, command: str, arguments: dict[str, Any], *, expect_success: bool = True
    ) -> dict[str, Any]:
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
        assert response["success"] is True or not expect_success, response
        return response

    def changes(self, **arguments: Any) -> list[dict[str, Any]]:
        arguments.setdefault("tags", WATCHED)
        arguments.setdefault("count", 500)
        return self._request("pyrungTagChanges", arguments)["body"]["entries"]

    def chain(self, query: str) -> dict[str, Any]:
        return self._request("pyrungCausal", {"query": query}, expect_success=False)

    def console(self, line: str) -> str:
        response = self._request(
            "evaluate", {"expression": line, "context": "repl"}, expect_success=False
        )
        return response["body"]["result"] if response["success"] else response["message"]


@pytest.fixture(autouse=True)
def _allowance_outlasts_a_loaded_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The panel's clock is the user's wait; a test that wants it short sets it to 0 itself."""
    monkeypatch.setattr(history_seek, "_SETTLE_CLOCK_S", 300.0)
    monkeypatch.setattr(history_seek, "_READ_CLOCK_S", 300.0)


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
        scan
        for scan, entry in by_scan.items()
        if entry["changes"].get("T2_Done", [0, 0])[1] == "True"
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


@pytest.fixture(scope="module")
def seam_stepped(tmp_path_factory: pytest.TempPathFactory) -> Session:
    return Session(tmp_path_factory.mktemp("seam_stepped"), jit=False, script=_SEAM_SCRIPT)


@pytest.fixture
def seam_compiled(tmp_path: Path) -> Session:
    return Session(tmp_path, jit=True, script=_SEAM_SCRIPT)


def _seam_changes(session: Session, **arguments: Any) -> list[dict[str, Any]]:
    return session.changes(tags=SEAM_WATCHED, **arguments)


def test_a_stretch_has_what_the_seam_tests_are_for(seam_stepped: Session) -> None:
    changes = {entry["scanId"]: entry["changes"] for entry in _seam_changes(seam_stepped)}
    assert changes[1500]["Alarm"] == ["False", "True"]
    assert changes[3001]["Alarm"] == ["True", "False"]
    assert sum("Count" in change for change in changes.values()) > 10


@pytest.mark.parametrize("count", [1, 7, 50, 500])
def test_each_entry_of_a_first_refresh_is_the_stepped_entry(
    seam_stepped: Session, seam_compiled: Session, count: int
) -> None:
    assert seam_compiled.runner._unrecorded_scans
    assert _seam_changes(seam_compiled, count=count) == _seam_changes(seam_stepped, count=count)


@pytest.mark.parametrize("count", [1, 3, 7])
def test_each_entry_of_every_page_is_the_stepped_entry(
    seam_stepped: Session, seam_compiled: Session, count: int
) -> None:
    pages: list[dict[str, Any]] = []
    while True:
        arguments = {"beforeScan": pages[-1]["scanId"]} if pages else {}
        page = _seam_changes(seam_compiled, count=count, **arguments)
        pages.extend(page)
        if len(page) < count:
            break
    assert pages == _seam_changes(seam_stepped)


@pytest.mark.parametrize(
    "bounds",
    [
        {"afterScan": 1100, "beforeScan": 1111},
        {"afterScan": 1499, "beforeScan": 1501},
        {"afterScan": 1000, "beforeScan": 3002},
        {"afterScan": 2422, "beforeScan": 2424},
    ],
)
def test_a_page_inside_a_stretch_starts_from_the_stepped_value(
    seam_stepped: Session, seam_compiled: Session, bounds: dict[str, int]
) -> None:
    assert _seam_changes(seam_compiled, **bounds) == _seam_changes(seam_stepped, **bounds)


def test_the_entry_above_a_window_stays_the_stepped_entry_on_every_refresh(
    seam_stepped: Session, seam_compiled: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = {entry["scanId"]: entry for entry in _seam_changes(seam_stepped)}
    monkeypatch.setattr(history_seek, "_SETTLE_CLOCK_S", 0.0)
    first = _seam_changes(seam_compiled)
    monkeypatch.undo()

    *read, window = first
    assert window["window"] is True
    assert read, "an entry above the window is what this is about"
    assert 3001 in {entry["scanId"] for entry in read}
    for entry in read:
        assert entry == expected[entry["scanId"]]
    # Out of clock, the next refreshes read only what is recorded, and say the same.
    assert _seam_changes(seam_compiled) == first
    assert _seam_changes(seam_compiled) == first


@pytest.mark.parametrize("at", [3000, 1600, 1200])
def test_the_first_read_after_a_fork_is_the_stepped_entry(tmp_path: Path, at: int) -> None:
    sessions = []
    for jit in (False, True):
        session = Session(tmp_path, jit=jit, script=_SEAM_SCRIPT)
        session._request("pyrungForkAt", {"scanId": at})
        session.runner.patch({"Ack": True})
        session.runner.step()
        sessions.append(session)
    twin, compiled = sessions
    assert compiled.runner._unrecorded_in()
    assert twin.runner.current_state.scan_id == compiled.runner.current_state.scan_id == at + 1
    for count in (1, 7, 500):
        assert _seam_changes(compiled, count=count) == _seam_changes(twin, count=count)


def test_a_transition_read_above_unrecorded_scans_has_the_scans_own_old_value(
    seam_stepped: Session, seam_compiled: Session
) -> None:
    """Below the panel, as any reader of a committed transition has it."""
    assert seam_compiled.runner._unrecorded_in(1001, 3000)
    got = seam_compiled.runner._causal_committed_transition_at("Alarm", 3001)
    assert got == seam_stepped.runner._causal_committed_transition_at("Alarm", 3001)
    assert (got.from_value, got.to_value) == (True, False)


def _pages_down(session: Session, *, count: int) -> list[dict[str, Any]]:
    """Every change, a page at a time from the newest, the way Load older walks a window."""
    read: list[dict[str, Any]] = []
    before = None
    for _ in range(1000):
        arguments = {} if before is None else {"beforeScan": before}
        page = _seam_changes(session, count=count, **arguments)
        entries = [entry for entry in page if not entry.get("window")]
        read.extend(entries)
        windows = [entry for entry in page if entry.get("window")]
        if not entries and not windows:
            return read
        if windows:
            next_before = windows[0]["scanId"] + 1
        elif len(page) < count:
            return read
        else:
            next_before = entries[-1]["scanId"]
        assert before is None or next_before < before, "a page has to get somewhere"
        before = next_before
    raise AssertionError("paging did not end")


def test_reading_out_of_clock_shows_a_window_and_paging_gets_the_rest(
    seam_stepped: Session, seam_compiled: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = _seam_changes(seam_stepped)
    monkeypatch.setattr(history_seek, "_READ_CLOCK_S", 0.0)

    page = _seam_changes(seam_stepped)
    assert page[-1]["window"] is True
    assert page[:-1] == expected[: len(page) - 1]
    assert page[:-1], "each tag gets a first turn, however little clock there is"
    assert page[-1]["scanId"] < page[-2]["scanId"]
    assert _pages_down(seam_stepped, count=500) == expected
    assert _pages_down(seam_compiled, count=500) == expected


def test_a_read_history_cannot_answer_shows_a_window(
    seam_stepped: Session, seam_compiled: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = _seam_changes(seam_stepped)
    runner = seam_stepped.runner
    real = runner._causal_last_committed_transition_before
    below = expected[3]["scanId"]

    def refuse(name: str, before: int) -> Any:
        if before <= below:
            raise KeyError(before)
        return real(name, before)

    monkeypatch.setattr(runner, "_causal_last_committed_transition_before", refuse)
    page = _seam_changes(seam_stepped)
    assert page[-1]["window"] is True
    for entry in page[:-1]:
        assert entry in expected


ADVICE = "ask first about a scan nearer the start of that range for cause"


def test_the_refusal_says_what_works_from_the_console(
    stepped: Session, compiled: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "SETTLE_SCAN_LIMIT", 800)

    assert ADVICE in compiled.console("cause Lamp@3005")
    # Earlier scans first, as it says.  The scans each question steps keep their records.
    # Steps of 700, as lockstep's run keeps no recorded prefix for the walk to start from.
    for scan in (700, 1400, 2100, 2800):
        compiled.console(f"cause Lamp@{scan}")
    assert compiled.console("cause Lamp@3005") == stepped.console("cause Lamp@3005")


def test_the_refusal_says_what_works_from_the_chain_panel(
    stepped: Session, compiled: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "SETTLE_SCAN_LIMIT", 500)

    refused = compiled.chain("effect:T1_Done@3005")
    assert "nearer its end for effect" in str(refused)
    # A later scan first, and on towards the one asked about.
    compiled.chain("effect:T1_Done@3700")
    compiled.chain("effect:T1_Done@3300")
    answered = compiled.chain("effect:T1_Done@3005")
    expected = stepped.chain("effect:T1_Done@3005")
    assert answered["success"] is True
    assert answered["body"] == expected["body"]
