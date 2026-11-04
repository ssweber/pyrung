"""``snapshot_dict``: whole-map read recording and the plain-mapping fallback."""

from __future__ import annotations

from types import MappingProxyType

from pyrung import PLC, Bool, Program, Rung, out
from pyrung.core.read_trace import _READ_ALL, _traced_call
from pyrung.core.state import snapshot_dict


def _plc() -> PLC:
    a = Bool("A")
    b = Bool("B")
    with Program() as prog:
        with Rung(a):
            out(b)
    return PLC(prog)


def test_snapshot_of_traced_tags_records_whole_map_read():
    plc = _plc()
    hit, reads = _traced_call(lambda s: bool(snapshot_dict(s.tags)), plc)
    assert _READ_ALL in reads


def test_snapshot_of_pmap_is_a_plain_copy():
    tags = _plc().state.tags
    copy = snapshot_dict(tags)
    assert type(copy) is dict
    assert copy == dict(tags)


def test_snapshot_of_plain_mappings_is_a_new_dict_in_order():
    src = {"z": 1, "a": 2, "m": 3}
    for mapping in (src, MappingProxyType(src)):
        copy = snapshot_dict(mapping)
        assert type(copy) is dict
        assert copy is not src
        assert list(copy.items()) == list(src.items())
