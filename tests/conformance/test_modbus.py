"""Every conformance group over Modbus TCP, plus the wire harness's own checks.

Runs against pyrung's soft PLC on localhost unless ``--conformance-plc`` (or
``PYRUNG_CONFORMANCE_PLC``) names a CLICK running the exported program.
``--conformance-group`` (or ``PYRUNG_CONFORMANCE_GROUP``) picks groups.
"""

from __future__ import annotations

import pytest

from pyrung.conformance import (
    BuildMismatchError,
    ConformanceError,
    ConformanceProgram,
    Endpoint,
    Group,
    Manifest,
    SimServer,
    assert_all_passed,
    build_program,
    case,
    run_modbus,
)
from pyrung.core import Rung, copy
from tests.conformance import suite

pytestmark = pytest.mark.conformance


def test_all_groups_over_modbus(
    conformance_endpoint: Endpoint | None, conformance_groups: list[str] | None
) -> None:
    build = suite.build()
    results = run_modbus(build, conformance_endpoint, groups=conformance_groups)
    if conformance_groups is None:
        assert len(results) == sum(len(g.cases) for g in suite.ALL_GROUPS)
    assert_all_passed(results)


# --- harness self-checks (always on the local sim) ---------------------------


def _prove_constant(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(7, slot.Result1)
        copy(1, slot.Fired)


def _prove_other_constant(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(8, slot.Result1)
        copy(1, slot.Fired)


def _prove_nothing(slot):
    with Rung(slot.Cmd != 0):
        copy(5, slot.Result1)


_TWO_GROUPS = Manifest.from_dict(
    {
        "groups": {
            "alpha": {"base": 101, "slots": 4, "cases": {"one": 1, "two": 2}},
            "beta": {"base": 201, "slots": 4, "cases": {"one": 1}},
        }
    }
)


def _alpha(ladder=_prove_constant, result=7) -> Group:
    return Group(
        "alpha",
        [
            case("Alpha one", ladder=ladder, expect={"Result1": result}, id="one"),
            case("Alpha two", ladder=_prove_constant, expect={"Result1": 7}, id="two"),
        ],
    )


def _beta() -> Group:
    return Group(
        "beta", [case("Beta one", ladder=_prove_constant, expect={"Result1": 7}, id="one")]
    )


def _slot_value(sim: SimServer, build: ConformanceProgram, name: str, field: str) -> int:
    tag = getattr(build.group(name).slot[1], field)
    return sim.plc.current_state.tags.get(tag.name, 0)


def test_mismatched_layout_bails_before_writing() -> None:
    loaded = suite.build()
    other = Group(
        "other", [case("A different program", ladder=_prove_constant, expect={"Result1": 7})]
    )
    with SimServer(loaded) as sim:
        with pytest.raises(BuildMismatchError, match="Export the whole project again"):
            run_modbus(other, sim.endpoint, timeout=2.0)
        # Nothing was written: the loaded program's slots never fired.
        assert sim.endpoint is not None
        results = run_modbus(loaded, sim.endpoint)
    assert_all_passed(results)


def test_one_group_runs_alone() -> None:
    build = build_program([_alpha(), _beta()], _TWO_GROUPS)
    with SimServer(build) as sim:
        results = run_modbus(build, sim.endpoint, groups=["beta"])
        assert [(r.group, r.case.id) for r in results] == [("beta", "one")]
        assert_all_passed(results)
        # alpha's registers were never written: its Cmd stayed 0 and nothing fired.
        assert _slot_value(sim, build, "alpha", "Cmd") == 0
        assert _slot_value(sim, build, "alpha", "Fired") == 0
        assert _slot_value(sim, build, "beta", "Fired") == 1


def test_stale_group_is_reported_and_the_rest_run() -> None:
    loaded = build_program([_alpha(), _beta()], _TWO_GROUPS)
    edited = build_program([_alpha(_prove_other_constant, 8), _beta()], _TWO_GROUPS)
    assert edited.program_hash == loaded.program_hash
    assert edited.group("alpha").group_hash != loaded.group("alpha").group_hash
    assert edited.group("beta").group_hash == loaded.group("beta").group_hash

    with SimServer(loaded) as sim:
        results = run_modbus(edited, sim.endpoint, timeout=2.0)
        # The stale group's registers were not touched.
        assert _slot_value(sim, loaded, "alpha", "Cmd") == 0
        assert _slot_value(sim, loaded, "alpha", "Fired") == 0
        assert _slot_value(sim, loaded, "beta", "Fired") == 1

    alpha = [r for r in results if r.group == "alpha"]
    beta = [r for r in results if r.group == "beta"]
    assert [r.stale for r in alpha] == [True, True]
    assert alpha[0].note.startswith("Conf_Alpha is stale: repush")
    assert [r.passed for r in beta] == [True]
    with pytest.raises(AssertionError, match="STALE: Conf_Alpha is stale: repush") as excinfo:
        assert_all_passed(results)
    assert "Beta" not in str(excinfo.value)

    # Selecting only the fresh group passes.
    with SimServer(loaded) as sim:
        assert_all_passed(run_modbus(edited, sim.endpoint, groups=["beta"]))


def test_unknown_group_name_raises() -> None:
    build = build_program([_alpha(), _beta()], _TWO_GROUPS)
    with pytest.raises(ValueError, match="Unknown conformance group.*gamma.*known: alpha, beta"):
        run_modbus(build, groups=["gamma"])


def test_case_that_never_fires_fails() -> None:
    silent = case("A case that forgets Fired", ladder=_prove_nothing, expect={"Result1": 5})
    results = run_modbus(Group("silent", [silent]), timeout=0.5)
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
        id="local",
    )
    results = run_modbus(Group("mixed", [wire, local]))
    assert [r.skipped for r in results] == [None, "in-process only: reads simulator internals"]
    assert results[0].passed
    assert_all_passed(results)


def test_unreachable_endpoint_raises() -> None:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    group = Group("x", [case("x", ladder=_prove_constant, expect={"Result1": 7})])
    with pytest.raises(ConformanceError, match="Cannot connect"):
        run_modbus(group, f"127.0.0.1:{port}")
