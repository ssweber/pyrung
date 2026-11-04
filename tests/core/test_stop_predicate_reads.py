"""What a traced stop predicate read, by every way into a state, and what a coast does with it.

A coast skips judging an opaque predicate only while nothing it read moves, so a read that
goes unrecorded is a false landing.  The loop's soundness argument rests on this list, and a
predicate that reads only tags has to stay in the loop.
"""

from __future__ import annotations

import copy
import pickle
from typing import Any

import pytest
from pyrsistent import PMap, pmap

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
    "tag map keys": (lambda s: list(s.tags.keys()), {_READ_ALL}),
    "tag map values": (lambda s: list(s.tags.values()), {_READ_ALL}),
    "tag map items": (lambda s: list(s.tags.items()), {_READ_ALL}),
    "tag map counted": (lambda s: len(s.tags), {_READ_ALL}),
    "tag map truth": (lambda s: bool(s.tags), {_READ_ALL}),
    "tag map set": (lambda s: s.tags.set("z", 1), {_READ_ALL}),
    "tag map update": (lambda s: s.tags.update({"z": 1}), {_READ_ALL}),
    "tag map remove": (lambda s: s.tags.remove("Lamp"), {_READ_ALL}),
    "tag map plus": (lambda s: s.tags + {"z": 1}, {_READ_ALL}),
    "tag map evolver": (lambda s: s.tags.evolver(), {_READ_ALL}),
    "map method named": (lambda s: PMap.__getitem__(s.tags, "Lamp"), {_READ_ALL}),
    "map in named": (lambda s: PMap.__contains__(s.tags, "Lamp"), {_READ_ALL}),
    "map items named": (lambda s: list(PMap.iteritems(s.tags)), {_READ_ALL}),
    "map length named": (lambda s: PMap.__len__(s.tags), {_READ_ALL}),
    "map equality named": (lambda s: PMap.__eq__(s.tags, pmap()), {_READ_ALL}),
    "map hash named": (lambda s: PMap.__hash__(s.tags), {_READ_ALL}),
    "map evolver named": (lambda s: PMap.evolver(s.tags), {_READ_ALL}),
    "map buckets by name": (lambda s: s.tags._buckets, {_READ_ALL}),
    "map size by name": (lambda s: s.tags._size, {_READ_ALL}),
    "state equality": (lambda s: s == SAVED, EVERY_FIELD),
    "state inequality": (lambda s: s != SAVED, EVERY_FIELD),
    "state equality the other way": (lambda s: SAVED == s, EVERY_FIELD),
    "state against a dict": (lambda s: s == dict(SAVED), EVERY_FIELD),
    "state hashed": (lambda s: hash(s) == 0, EVERY_FIELD),
    "state items": (lambda s: list(s.items()) == [], EVERY_FIELD),
    "state values": (lambda s: list(s.values()) == [], EVERY_FIELD),
    "state as a dict": (lambda s: dict(s) == {}, EVERY_FIELD),
    "state repr": (lambda s: repr(s) == "", EVERY_FIELD),
    "state copied": (lambda s: copy.copy(s), EVERY_FIELD),
    "state deep copied": (lambda s: copy.deepcopy(s), EVERY_FIELD),
    "state pickled": (lambda s: pickle.dumps(s), EVERY_FIELD),
    "state set": (lambda s: s.set(scan_id=0), EVERY_FIELD),
    "state with tags": (lambda s: s.with_tags({"z": 1}), EVERY_FIELD),
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
    assert _traced_call(lambda s: repr(s) == repr(state), plc)[0] is True


