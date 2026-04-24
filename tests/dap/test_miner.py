"""Tests for the invariant miner and review console verbs."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from pyrung.core import PLC, Bool, Int, Program, Rung, latch, out
from pyrung.core.physical import Physical
from pyrung.dap.adapter import DAPAdapter
from pyrung.dap.capture import CaptureEntry
from pyrung.dap.miner import Candidate, _transitive_reduce, mine_candidates
from pyrung.dap.protocol import read_message

# ---------------------------------------------------------------------------
# Helpers (same pattern as test_capture.py)
# ---------------------------------------------------------------------------


def _drain_messages(stream: io.BytesIO) -> list[dict[str, Any]]:
    data = stream.getvalue()
    reader = io.BytesIO(data)
    messages: list[dict[str, Any]] = []
    while True:
        message = read_message(reader)
        if message is None:
            break
        messages.append(message)
    stream.seek(0)
    stream.truncate(0)
    return messages


def _send_request(
    adapter: DAPAdapter,
    out_stream: io.BytesIO,
    *,
    seq: int,
    command: str,
    arguments: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    adapter.handle_request(
        {"seq": seq, "type": "request", "command": command, "arguments": arguments or {}}
    )
    return _drain_messages(out_stream)


def _single_response(messages: list[dict[str, Any]]) -> dict[str, Any]:
    responses = [msg for msg in messages if msg.get("type") == "response"]
    assert len(responses) == 1
    return responses[0]


def _repl(
    adapter: DAPAdapter, out_stream: io.BytesIO, expression: str, *, seq: int = 10
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    messages = _send_request(
        adapter,
        out_stream,
        seq=seq,
        command="evaluate",
        arguments={"expression": expression, "context": "repl"},
    )
    response = _single_response(messages)
    stopped = [
        msg for msg in messages if msg.get("type") == "event" and msg.get("event") == "stopped"
    ]
    return response, stopped


def _runner_script() -> str:
    return (
        "from pyrung.core import Bool, Int, PLC, Program, Rung, out, copy\n"
        "\n"
        "button = Bool('Button')\n"
        "light = Bool('Light')\n"
        "counter = Int('Counter')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung(button):\n"
        "        out(light)\n"
        "    with Rung():\n"
        "        copy(0, counter)\n"
        "\n"
        "runner = PLC(prog, dt=0.010)\n"
    )


def _setup(tmp_path: Path) -> tuple[DAPAdapter, io.BytesIO]:
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script_path = tmp_path / "logic.py"
    script_path.write_text(_runner_script(), encoding="utf-8")
    _send_request(
        adapter, out_stream, seq=1, command="launch", arguments={"program": str(script_path)}
    )
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)
    return adapter, out_stream


# ---------------------------------------------------------------------------
# Unit tests: mine_candidates with pure PLC
# ---------------------------------------------------------------------------


class TestEdgeCorrelation:
    def test_basic_button_light(self):
        button = Bool("Button")
        light = Bool("Light")

        with Program(strict=False) as prog:
            with Rung(button):
                out(light)

        plc = PLC(prog, dt=0.010)
        plc.step()

        entries: list[CaptureEntry] = []
        scan_base = plc.current_state.scan_id

        plc.patch({"Button": True})
        plc.step()
        entries.append(CaptureEntry("patch Button true", plc.current_state.scan_id, 0.0))

        plc.step()
        entries.append(CaptureEntry("step 1", plc.current_state.scan_id, 0.0))

        plc.patch({"Button": False})
        plc.step()
        entries.append(CaptureEntry("patch Button false", plc.current_state.scan_id, 0.0))

        plc.step()
        entries.append(CaptureEntry("step 1", plc.current_state.scan_id, 0.0))

        candidates = mine_candidates("test", entries, plc, start_scan_id=scan_base)
        edge_cands = [c for c in candidates if c.kind == "edge_correlation"]
        assert len(edge_cands) >= 1
        descs = [c.description for c in edge_cands]
        assert any("Button" in d and "Light" in d for d in descs)

    def test_dt_stamped(self):
        button = Bool("Button")
        light = Bool("Light")

        with Program(strict=False) as prog:
            with Rung(button):
                out(light)

        plc = PLC(prog, dt=0.005)
        plc.step()
        scan_base = plc.current_state.scan_id

        entries: list[CaptureEntry] = []
        for _ in range(2):
            plc.patch({"Button": True})
            plc.step()
            entries.append(CaptureEntry("patch Button true", plc.current_state.scan_id, 0.0))
            plc.step()
            entries.append(CaptureEntry("step 1", plc.current_state.scan_id, 0.0))
            plc.patch({"Button": False})
            plc.step()
            entries.append(CaptureEntry("patch Button false", plc.current_state.scan_id, 0.0))
            plc.step()
            entries.append(CaptureEntry("step 1", plc.current_state.scan_id, 0.0))

        candidates = mine_candidates("test", entries, plc, start_scan_id=scan_base)
        for c in candidates:
            assert c.dt_seconds == 0.005

    def test_no_candidates_empty_recording(self):
        button = Bool("Button")
        light = Bool("Light")
        with Program(strict=False) as prog:
            with Rung(button):
                out(light)
        plc = PLC(prog, dt=0.010)
        assert mine_candidates("test", [], plc) == []


class TestPhysicsFloor:
    def test_floor_rejects_impossible(self):
        enable = Bool("Enable")
        feedback = Bool(
            "Feedback",
            physical=Physical("Feedback", on_delay="2s"),
            link="Enable",
        )

        with Program(strict=False) as prog:
            with Rung(enable):
                out(feedback)

        plc = PLC(prog, dt=0.010)
        plc.step()
        scan_base = plc.current_state.scan_id

        entries: list[CaptureEntry] = []
        for _ in range(2):
            plc.patch({"Enable": True})
            plc.patch({"Feedback": True})
            plc.step()
            entries.append(CaptureEntry("patch Enable true", plc.current_state.scan_id, 0.0))
            plc.patch({"Enable": False})
            plc.patch({"Feedback": False})
            plc.step()
            entries.append(CaptureEntry("patch Enable false", plc.current_state.scan_id, 0.0))

        candidates = mine_candidates("test", entries, plc, start_scan_id=scan_base)
        edge_cands = [
            c
            for c in candidates
            if c.kind == "edge_correlation"
            and c.antecedent_tag == "Enable"
            and c.consequent_tag == "Feedback"
        ]
        for c in edge_cands:
            assert c.physics_floor_scans is None or c.observed_delay_scans >= c.physics_floor_scans

    def test_floor_annotated(self):
        enable = Bool("Enable")
        feedback = Bool(
            "Feedback",
            physical=Physical("Feedback", on_delay="100ms"),
            link="Enable",
        )

        with Program(strict=False) as prog:
            with Rung(enable):
                out(feedback)

        plc = PLC(prog, dt=0.010)
        plc.step()
        scan_base = plc.current_state.scan_id

        entries: list[CaptureEntry] = []
        for _ in range(3):
            plc.patch({"Enable": True})
            plc.step()
            entries.append(CaptureEntry("patch Enable true", plc.current_state.scan_id, 0.0))
            plc.step()
            entries.append(CaptureEntry("step 1", plc.current_state.scan_id, 0.0))
            plc.patch({"Enable": False})
            plc.step()
            entries.append(CaptureEntry("patch Enable false", plc.current_state.scan_id, 0.0))
            plc.step()
            entries.append(CaptureEntry("step 1", plc.current_state.scan_id, 0.0))

        candidates = mine_candidates("test", entries, plc, start_scan_id=scan_base)
        fb_rising = [
            c
            for c in candidates
            if c.kind == "edge_correlation"
            and c.consequent_tag == "Feedback"
            and "^" in c.description
            and "Feedback^" in c.description
        ]
        for c in fb_rising:
            if c.physics_floor_scans is not None:
                assert c.physics_floor_scans == 10


class TestSteadyImplication:
    def test_basic_implication(self):
        running = Bool("Running")
        fault = Bool("Fault")
        trigger = Bool("Trigger")

        with Program(strict=False) as prog:
            with Rung(trigger):
                out(running)
            with Rung(running):
                out(fault)

        plc = PLC(prog, dt=0.010)
        plc.step()
        scan_base = plc.current_state.scan_id

        plc.patch({"Trigger": True})
        entries: list[CaptureEntry] = []
        entries.append(CaptureEntry("patch Trigger true", plc.current_state.scan_id, 0.0))

        for _ in range(5):
            plc.step()
            entries.append(CaptureEntry("step 1", plc.current_state.scan_id, 0.0))

        candidates = mine_candidates("test", entries, plc, start_scan_id=scan_base)
        impl_cands = [c for c in candidates if c.kind == "steady_implication"]
        descs = [c.description for c in impl_cands]
        assert any("Running" in d and "Fault" in d for d in descs)

    def test_implication_not_proposed_when_violated(self):
        running = Bool("Running")
        fault = Bool("Fault")
        overtemp = Bool("Overtemp")

        with Program(strict=False) as prog:
            with Rung(running):
                out(running)
            with Rung(overtemp):
                latch(fault)

        plc = PLC(prog, dt=0.010)
        plc.step()
        scan_base = plc.current_state.scan_id

        entries: list[CaptureEntry] = []

        plc.patch({"Running": True})
        entries.append(CaptureEntry("patch Running true", plc.current_state.scan_id, 0.0))
        for _ in range(3):
            plc.step()
            entries.append(CaptureEntry("step 1", plc.current_state.scan_id, 0.0))

        plc.patch({"Overtemp": True})
        entries.append(CaptureEntry("patch Overtemp true", plc.current_state.scan_id, 0.0))
        plc.step()
        entries.append(CaptureEntry("step 1", plc.current_state.scan_id, 0.0))

        plc.step()
        entries.append(CaptureEntry("step 1", plc.current_state.scan_id, 0.0))

        candidates = mine_candidates("test", entries, plc, start_scan_id=scan_base)
        bad = [
            c
            for c in candidates
            if c.kind == "steady_implication"
            and c.antecedent_tag == "Running"
            and "~Fault" in c.description
        ]
        assert len(bad) == 0


class TestForcedTagFiltering:
    def test_forced_antecedent_excluded(self):
        running = Bool("Running")
        light = Bool("Light")
        trigger = Bool("Trigger")

        with Program(strict=False) as prog:
            with Rung(trigger):
                out(running)
            with Rung(running):
                out(light)

        plc = PLC(prog, dt=0.010)
        plc.step()
        scan_base = plc.current_state.scan_id

        plc.force("Running", True)
        entries: list[CaptureEntry] = []
        for _ in range(5):
            plc.step()
            entries.append(CaptureEntry("step 1", plc.current_state.scan_id, 0.0))

        candidates = mine_candidates("test", entries, plc, start_scan_id=scan_base)
        impl_cands = [c for c in candidates if c.kind == "steady_implication"]
        antecedents = {c.antecedent_tag for c in impl_cands}
        assert "Running" not in antecedents

    def test_forced_consequent_excluded(self):
        running = Bool("Running")
        light = Bool("Light")
        trigger = Bool("Trigger")

        with Program(strict=False) as prog:
            with Rung(trigger):
                out(running)
            with Rung(running):
                out(light)

        plc = PLC(prog, dt=0.010)
        plc.step()
        scan_base = plc.current_state.scan_id

        plc.force("Light", True)
        plc.patch({"Trigger": True})
        entries: list[CaptureEntry] = []
        for _ in range(5):
            plc.step()
            entries.append(CaptureEntry("step 1", plc.current_state.scan_id, 0.0))

        candidates = mine_candidates("test", entries, plc, start_scan_id=scan_base)
        impl_cands = [c for c in candidates if c.kind == "steady_implication"]
        consequents = {c.consequent_tag for c in impl_cands}
        assert "Light" not in consequents

    def test_partial_force_not_excluded(self):
        running = Bool("Running")
        fault = Bool("Fault")

        with Program(strict=False) as prog:
            with Rung(running):
                out(running)
            with Rung(running):
                out(fault)

        plc = PLC(prog, dt=0.010)
        plc.step()
        scan_base = plc.current_state.scan_id

        entries: list[CaptureEntry] = []
        plc.patch({"Running": True})
        entries.append(CaptureEntry("patch Running true", plc.current_state.scan_id, 0.0))
        for _ in range(3):
            plc.step()
            entries.append(CaptureEntry("step 1", plc.current_state.scan_id, 0.0))
        # Force Running partway through — not entire window
        plc.force("Running", True)
        for _ in range(3):
            plc.step()
            entries.append(CaptureEntry("step 1", plc.current_state.scan_id, 0.0))

        candidates = mine_candidates("test", entries, plc, start_scan_id=scan_base)
        impl_cands = [c for c in candidates if c.kind == "steady_implication"]
        antecedents = {c.antecedent_tag for c in impl_cands}
        assert "Running" in antecedents


class TestValueTemporal:
    def test_basic_value_temporal(self):
        state = Int("State")
        motor = Bool("MotorOut")

        with Program(strict=False) as prog:
            with Rung(state):
                out(motor)

        plc = PLC(prog, dt=0.010)
        plc.step()
        scan_base = plc.current_state.scan_id

        entries: list[CaptureEntry] = []
        for _ in range(3):
            plc.patch({"State": 2})
            plc.step()
            entries.append(CaptureEntry("patch State 2", plc.current_state.scan_id, 0.0))
            plc.step()
            entries.append(CaptureEntry("step 1", plc.current_state.scan_id, 0.0))
            plc.patch({"State": 0})
            plc.step()
            entries.append(CaptureEntry("patch State 0", plc.current_state.scan_id, 0.0))
            plc.step()
            entries.append(CaptureEntry("step 1", plc.current_state.scan_id, 0.0))

        candidates = mine_candidates("test", entries, plc, start_scan_id=scan_base)
        vt_cands = [c for c in candidates if c.kind == "value_temporal"]
        descs = [c.description for c in vt_cands]
        assert any("State" in d and "MotorOut" in d for d in descs)


class TestSuppression:
    def test_suppressed_not_reproposed(self):
        button = Bool("Button")
        light = Bool("Light")

        with Program(strict=False) as prog:
            with Rung(button):
                out(light)

        plc = PLC(prog, dt=0.010)
        plc.step()
        scan_base = plc.current_state.scan_id

        entries: list[CaptureEntry] = []
        for _ in range(2):
            plc.patch({"Button": True})
            plc.step()
            entries.append(CaptureEntry("patch Button true", plc.current_state.scan_id, 0.0))
            plc.step()
            entries.append(CaptureEntry("step 1", plc.current_state.scan_id, 0.0))
            plc.patch({"Button": False})
            plc.step()
            entries.append(CaptureEntry("patch Button false", plc.current_state.scan_id, 0.0))
            plc.step()
            entries.append(CaptureEntry("step 1", plc.current_state.scan_id, 0.0))

        first = mine_candidates("test", entries, plc, start_scan_id=scan_base)
        assert len(first) > 0

        suppressed = frozenset(c.formula for c in first)
        second = mine_candidates(
            "test", entries, plc, start_scan_id=scan_base, suppressed=suppressed
        )
        assert len(second) == 0


# ---------------------------------------------------------------------------
# Integration tests: DAP adapter
# ---------------------------------------------------------------------------


class TestRecordStopCandidates:
    def test_record_stop_shows_count(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "record test_action", seq=10)
        _repl(adapter, out, "patch Button true", seq=11)
        _repl(adapter, out, "step 3", seq=12)
        _repl(adapter, out, "patch Button false", seq=13)
        _repl(adapter, out, "step 3", seq=14)
        _repl(adapter, out, "patch Button true", seq=15)
        _repl(adapter, out, "step 3", seq=16)
        _repl(adapter, out, "patch Button false", seq=17)
        _repl(adapter, out, "step 3", seq=18)
        resp, _ = _repl(adapter, out, "record stop", seq=19)
        text = resp["body"]["result"]
        assert "Recording stopped" in text

    def test_candidates_verb(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "record test_action", seq=10)
        _repl(adapter, out, "patch Button true", seq=11)
        _repl(adapter, out, "step 3", seq=12)
        _repl(adapter, out, "patch Button false", seq=13)
        _repl(adapter, out, "step 3", seq=14)
        _repl(adapter, out, "patch Button true", seq=15)
        _repl(adapter, out, "step 3", seq=16)
        _repl(adapter, out, "patch Button false", seq=17)
        _repl(adapter, out, "step 3", seq=18)
        _repl(adapter, out, "record stop", seq=19)

        resp, _ = _repl(adapter, out, "candidates", seq=20)
        text = resp["body"]["result"]
        assert resp["success"]
        assert "c-" in text or "No pending" in text

    def test_accept_moves_to_accepted(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "record test_action", seq=10)
        _repl(adapter, out, "patch Button true", seq=11)
        _repl(adapter, out, "step 3", seq=12)
        _repl(adapter, out, "patch Button false", seq=13)
        _repl(adapter, out, "step 3", seq=14)
        _repl(adapter, out, "patch Button true", seq=15)
        _repl(adapter, out, "step 3", seq=16)
        _repl(adapter, out, "patch Button false", seq=17)
        _repl(adapter, out, "step 3", seq=18)
        _repl(adapter, out, "record stop", seq=19)

        if adapter._miner_candidates:
            cid = adapter._miner_candidates[0].id
            resp, _ = _repl(adapter, out, f"accept {cid}", seq=20)
            assert resp["success"]
            assert "Accepted" in resp["body"]["result"]
            assert len(adapter._miner_accepted) >= 1
            assert all(c.id != cid for c in adapter._miner_candidates)

    def test_deny_removes(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "record test_action", seq=10)
        _repl(adapter, out, "patch Button true", seq=11)
        _repl(adapter, out, "step 3", seq=12)
        _repl(adapter, out, "patch Button false", seq=13)
        _repl(adapter, out, "step 3", seq=14)
        _repl(adapter, out, "patch Button true", seq=15)
        _repl(adapter, out, "step 3", seq=16)
        _repl(adapter, out, "patch Button false", seq=17)
        _repl(adapter, out, "step 3", seq=18)
        _repl(adapter, out, "record stop", seq=19)

        if adapter._miner_candidates:
            cid = adapter._miner_candidates[0].id
            before = len(adapter._miner_candidates)
            resp, _ = _repl(adapter, out, f"deny {cid}", seq=20)
            assert resp["success"]
            assert "Denied" in resp["body"]["result"]
            assert len(adapter._miner_candidates) == before - 1

    def test_suppress_prevents_reappearance(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "record test_action", seq=10)
        _repl(adapter, out, "patch Button true", seq=11)
        _repl(adapter, out, "step 3", seq=12)
        _repl(adapter, out, "patch Button false", seq=13)
        _repl(adapter, out, "step 3", seq=14)
        _repl(adapter, out, "patch Button true", seq=15)
        _repl(adapter, out, "step 3", seq=16)
        _repl(adapter, out, "patch Button false", seq=17)
        _repl(adapter, out, "step 3", seq=18)
        _repl(adapter, out, "record stop", seq=19)

        if adapter._miner_candidates:
            cid = adapter._miner_candidates[0].id
            _repl(adapter, out, f"suppress {cid}", seq=20)
            assert len(adapter._miner_suppressed) >= 1

            _repl(adapter, out, "record test_action2", seq=21)
            _repl(adapter, out, "patch Button true", seq=22)
            _repl(adapter, out, "step 3", seq=23)
            _repl(adapter, out, "patch Button false", seq=24)
            _repl(adapter, out, "step 3", seq=25)
            _repl(adapter, out, "patch Button true", seq=26)
            _repl(adapter, out, "step 3", seq=27)
            _repl(adapter, out, "patch Button false", seq=28)
            _repl(adapter, out, "step 3", seq=29)
            _repl(adapter, out, "record stop", seq=30)

            formulas = {c.formula for c in adapter._miner_candidates}
            for s in adapter._miner_suppressed:
                assert s not in formulas

    def test_bad_id_errors(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "accept c-99", seq=10)
        assert not resp["success"]
        resp, _ = _repl(adapter, out, "deny c-99", seq=11)
        assert not resp["success"]
        resp, _ = _repl(adapter, out, "suppress c-99", seq=12)
        assert not resp["success"]

    def test_no_candidates_message(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "candidates", seq=10)
        assert resp["success"]
        assert "No pending" in resp["body"]["result"]


# ---------------------------------------------------------------------------
# Transitive reduction
# ---------------------------------------------------------------------------


def _make_impl(ant: str, cons: str, *, negated: bool = False) -> Candidate:
    neg = "~" if negated else ""
    return Candidate(
        id="",
        kind="steady_implication",
        description=f"{ant} => {neg}{cons}",
        formula=f"{ant} => {neg}{cons} [dt=0.01]",
        antecedent_tag=ant,
        consequent_tag=cons,
        observed_delay_scans=0,
        physics_floor_scans=None,
        dt_seconds=0.01,
        observation_count=5,
        violation_count=0,
        scan_range=(0, 10),
    )


def _make_edge(ant: str, cons: str) -> Candidate:
    return Candidate(
        id="",
        kind="edge_correlation",
        description=f"{ant}^ -> {cons}^ within 1 scan",
        formula=f"{ant}^ -> {cons}^ within 1 scans [dt=0.01]",
        antecedent_tag=ant,
        consequent_tag=cons,
        observed_delay_scans=1,
        physics_floor_scans=None,
        dt_seconds=0.01,
        observation_count=3,
        violation_count=0,
        scan_range=(0, 10),
    )


class TestTransitiveReduction:
    def test_basic_chain(self):
        """A => B, B => C, A => C → removes A => C."""
        cands = [_make_impl("A", "B"), _make_impl("B", "C"), _make_impl("A", "C")]
        result = _transitive_reduce(cands)
        formulas = {c.formula for c in result}
        assert "A => B [dt=0.01]" in formulas
        assert "B => C [dt=0.01]" in formulas
        assert "A => C [dt=0.01]" not in formulas

    def test_no_redundancy(self):
        """Independent implications are all kept."""
        cands = [_make_impl("A", "B"), _make_impl("C", "D")]
        result = _transitive_reduce(cands)
        assert len(result) == 2

    def test_equivalence_class_preserved(self):
        """A <=> B <=> C — all edges within an SCC are kept."""
        cands = [
            _make_impl("A", "B"),
            _make_impl("B", "A"),
            _make_impl("B", "C"),
            _make_impl("C", "B"),
            _make_impl("A", "C"),
            _make_impl("C", "A"),
        ]
        result = _transitive_reduce(cands)
        assert len(result) == 6

    def test_scc_hub_reduces_satellites(self):
        """A <=> B with shared targets: A => C, B => C → one removed."""
        cands = [
            _make_impl("A", "B"),
            _make_impl("B", "A"),
            _make_impl("A", "C"),
            _make_impl("B", "C"),
        ]
        result = _transitive_reduce(cands)
        impl_formulas = {c.formula for c in result if c.kind == "steady_implication"}
        assert "A => B [dt=0.01]" in impl_formulas
        assert "B => A [dt=0.01]" in impl_formulas
        # One of the C edges survives, the other is redundant
        c_edges = [c for c in result if c.consequent_tag == "C"]
        assert len(c_edges) == 1

    def test_negated_not_reduced(self):
        """Negated implications don't participate in reduction."""
        cands = [
            _make_impl("A", "B"),
            _make_impl("B", "C", negated=True),
            _make_impl("A", "C", negated=True),
        ]
        result = _transitive_reduce(cands)
        assert len(result) == 3

    def test_edge_correlations_unaffected(self):
        """Non-implication candidates pass through unchanged."""
        cands = [
            _make_impl("A", "B"),
            _make_impl("B", "C"),
            _make_impl("A", "C"),
            _make_edge("A", "C"),
        ]
        result = _transitive_reduce(cands)
        edge_cands = [c for c in result if c.kind == "edge_correlation"]
        assert len(edge_cands) == 1

    def test_fewer_than_three_unchanged(self):
        """With fewer than 3 positive implications, no reduction possible."""
        cands = [_make_impl("A", "B"), _make_impl("B", "C")]
        result = _transitive_reduce(cands)
        assert len(result) == 2


