"""Every conformance group over Modbus TCP, plus the wire harness's own checks.

Runs against pyrung's soft PLC on localhost unless ``--conformance-plc`` (or
``PYRUNG_CONFORMANCE_PLC``) names a CLICK running the exported program, or
``--conformance-target=circuitpy`` (``PYRUNG_CONFORMANCE_TARGET``) runs the
program's generated P1AM-200 code on this machine instead.
``--conformance-group`` (or ``PYRUNG_CONFORMANCE_GROUP``) picks groups.  A
real PLC may run one part of the suite (``suite.PARTS``); its ``DS1`` says which.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import pytest

from pyrung.click import ClickBlocks
from pyrung.conformance import (
    BuildMismatchError,
    CircuitPySim,
    ConformanceError,
    ConformanceProgram,
    Endpoint,
    Group,
    Manifest,
    SimServer,
    assert_all_passed,
    build_program,
    case,
    format_report,
    run_modbus,
)
from pyrung.core import Rung, calc, copy
from tests.conformance import suite

ds = ClickBlocks().ds

pytestmark = pytest.mark.conformance


def test_all_groups_over_modbus(
    conformance_endpoint: Endpoint | None,
    conformance_groups: list[str] | None,
    conformance_sim: type[SimServer] | type[CircuitPySim],
) -> None:
    build = suite.build_for(conformance_endpoint)
    # Without GROUP=, every loaded group (on pyrung's own targets, but the ones
    # that stop pyrung's PLC: test_math_error.py runs those alone).
    normal = suite.wire_groups(build, conformance_endpoint)
    groups = conformance_groups or [g.name for g in normal]
    results = run_modbus(build, conformance_endpoint, groups=groups, sim=conformance_sim)
    print(format_report(results))
    if conformance_groups is None:
        assert len(results) == sum(len(g.cases) for g in normal)
    assert_all_passed(results)


@contextmanager
def _one_plc(
    build: ConformanceProgram,
    endpoint: Endpoint | None,
    local: type[SimServer] | type[CircuitPySim] = SimServer,
) -> Generator[Endpoint]:
    """*endpoint*, or one *local* target kept running for the whole block."""
    if endpoint is not None:
        yield endpoint
        return
    with local(build) as sim:
        assert sim.endpoint is not None
        yield sim.endpoint
        sim.raise_if_failed()


def _passes(results, label: str) -> None:
    try:
        assert_all_passed(results)
    except AssertionError as exc:
        raise AssertionError(f"{label}:\n{exc}") from None


def test_suite_repeats_without_restarting(
    conformance_endpoint: Endpoint | None,
    conformance_groups: list[str] | None,
    conformance_sim: type[SimServer] | type[CircuitPySim],
) -> None:
    """The suite twice on one PLC, then each group alone, A -> B -> ... -> B -> A.

    Every case resets its own state while ``Cmd == 0``, so a run never depends
    on what ran before it on the same PLC.
    """
    build = suite.build_for(conformance_endpoint)
    local = conformance_endpoint is None
    allowed = {g.name for g in suite.wire_groups(build, conformance_endpoint)}
    names = [n for n in conformance_groups or [g.name for g in build.groups] if n in allowed]
    if not names:
        pytest.skip("a group that stops pyrung's PLC runs once per target")
    with _one_plc(build, conformance_endpoint, conformance_sim) as endpoint:
        for n in (1, 2):
            results = run_modbus(build, endpoint, groups=names, pending=local)
            _passes(results, f"suite run {n} of 2")
        for step, name in enumerate(names + names[-2::-1], start=1):
            results = run_modbus(build, endpoint, groups=[name], pending=local)
            _passes(results, f"step {step}: {name} alone")


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
        with pytest.raises(BuildMismatchError, match="Export the project again"):
            run_modbus(other, sim.endpoint, timeout=2.0)
        # Nothing was written: the loaded program's slots never fired.
        assert sim.endpoint is not None
        results = run_modbus(
            loaded, sim.endpoint, groups=[g.name for g in suite.ALL_GROUPS], pending=True
        )
    assert_all_passed(results)


def test_the_loaded_part_is_read_from_ds1() -> None:
    assert suite.build_for(None).program_hash == suite.build().program_hash
    part_a = suite.build("A")
    with SimServer(part_a) as sim:
        assert sim.endpoint is not None
        assert suite.build_for(sim.endpoint).program_hash == part_a.program_hash
    other = build_program([_alpha(), _beta()], _TWO_GROUPS)
    with SimServer(other) as sim:
        assert sim.endpoint is not None
        with pytest.raises(BuildMismatchError, match="make conformance-export PART="):
            suite.build_for(sim.endpoint)


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


def test_circuitpy_target_runs_the_generated_code() -> None:
    """The P1AM-200 code for the program, on this machine, answering through its own server."""
    build = build_program([_alpha(), _beta()], _TWO_GROUPS)
    results = run_modbus(build, sim=CircuitPySim)
    assert [(r.group, r.case.id) for r in results] == [
        ("alpha", "one"),
        ("alpha", "two"),
        ("beta", "one"),
    ]
    assert_all_passed(results)


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
    assert "STALE alpha/one" in format_report(results)

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


def test_pending_case_is_an_xfail_on_a_local_target_and_judged_on_a_plc() -> None:
    # pyrung reads 7; CLICK (says the case) reads 8.
    pending = case("CLICK reads 8", ladder=_prove_constant, expect={"Result1": 8}, pending="S2")
    build = build_program(Group("p", [pending]))
    (local,) = run_modbus(build)
    assert local.passed and local.xfail
    with SimServer(build) as sim:
        # A served endpoint is judged as a CLICK unless told it is pyrung's own.
        (as_plc,) = run_modbus(build, sim.endpoint)
        (as_local,) = run_modbus(build, sim.endpoint, pending=True)
    assert not as_plc.passed and not as_plc.xfail
    assert as_local.passed and as_local.xfail


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


def _prove_counts_runs(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        calc(ds[4000] + 1, ds[4000])
        copy(ds[4000], slot.Result1)
        copy(1, slot.Fired)


def _init_counts_runs(slot):
    copy(0, ds[4000])


def test_a_leak_shows_on_the_second_run_and_init_fixes_it() -> None:
    leaky = Group("g", [case("Counts its runs", ladder=_prove_counts_runs, expect={"Result1": 1})])
    build = build_program(leaky)
    with SimServer(build) as sim:
        assert_all_passed(run_modbus(build, sim.endpoint))
        (second,) = run_modbus(build, sim.endpoint)
    assert not second.passed
    assert second.actual == {"Result1": 2}

    reset = case(
        "Counts its runs", ladder=_prove_counts_runs, init=_init_counts_runs, expect={"Result1": 1}
    )
    build = build_program(Group("g", [reset]))
    with SimServer(build) as sim:
        for _ in range(2):
            assert_all_passed(run_modbus(build, sim.endpoint))


def test_a_reset_that_never_completes_is_judged_not_fired() -> None:
    stuck = case(
        "Never ready",
        ladder=_prove_constant,
        expect={"Result1": 7},
        ready=lambda slot: slot.Cmd != 0,
        id="stuck",
    )
    build = build_program(
        Group("g", [stuck, case("Ready", ladder=_prove_constant, expect={"Result1": 7})])
    )
    with SimServer(build) as sim:
        results = run_modbus(build, sim.endpoint, timeout=0.5)
        # The stuck case's Cmd was never written.
        assert _slot_value(sim, build, "g", "Cmd") == 0
    assert [r.passed for r in results] == [False, True]
    assert not results[0].fired
    assert (
        "reset did not complete within 0.5 s: {'Cmd': 0, 'Fired': 0, 'Ready': 0}" in results[0].note
    )


def test_unreachable_endpoint_raises() -> None:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    group = Group("x", [case("x", ladder=_prove_constant, expect={"Result1": 7})])
    with pytest.raises(ConformanceError, match="Cannot connect"):
        run_modbus(group, f"127.0.0.1:{port}")