def test_the_state_and_map_say_they_are_the_real_ones() -> None:
    plc = _plc()
    assert _traced_call(lambda s: s.__class__ is SystemState, plc)[0] is True
    assert _traced_call(lambda s: s.tags.__class__ is PMap, plc)[0] is True
    assert _traced_call(lambda s: type(s).__name__ == "SystemState", plc)[0] is True
    assert _traced_call(lambda s: type(s.tags).__name__ == "PMap", plc)[0] is True
    assert _traced_call(lambda s: isinstance(s, SystemState), plc)[0] is True
    assert _traced_call(lambda s: isinstance(s.tags, PMap), plc)[0] is True


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
    # A scan first, as a coast does: it does not judge the state it starts from.
    plc = _plc()
    for _ in range(SCANS):
        plc.step()
        if predicate(plc.state):
            break
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
        pytest.param(lambda s: any(k == ACC and s.tags[k] >= 20 for k in s.tags.keys()), id="keys"),
        pytest.param(lambda s: dict(s.tags.items())[ACC] >= 20, id="map items"),
        pytest.param(lambda s: 20 in s.tags.values(), id="map values"),
        pytest.param(lambda s: copy.copy(s).tags[ACC] >= 20, id="state copied"),
        pytest.param(lambda s: copy.deepcopy(s).tags[ACC] >= 20, id="state deep copied"),
        pytest.param(lambda s: pickle.loads(pickle.dumps(s)).tags[ACC] >= 20, id="state pickled"),
        pytest.param(lambda s: s.set(scan_id=0).tags[ACC] >= 20, id="state set"),
        pytest.param(lambda s: s.with_tags({"z": 1}).tags[ACC] >= 20, id="state with tags"),
        pytest.param(lambda s: s.next_scan(0.01).tags[ACC] >= 20, id="state next scan"),
        pytest.param(lambda s: type(s)(tags=s.tags).tags[ACC] >= 20, id="state rebuilt"),
        pytest.param(lambda s: PMap.__getitem__(s.tags, ACC) >= 20, id="map method named"),
        pytest.param(lambda s: dict(PMap.iteritems(s.tags))[ACC] >= 20, id="map items named"),
        pytest.param(lambda s: PMap._getitem(s.tags._buckets, ACC) >= 20, id="map buckets"),
        pytest.param(
            lambda s: PMap._getitem(object.__getattribute__(s.tags, "_buckets"), ACC) >= 20,
            id="map buckets past getattr",
        ),
        pytest.param(
            lambda s: s.__class__ is SystemState and s.tags[ACC] >= 20, id="state class"
        ),
        pytest.param(lambda s: s.tags.__class__ is PMap and s.tags[ACC] >= 20, id="map class"),
        pytest.param(
            lambda s: type(s).__name__ == "SystemState" and s.tags[ACC] >= 20, id="state type name"
        ),
        pytest.param(
            lambda s: type(s.tags).__name__ == "PMap" and s.tags[ACC] >= 20, id="map type name"
        ),
    ],
)
def test_a_predicate_reading_what_moves_lands_as_stepping(predicate: Any) -> None:
    plc = _plc()
    assert coast_until(plc, predicate, budget=SCANS) is True
    _assert_landed_as_stepping(plc, _stepped_until(predicate))


@pytest.mark.parametrize(
    "predicate",
    [
        pytest.param(lambda s: SystemState.tags.__get__(s)[ACC] >= 20, id="tags slot"),
        pytest.param(lambda s: SystemState.scan_id.__get__(s) >= 1777, id="scan_id slot"),
        pytest.param(lambda s: SystemState.timestamp.__get__(s) >= 17.77, id="timestamp slot"),
        pytest.param(lambda s: len(SystemState.memory.__get__(s)) > 99, id="memory slot"),
        pytest.param(lambda s: PMap._getitem(PMap._buckets.__get__(s.tags), ACC) >= 20, id="map slot"),
        pytest.param(
            lambda s: (
                super(type(s), s).tags[ACC] >= 20
                if type(s) is not SystemState
                else s.tags[ACC] >= 20
            ),
            id="tags past the traced class",
        ),
        pytest.param(
            lambda s: (
                super(type(s), s).scan_id >= 1777
                if type(s) is not SystemState
                else s.scan_id >= 1777
            ),
            id="scan_id past the traced class",
        ),
    ],
)
def test_a_read_of_the_slots_themselves_lands_as_stepping(predicate: Any) -> None:
    plc = _plc()
    coast_until(plc, predicate, budget=SCANS)
    _assert_landed_as_stepping(plc, _stepped_until(predicate))


def test_the_tags_slot_records_and_the_others_refuse() -> None:
    plc = _plc()
    assert _traced_call(lambda s: SystemState.tags.__get__(s)["Lamp"], plc)[1] == {"Lamp"}
    for name in FIELD:
        slot: Any = getattr(SystemState, name)
        with pytest.raises(AttributeError):
            _traced_call(slot.__get__, plc)


def test_what_evolving_a_traced_state_makes_answers_as_the_real_one_does() -> None:
    plc = _plc()
    state = plc.state

    def same(made: Any) -> bool:
        return (made.scan_id, made.timestamp, dict(made.tags), made.memory) == (
            state.scan_id,
            state.timestamp,
            dict(state.tags),
            state.memory,
        )

    assert _traced_call(lambda s: s.set() is s, plc)[0] is True
    assert _traced_call(lambda s: same(copy.copy(s)), plc)[0] is True
    assert _traced_call(lambda s: same(copy.deepcopy(s)), plc)[0] is True
    assert _traced_call(lambda s: same(pickle.loads(pickle.dumps(s))), plc)[0] is True
    assert _traced_call(lambda s: same(s.set(tags=pmap(dict(s.tags)))), plc)[0] is True
    assert _traced_call(lambda s: s.next_scan(0.5).scan_id == state.scan_id + 1, plc)[0] is True


def test_a_predicate_hashing_the_state_lands_as_stepping() -> None:
    # String hashes are salted per process, so it may not hit: the two must agree either way.
    def predicate(s: Any) -> bool:
        return hash(s) % 997 == 0

    plc = _plc()
    assert coast_until(plc, predicate, budget=SCANS) is predicate(plc.state)
    _assert_landed_as_stepping(plc, _stepped_until(predicate))
