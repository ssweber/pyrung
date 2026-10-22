"""Every conformance case over Modbus TCP, plus the wire harness's own checks.

Runs against pyrung's soft PLC on localhost unless ``--conformance-plc`` (or
``PYRUNG_CONFORMANCE_PLC``) names a CLICK running the exported program.
"""

from __future__ import annotations

import pytest

from pyrung.conformance import (
    BuildMismatchError,
    ConformanceError,
    Endpoint,
    SimServer,
    assert_all_passed,
    build_program,
    case,
    run_modbus,
)
from pyrung.core import Rung, copy
from tests.conformance import test_rung_semantics

pytestmark = pytest.mark.conformance

#: Every case module's ``cases``.  A new case module adds itself here.
ALL_CASES = [
    *test_rung_semantics.cases,
]


def test_all_cases_over_modbus(conformance_endpoint: Endpoint | None) -> None:
    results = run_modbus(ALL_CASES, conformance_endpoint)
    assert len(results) == len(ALL_CASES)
    assert_all_passed(results)


# --- harness self-checks (always on the local sim) ---------------------------


def _prove_constant(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(7, slot.Result1)
        copy(1, slot.Fired)


def _prove_nothing(slot):
    with Rung(slot.Cmd != 0):
        copy(5, slot.Result1)


def test_mismatched_build_bails_before_writing() -> None:
    loaded = build_program(ALL_CASES)
    other = [case("A different program", ladder=_prove_constant, expect={"Result1": 7})]
    with SimServer(loaded) as sim:
        with pytest.raises(BuildMismatchError, match="Export the program again"):
            run_modbus(other, sim.endpoint, timeout=2.0)
        # Nothing was written: the loaded program's slots never fired.
        assert sim.endpoint is not None
        results = run_modbus(loaded, sim.endpoint)
    assert_all_passed(results)


def test_case_that_never_fires_fails() -> None:
    silent = case("A case that forgets Fired", ladder=_prove_nothing, expect={"Result1": 5})
    results = run_modbus([silent], timeout=0.5)
    (result,) = results
    assert not result.passed
    assert not result.fired
    assert result.actual == {"Result1": 5}
    with pytest.raises(AssertionError, match="did not fire"):
        assert_all_passed(results)


def test_in_process_only_case_is_reported_skipped() -> None:
    wire = case("Runs on the wire", ladder=_prove_constant, expect={"Result1": 7})
    local = case(
        "Needs in-process state",
        ladder=_prove_constant,
        expect={"Result1": 7},
        in_process_only="reads simulator internals",
    )
    results = run_modbus([wire, local])
    assert [r.skipped for r in results] == [None, "in-process only: reads simulator internals"]
    assert results[0].passed
    assert_all_passed(results)


def test_unreachable_endpoint_raises() -> None:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    with pytest.raises(ConformanceError, match="Cannot connect"):
        run_modbus([case("x", ladder=_prove_constant, expect={"Result1": 7})], f"127.0.0.1:{port}")
