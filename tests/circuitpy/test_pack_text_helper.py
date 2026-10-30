"""The generated pack_text parse helper, run as written."""

from __future__ import annotations

import math
import re
import struct

import pytest

from pyrung.circuitpy.codegen._helpers import _HELPER_DEFS


def _helper():
    ns = {"re": re, "math": math, "struct": struct}
    exec("\n".join(_HELPER_DEFS["_parse_pack_text_value"]), ns)
    return ns["_parse_pack_text_value"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("12.9", 12), ("-42", -42), ("+7", 7), ("123456", 123456), ("12.", 12)],
)
def test_int_parse_stops_at_a_point(text, expected):
    assert _helper()(text, "INT") == expected
    assert _helper()(text, "DINT") == expected


@pytest.mark.parametrize("text", ["12x", "x12", "12.9x", "1.2.3", ""])
def test_int_parse_refuses_other_non_digits(text):
    with pytest.raises(ValueError):
        _helper()(text, "INT")