class TestLockFiltering:
    def test_lock_proven_filtered(self, tmp_path: Path):
        """Implications proven by lock file reachable states are removed."""
        import json

        lock_data = {
            "version": 1,
            "program_hash": "test",
            "projection": ["A", "B"],
            "reachable": [
                {"A": False, "B": False},
                {"A": True, "B": True},
            ],
        }
        lock_path = tmp_path / "pyrung.lock"
        lock_path.write_text(json.dumps(lock_data))

        a = Bool("A")
        b = Bool("B")
        with Program(strict=False) as prog:
            with Rung(a):
                out(b)

        plc = PLC(prog, dt=0.010)
        plc._program_path = str(tmp_path / "logic.py")
        plc.step()
        scan_base = plc.current_state.scan_id

        entries: list[CaptureEntry] = []
        plc.patch({"A": True})
        entries.append(CaptureEntry("patch A true", plc.current_state.scan_id, 0.0))
        for _ in range(5):
            plc.step()
            entries.append(CaptureEntry("step 1", plc.current_state.scan_id, 0.0))

        candidates = mine_candidates("test", entries, plc, start_scan_id=scan_base)
        impl_formulas = {c.formula for c in candidates if c.kind == "steady_implication"}
        # A => B and B => A are both proven by the lock (every reachable state satisfies both)
        assert not any("=> A" in f or "=> B" in f for f in impl_formulas)

    def test_lock_not_proven_kept(self, tmp_path: Path):
        """Implications NOT proven by lock are kept."""
        import json

        lock_data = {
            "version": 1,
            "program_hash": "test",
            "projection": ["A"],
            "reachable": [{"A": False}, {"A": True}],
        }
        lock_path = tmp_path / "pyrung.lock"
        lock_path.write_text(json.dumps(lock_data))

        a = Bool("A")
        b = Bool("B")
        with Program(strict=False) as prog:
            with Rung(a):
                out(b)

        plc = PLC(prog, dt=0.010)
        plc._program_path = str(tmp_path / "logic.py")
        plc.step()
        scan_base = plc.current_state.scan_id

        entries: list[CaptureEntry] = []
        plc.patch({"A": True})
        entries.append(CaptureEntry("patch A true", plc.current_state.scan_id, 0.0))
        for _ in range(5):
            plc.step()
            entries.append(CaptureEntry("step 1", plc.current_state.scan_id, 0.0))

        candidates = mine_candidates("test", entries, plc, start_scan_id=scan_base)
        impl_cands = [c for c in candidates if c.kind == "steady_implication"]
        # B is not in the lock projection, so implications involving B are kept
        assert len(impl_cands) > 0
