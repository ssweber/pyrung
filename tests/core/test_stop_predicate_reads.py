"""What a traced stop predicate read, by every way into a state, and what a coast does with it.

A coast skips judging an opaque predicate only while nothing it read moves, so a read that
goes unrecorded is a false landing.  The loop's soundness argument rests on this list, and a
predicate that reads only tags has to stay in the loop.
"""

from __future__ import annotations

from typing import Any

import pytest
from pyrsistent import pmap

from pyrung import Bool, Program, Rung, Timer, on_delay, out
from pyrung.core import SystemState
from pyrung.core.coast_run import coast_until
from pyrung.core.read_trace import _FIELD_READ, _READ_ALL, _traced_call
from pyrung.core.runner import PLC

pytestmark = pytest.mark.usefixtures("loop_from_first_scan")

Run = Bool("Run")
Lamp = Bool("Lamp")
T = Timer.clone("TraceT")
ACC = T.Acc.name  # in seconds: 20 at about scan 2000, inside SCANS

SCANS = 5_000
SAVED = SystemState(scan_id=1, timestamp=0.5, tags=pmap({"Run": True}), memory=pmap())


def _program() -> Program:
    with Program() as logic:
        with Rung(Run):
            on_delay(T, 30, "s")
        with Rung(T.Done):
            out(Lamp)
    return logic


def _plc() -> PLC:
    plc = PLC(_program(), dt=0.01)
    plc.patch({Run: True})
    plc.step()
    return plc


FIELD = {name: _FIELD_READ + name for name in ("scan_id", "timestamp", "memory")}
EVERY_FIELD = set(FIELD.values())  # any of them keeps a coast from skipping the predicate

READS: dict[str, tuple[Any, set[str]]] = {
    "tag attribute": (lambda s: s.tags["Lamp"], {"Lamp"}),
    "tag get": (lambda s: s.tags.get("Lamp"), {"Lamp"}),
    "tag in": (lambda s: "Lamp" in s.tags, {"Lamp"}),
    "tag by subscript": (lambda s: s["tags"]["Lamp"], {"Lamp"}),
    "tag by get": (lambda s: s.get("tags")["Lamp"], {"Lamp"}),
    "two tags": (lambda s: s.tags["Lamp"] or s.tags["Run"], {"Lamp", "Run"}),
    "no read": (lambda _s: False, set()),
    "field names": (lambda s: sorted(s) and len(s) and "tags" in s, set()),
    "scan_id": (lambda s: s.scan_id > 0, {FIELD["scan_id"]}),
    "scan_id by subscript": (lambda s: s["scan_id"] > 0, {FIELD["scan_id"]}),
    "timestamp": (lambda s: s.timestamp > 0, {FIELD["timestamp"]}),
    "timestamp by get": (lambda s: s.get("timestamp") > 0, {FIELD["timestamp"]}),
    "memory": (lambda s: s.memory.get("x"), {FIELD["memory"]}),
    "memory by subscript": (lambda s: s["memory"].get("x"), {FIELD["memory"]}),
    "whole tag map": (lambda s: dict(s.tags), {_READ_ALL, "Lamp", "Run"}),
    "tag map equality": (lambda s: s.tags == pmap(), {_READ_ALL}),
    "tag map iterated": (lambda s: list(s.tags), {_READ_ALL}),
    "tag map counted": (lambda s: len(s.tags), {_READ_ALL}),
    "tag map truth": (lambda s: bool(s.tags), {_READ_ALL}),
    "tag map set": (lambda s: s.tags.set("z", 1), {_READ_ALL}),
    "tag map update": (lambda s: s.tags.update({"z": 1}), {_READ_ALL}),
    "tag map remove": (lambda s: s.tags.remove("Lamp"), {_READ_ALL}),
    "tag map plus": (lambda s: s.tags + {"z": 1}, {_READ_ALL}),
    "tag map evolver": (lambda s: s.tags.evolver(), {_READ_ALL}),
    "state equality": (lambda s: s == SAVED, EVERY_FIELD),
    "state inequality": (lambda s: s != SAVED, EVERY_FIELD),
    "state equality the other way": (lambda s: SAVED == s, EVERY_FIELD),
    "state against a dict": (lambda s: s == dict(SAVED), EVERY_FIELD),
    "state hashed": (lambda s: hash(s) == 0, EVERY_FIELD),
    "state items": (lambda s: list(s.items()) == [], EVERY_FIELD),
    "state values": (lambda s: list(s.values()) == [], EVERY_FIELD),
    "state as a dict": (lambda s: dict(s) == {}, EVERY_FIELD),
    "state repr": (lambda s: repr(s) == "", EVERY_FIELD),
}


@pytest.mark.parametrize("name", sorted(READS))
def test_every_read_of_the_state_is_recorded(name: str) -> None:
    predicate, expected = READS[name]
    plc = _plc()
    tip = plc._state
    _hit, reads = _traced_call(predicate, plc)
    if expected == EVERY_FIELD or _READ_ALL in expected:
        assert reads >= expected
    else:
        assert reads == expected
    assert plc._state is tip


def test_the_answer_is_the_answer_on_the_same_values() -> None:
    plc = _plc()
    state = plc.state
    assert _traced_call(lambda s: s == state, plc)[0] is True
    assert _traced_call(lambda s: hash(s) == hash(state), plc)[0] is True
    assert _traced_call(lambda s: s.tags == state.tags, plc)[0] is True


def test_a_read_of_plc_state_instead_of_the_argument_is_recorded() -> None:
    plc = _plc()
    _hit, reads = _traced_call(lambda _s: plc.state.tags["Lamp"], plc)
    assert reads == {"Lamp"}


def test_the_tip_comes_back_when_the_predicate_raises() -> None:
    plc = _plc()
    tip = plc._state

    def boom(_state: Any) -> bool:
        raise ValueError

    with pytest.raises(ValueError):
        _traced_call(boom, plc)
    assert plc._state is tip


def _stepped_until(predicate: Any) -> PLC:
    plc = _plc()
    for _ in range(SCANS):
        if predicate(plc.state):
            break
        plc.step()
    return plc


def _assert_landed_as_stepping(plc: PLC, stepped: PLC) -> None:
    assert plc.state.scan_id == stepped.state.scan_id
    assert plc.state.timestamp == stepped.state.timestamp
    assert dict(plc.state.tags) == dict(stepped.state.tags)
    assert plc.state.memory == stepped.state.memory


@pytest.mark.parametrize(
    "predicate",
    [
        pytest.param(lambda s: s.tags["Lamp"], id="attribute"),
        pytest.param(lambda s: s.tags.get("Lamp"), id="get"),
        pytest.param(lambda s: s["tags"]["Lamp"], id="subscript"),
        pytest.param(lambda s: "Lamp" in s.tags and s.tags["Lamp"], id="in"),
    ],
)
def test_a_predicate_reading_tags_stays_in_the_loop(predicate: Any) -> None:
    plc = _plc()
    stats: dict[str, int] = {}
    assert coast_until(plc, predicate, budget=SCANS, stats=stats) is True
    assert stats["stretched_scans"] > 0
    _assert_landed_as_stepping(plc, _stepped_until(predicate))


def test_a_predicate_reading_plc_state_stays_in_the_loop() -> None:
    plc = _plc()
    stats: dict[str, int] = {}
    assert coast_until(plc, lambda _s: plc.state.tags["Lamp"], budget=SCANS, stats=stats) is True
    assert stats["stretched_scans"] > 0
    _assert_landed_as_stepping(plc, _stepped_until(lambda s: s.tags["Lamp"]))


@pytest.mark.parametrize(
    "predicate",
    [
        pytest.param(lambda s: s.scan_id >= 777, id="scan_id"),
        pytest.param(lambda s: s["scan_id"] >= 777, id="scan_id subscript"),
        pytest.param(lambda s: s.timestamp >= 7.77, id="timestamp"),
        pytest.param(lambda s: s.get("timestamp") >= 7.77, id="timestamp get"),
        pytest.param(lambda s: dict(s)["scan_id"] >= 777, id="as a dict"),
        pytest.param(lambda s: s.tags[ACC] >= 20, id="an accumulator"),
        pytest.param(lambda s: dict(s.items())["tags"][ACC] >= 20, id="items"),
        pytest.param(lambda s: s.tags.set("z", 1)[ACC] >= 20, id="map set"),
        pytest.param(lambda s: s.tags.update({"z": 1})[ACC] >= 20, id="map update"),
        pytest.param(lambda s: s.tags.remove("Run")[ACC] >= 20, id="map remove"),
        pytest.param(lambda s: (s.tags + {"z": 1})[ACC] >= 20, id="map plus"),
        pytest.param(lambda s: s.tags.evolver()[ACC] >= 20, id="map evolver"),
        pytest.param(lambda s: s.tags.transform(["z"], 1)[ACC] >= 20, id="map transform"),
    ],
)
def test_a_predicate_reading_what_moves_lands_as_stepping(predicate: Any) -> None:
    plc = _plc()
    assert coast_until(plc, predicate, budget=SCANS) is True
    _assert_landed_as_stepping(plc, _stepped_until(predicate))


def test_a_predicate_hashing_the_state_lands_as_stepping() -> None:
    # String hashes are salted per process, so it may not hit: the two must agree either way.
    def predicate(s: Any) -> bool:
        return hash(s) % 997 == 0

    plc = _plc()
    assert coast_until(plc, predicate, budget=SCANS) is predicate(plc.state)
    _assert_landed_as_stepping(plc, _stepped_until(predicate))
