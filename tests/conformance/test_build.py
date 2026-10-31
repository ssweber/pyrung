"""The canonical conformance program, the manifest, and case definitions (no network)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from pyrung.click import ClickBlocks
from pyrung.click.ladder import estimate_steps, pyrung_to_ladder
from pyrung.conformance import (
    Case,
    CaseResult,
    Confirmation,
    Group,
    Manifest,
    ManifestError,
    assert_all_passed,
    build_program,
    case,
    format_report,
    is_confirmed,
    load_confirmations,
    load_manifest,
    parse_endpoint,
    record_confirmations,
    run,
)
from pyrung.conformance._ladder import case_comment
from pyrung.conformance._slot import DS_LAST, slot_register
from pyrung.core import PLC, Rung, calc, copy
from pyrung.core.system_points import system
from tests.conformance import memory, suite

ds = ClickBlocks().ds


def _prove_constant(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(7, slot.Result1)
        copy(1, slot.Fired)


def _prove_other_constant(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(8, slot.Result1)
        copy(1, slot.Fired)


def _seven(id: str = "seven", sentence: str = "c") -> Case:
    return case(sentence, ladder=_prove_constant, expect={"Result1": 7}, id=id)


def _layout(**groups: dict) -> Manifest:
    return Manifest.from_dict({"groups": groups}, source="test.toml")


# --- layout ------------------------------------------------------------------


def test_checked_in_manifest_covers_the_suite() -> None:
    manifest = load_manifest(suite.MANIFEST_PATH)
    for group in suite.groups():
        manifest.layout_for(group)


def test_cases_stay_in_their_groups_raw_memory() -> None:
    problems = memory.outside_owned(suite.groups())
    assert not problems, "\n".join(problems)


def test_owned_memory_is_disjoint_and_inside_clicks_banks() -> None:
    claims: dict[tuple[str, int], str] = {}
    for group, banks in memory.OWNED.items():
        assert len(group) <= 12, group
        for bank, entry in banks.items():
            assert bank in memory.BANKS, (group, bank)
            for owned in memory.ranges(entry):
                assert 1 <= owned.start and owned.stop - 1 <= memory.BANKS[bank], (group, bank)
                for n in owned:
                    other = claims.setdefault((bank, n), group)
                    assert other == group, f"{group} and {other} both own {bank}{n}"


def test_owned_ds_is_staging_and_no_group_block_reaches_it() -> None:
    staging = memory.STAGING_DS
    for group, banks in memory.OWNED.items():
        for owned in memory.ranges(banks.get("DS", ())):
            assert staging.start <= owned.start and owned.stop <= staging.stop, group
    for layout in load_manifest(suite.MANIFEST_PATH).groups.values():
        assert layout.last_register < staging.start, layout


def test_parts_split_the_suite_into_projects_with_their_own_layout() -> None:
    every = [g.name for g in suite.groups()]
    in_parts = [name for names in suite.PARTS.values() for name in names]
    assert sorted(in_parts) == sorted(every)

    whole = suite.build()
    hashes = {whole.program_hash}
    for part in suite.PARTS:
        build = suite.build(part)
        assert set(build.names) == set(suite.PARTS[part])
        # One subroutine, one block, one hash, whichever project holds it.
        for g in build.groups:
            assert (g.base, g.group_hash) == (
                whole.group(g.name).base,
                whole.group(g.name).group_hash,
            )
        hashes.add(build.program_hash)
    assert len(hashes) == 1 + len(suite.PARTS)
    # The system group still runs first in its part (see test_system.py).
    assert suite.groups("B")[0].name == "system"


@pytest.mark.parametrize("part", sorted(suite.PARTS))
def test_each_part_fits_a_click_project(part: str) -> None:
    build = suite.build(part)
    estimate = estimate_steps(pyrung_to_ladder(build.program, build.tag_map, validate=False))
    assert estimate.total <= suite.PART_STEP_BUDGET, (
        f"part {part} estimates {estimate.total} steps, over {suite.PART_STEP_BUDGET}: "
        "move a group to another part"
    )


def test_groups_live_at_fixed_ds_registers() -> None:
    build = suite.build()
    m = build.tag_map
    rungs = build.group("rungs")
    assert rungs.subroutine == "Conf_Rungs"
    assert m.resolve("Conf_ProgramHash") == "DS1"
    assert m.resolve(rungs.hash_tag.name) == "DS101"
    assert m.resolve(rungs.slot[1].Cmd.name) == "DS111"
    assert m.resolve(rungs.slot[1].Fired.name) == "DS117"
    assert m.resolve(rungs.slot[3].ErrorCode.name) == "DS138"
    assert rungs.register(3, "ErrorCode") == 138
    assert rungs.span == (111, 10 * max(i for i, _ in rungs.cases))
    assert rungs.layout.last_register == 300


def test_slots_come_from_the_manifest_not_case_order() -> None:
    manifest = _layout(g={"base": 101, "slots": 9, "cases": {"a": 5, "b": 2}})
    build = build_program(Group("g", [_seven("a"), _seven("b")]), manifest)
    g = build.group("g")
    assert [i for i, _ in g.cases] == [5, 2]
    assert g.register(5) == 151
    assert g.span == (121, 40)


def test_adding_a_case_moves_nothing_else() -> None:
    manifest = _layout(
        a={"base": 101, "slots": 9, "cases": {"one": 1, "two": 2}},
        b={"base": 201, "slots": 9, "cases": {"one": 1}},
    )
    before = build_program([Group("a", [_seven("one")]), Group("b", [_seven("one")])], manifest)
    after = build_program(
        [Group("a", [_seven("one"), _seven("two")]), Group("b", [_seven("one")])], manifest
    )
    assert after.program_hash == before.program_hash
    assert after.group("b").group_hash == before.group("b").group_hash
    assert after.group("a").group_hash != before.group("a").group_hash
    assert after.group("a").register(1) == before.group("a").register(1)


def test_group_hash_is_stable_and_tracks_the_ladder() -> None:
    def build(ladder):
        group = Group("g", [case("c", ladder=ladder, expect={"Result1": 7}, id="c")])
        return build_program(group).group("g").group_hash

    one = build(_prove_constant)
    assert 1 <= one <= 32767
    assert one == build(_prove_constant)
    assert one != build(_prove_other_constant)


def test_program_hash_tracks_the_group_table() -> None:
    a = build_program(Group("a", [_seven()]))
    b = build_program(Group("b", [_seven()]))
    ab = build_program([Group("a", [_seven()]), Group("b", [_seven()])])
    assert len({a.program_hash, b.program_hash, ab.program_hash}) == 3


def test_every_scan_writes_the_headers() -> None:
    build = suite.build()
    rungs = build.group("rungs")
    plc = PLC(build.program)
    plc.step()
    tags = plc.current_state.tags
    assert tags["Conf_ProgramHash"] == build.program_hash
    assert tags[rungs.hash_tag.name] == rungs.group_hash


def test_in_process_only_cases_are_left_out_of_the_program() -> None:
    wire = _seven("wire")
    local = case("local", ladder=_prove_constant, expect={"Result1": 7}, in_process_only="why")
    # The local case needs no manifest entry.
    build = build_program(
        [Group("g", [local, wire]), Group("only_local", [local])],
        _layout(g={"base": 101, "slots": 2, "cases": {"wire": 1}}),
    )
    assert [c for _, c in build.group("g").cases] == [wire]
    assert [g.name for g in build.groups] == ["g"]
    assert build.names == ("g", "only_local")
    assert build.skipped == (("g", local), ("only_local", local))
    with pytest.raises(ValueError, match="No cases can run over Modbus"):
        build_program(Group("g", [local]))


def test_export_writes_main_one_subroutine_per_group_and_nicknames(tmp_path) -> None:
    build = suite.build()
    rungs = build.group("rungs")
    # The whole suite is more than one CLICK project holds: it exports, with a warning.
    with pytest.warns(UserWarning, match="CLK_STEP_LIMIT"):
        out = build.export(tmp_path / "conformance")
    main = (out / "main.csv").read_text(encoding="utf-8")
    assert f"copy({build.program_hash},DS1)" in main
    assert 'call(""Conf_Rungs"")' in main
    sub = (out / "subroutines" / "Conf_Rungs.csv").read_text(encoding="utf-8")
    assert f"copy({rungs.group_hash},DS101)" in sub
    assert "DS111!=0" in sub
    assert (out / "nicknames.csv").exists()


def test_export_leaves_no_subroutine_from_an_earlier_export(tmp_path) -> None:
    out = tmp_path / "part"
    build_program([Group("alpha", [_seven()])]).export(out)
    build_program([Group("beta", [_seven()])]).export(out)
    assert sorted(p.name for p in (out / "subroutines").iterdir()) == ["Conf_Beta.csv"]


# --- manifest errors -----------------------------------------------------------


def test_missing_group_says_what_to_add() -> None:
    manifest = _layout(a={"base": 101, "slots": 19, "cases": {"seven": 1}})
    group = Group("math", [_seven("int_wraps"), _seven("div_by_zero")])
    with pytest.raises(ManifestError) as excinfo:
        build_program([group], manifest)
    text = str(excinfo.value)
    assert "Group 'math' has no block in test.toml" in text
    assert "[groups.math]\nbase = 301\nslots = 19" in text
    assert "[groups.math.cases]\nint_wraps = 1\ndiv_by_zero = 2" in text


def test_missing_case_says_what_to_add() -> None:
    manifest = _layout(g={"base": 101, "slots": 3, "cases": {"a": 1, "gone": 3}})
    group = Group("g", [_seven("a"), _seven("b"), _seven("c")])
    with pytest.raises(ManifestError) as excinfo:
        build_program(group, manifest)
    text = str(excinfo.value)
    assert "Add under [groups.g.cases]:\n\nb = 4\nc = 5" in text
    assert "holds 3 slots: raise slots" in text


@pytest.mark.parametrize(
    ("groups", "match"),
    [
        ({"g": {"base": 5, "slots": 1}}, "base must be an integer > 10"),
        ({"g": {"base": 101, "slots": 0}}, "slots must be an integer >= 1"),
        ({"g": {"base": 101, "slots": 2, "cases": {"a": 3}}}, r"slot must be 1\.\.2"),
        ({"g": {"base": 101, "slots": 2, "cases": {"a": 1, "b": 1}}}, "share slot 1"),
        ({"g": {"base": 4401, "slots": 19}}, f"past DS{DS_LAST}"),
        (
            {"a": {"base": 101, "slots": 19}, "b": {"base": 251, "slots": 1}},
            r"\[groups\.a\] \(DS101\.\.DS300\) overlaps \[groups\.b\]",
        ),
    ],
)
def test_bad_manifest_is_rejected(groups: dict, match: str) -> None:
    with pytest.raises(ManifestError, match=match):
        _layout(**groups)


def test_long_group_name_is_rejected_before_click_sees_it() -> None:
    with pytest.raises(ValueError, match="CLICK allows 24"):
        build_program(Group("rung_semantics_order", [_seven()]))


# --- cases and groups ------------------------------------------------------------


def test_case_id_defaults_to_the_ladder_name() -> None:
    def prove_widgets(slot):
        _prove_constant(slot)

    assert case("x", ladder=prove_widgets, expect={}).id == "widgets"
    assert case("x", ladder=_prove_constant, expect={}).id == "constant"
    with pytest.raises(ValueError, match="pass id="):
        case("x", ladder=lambda slot: None, expect={})


def test_group_rejects_bad_names_and_repeated_ids() -> None:
    with pytest.raises(ValueError, match="snake_case"):
        Group("Math", [])
    with pytest.raises(ValueError, match="two cases with id 'seven'"):
        Group("g", [_seven(), _seven()])
    assert Group("rung_order", []).subroutine == "Conf_RungOrder"


def test_run_a_group_in_process_inside_its_subroutine() -> None:
    group = Group("g", [_seven()])
    results = run(group)
    assert_all_passed(results)
    assert results[0].group == "g"


def test_report_marks_unconfirmed_cases() -> None:
    confirmed = case("on hardware", ladder=_prove_constant, expect={"Result1": 7})
    results = run(Group("g", [confirmed, _seven(sentence="not yet")]))
    report = format_report(results, {"g/constant": Confirmation("abc", "2026-10-30")})
    assert "PASS  g/constant [confirmed] on hardware" in report
    assert "PASS  g/seven [unconfirmed] not yet" in report
    assert report.endswith("1 unconfirmed on hardware")
    assert report.isascii()


def test_case_hash_follows_only_its_own_rows() -> None:
    def hashes(*cases: Case) -> dict[str, str]:
        return build_program(Group("g", list(cases))).groups[0].case_hashes

    before = hashes(_seven(), case("x", ladder=_prove_constant, expect={"Result1": 7}))
    assert set(before) == {"seven", "constant"}
    # Editing one case's expectation changes its hash and no other's.
    after = hashes(_seven(), case("x", ladder=_prove_constant, expect={"Result1": 8}))
    assert after["seven"] == before["seven"]
    assert after["constant"] != before["constant"]


def test_a_click_run_records_passes_and_drops_failures(tmp_path) -> None:
    path = tmp_path / "confirmed.toml"
    one, two = _seven(), case("x", ladder=_prove_constant, expect={"Result1": 7})
    passed = CaseResult(case=one, passed=True, actual={}, slot_index=1, group="g", case_hash="h1")
    failed = CaseResult(case=two, passed=False, actual={}, slot_index=2, group="g", case_hash="h2")
    added, dropped = record_confirmations(
        path, [passed, replace(failed, passed=True)], on="2026-10-30"
    )
    assert (sorted(added), dropped) == (["g/constant", "g/seven"], [])
    added, dropped = record_confirmations(path, [passed, failed], on="2026-10-31")
    assert (added, dropped) == ([], ["g/constant"])
    record = load_confirmations(path)
    assert record == {"g/seven": Confirmation("h1", "2026-10-31")}
    assert is_confirmed(passed, record)
    # Rewritten since (another hash): unconfirmed until it passes again.
    assert not is_confirmed(replace(passed, case_hash="h9"), record)


def test_pending_case_is_a_strict_xfail_on_pyrung() -> None:
    # pyrung reads 7.  A pending case expecting 8 (CLICK's answer) passes as XFAIL
    # in-process; once pyrung reads what it expects, it fails until the marker goes.
    missing = case("CLICK reads 8", ladder=_prove_constant, expect={"Result1": 8}, pending="S2")
    (result,) = run(Group("g", [missing]))
    assert result.passed and result.xfail
    assert "pending S2" in result.note
    assert_all_passed([result])
    report = format_report([result])
    assert "XFAIL g/constant [unconfirmed; pending S2] CLICK reads 8" in report
    assert "1 pending (pyrung differs from CLICK)" in report
    assert report.isascii()

    met = case("pyrung caught up", ladder=_prove_constant, expect={"Result1": 7}, pending="S2")
    (result,) = run(Group("g", [met]))
    assert not result.passed and not result.xfail
    with pytest.raises(AssertionError, match="remove pending="):
        assert_all_passed([result])


def test_pending_case_still_fails_a_leak() -> None:
    leaky = case("Counts its runs", ladder=_prove_counts_runs, expect={"Result1": 5}, pending="S2")
    (result,) = run(Group("g", [leaky]))
    assert not result.passed
    assert "state left after the reset" in result.note


def test_pending_and_probe_are_checked_and_reported() -> None:
    with pytest.raises(ValueError, match="pending='S9' must name a fix step"):
        case("x", ladder=_prove_constant, expect={"Result1": 7}, pending="S9")
    probe = case("x", ladder=_prove_constant, expect={"Result1": 7}, probe="what CLICK does")
    report = format_report(run(Group("g", [probe])))
    assert "PASS  g/constant [unconfirmed; probe: what CLICK does] x" in report


def _prove_calls(slot):
    # Result1 counts the calls while Cmd is up; in-process the case runs 10 scans.
    with Rung(slot.Cmd != 0):
        calc(slot.Result1 + 1, slot.Result1)
        copy(1, slot.Fired)


def test_a_group_can_be_called_twice_a_scan() -> None:
    def calls(n: int) -> Group:
        counted = case("calls", ladder=_prove_calls, expect={"Result1": 10 * n}, scans=10)
        return Group("g", [counted], calls=n)

    assert_all_passed(run(calls(1)))
    assert_all_passed(run(calls(2)))
    assert build_program(calls(2)).program_hash != build_program(calls(1)).program_hash
    with pytest.raises(ValueError, match="calls must be >= 1"):
        Group("g", [], calls=0)


def test_range_expectation_in_process() -> None:
    results = run([case("bracket", ladder=_prove_constant, expect={"Result1": range(5, 10)})])
    assert_all_passed(results)
    results = run([case("bracket", ladder=_prove_constant, expect={"Result1": range(8, 10)})])
    with pytest.raises(AssertionError, match=r"'Result1': 8\.\.9"):
        assert_all_passed(results)


def test_case_rejects_bad_expectations() -> None:
    with pytest.raises(ValueError, match="Unknown slot fields"):
        case("x", ladder=_prove_constant, expect={"Cmd": 1})
    with pytest.raises(TypeError, match="int or a range"):
        case("x", ladder=_prove_constant, expect={"Result1": 1.5})


def test_parse_endpoint() -> None:
    assert str(parse_endpoint("192.168.0.10")) == "192.168.0.10:502"
    assert str(parse_endpoint(" localhost:5020 ")) == "localhost:5020"
    with pytest.raises(ValueError):
        parse_endpoint("host:notaport")
    with pytest.raises(ValueError):
        parse_endpoint("")


def test_slot_register_layout() -> None:
    assert slot_register(101, 1, "Cmd") == 111
    assert slot_register(101, 19, "ErrorCode") == 298
    assert slot_register(101, 19, "Ready") == 299


# --- comments and the reset ------------------------------------------------------


def test_each_case_starts_with_its_comment() -> None:
    c = case(
        "Seven, or a draw",
        ladder=_prove_constant,
        expect={"Result1": 7, "Result2": range(1, 7)},
        id="seven",
    )
    text = case_comment("g", c)
    assert text.splitlines() == [
        "Case g/seven",
        "Seven, or a draw",
        "Expect: Result1 = 7, Result2 in 1..6",
    ]
    build = build_program(Group("g", [c]))
    rungs = build.program.subroutines["Conf_G"]
    # Rung 1 writes the group hash; the case's reset rung follows, carrying the comment.
    assert [r.comment for r in rungs[:2]] == [None, text]


def test_the_reset_rung_zeroes_the_slot_with_one_fill(tmp_path) -> None:
    out = build_program(Group("g", [_seven()])).export(tmp_path)
    sub = (out / "subroutines" / "Conf_G.csv").read_text(encoding="utf-8")
    assert "#,Case g/seven" in sub
    assert "fill(0,DS21..DS29)" in sub.replace(" ", "")


def _prove_counts_runs(slot):
    # Leaks unless DS4000 is reset: each run adds one to it.
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        calc(ds[4000] + 1, ds[4000])
        copy(ds[4000], slot.Result1)
        copy(1, slot.Fired)


def _init_counts_runs(slot):
    copy(0, ds[4000])


def test_state_left_after_the_reset_fails_the_case() -> None:
    leaky = case("Counts its runs", ladder=_prove_counts_runs, expect={"Result1": 1})
    (result,) = run([leaky])
    assert not result.passed
    assert result.actual == {"Result1": 2}
    assert "run 1 of 2: actual {'Result1': 1}" in result.note
    assert "state left after the reset: DS4000 = 1 (fresh 0)" in result.note

    reset = case(
        "Counts its runs",
        ladder=_prove_counts_runs,
        init=_init_counts_runs,
        expect={"Result1": 1},
    )
    assert_all_passed(run([reset]))
    # keep= exempts the tag from the leak check, not from the repeat.
    kept = case(
        "Counts its runs", ladder=_prove_counts_runs, expect={"Result1": 1}, keep=[ds[4000]]
    )
    (result,) = run([kept])
    assert not result.passed
    assert "state left" not in result.note


def test_edge_history_of_a_system_clock_is_not_a_leak() -> None:
    # rise() on a clock leaves _prev:sys.clock_* memory that moves with time,
    # not with any case; a user tag's edge history still counts.
    from pyrung.conformance._runner import _diff
    from pyrung.core.state import SystemState

    clock = f"_prev:{system.sys.clock_100ms.name}"
    fresh = SystemState().set(memory=SystemState().memory.set(clock, False))
    now = fresh.set(memory=fresh.memory.set(clock, True).set("_prev:C1", True))
    assert _diff(fresh, now, kept=()) == {"_prev:C1": (True, None)}


def test_a_reset_that_never_completes_fails_only_its_case() -> None:
    stuck = case(
        "Never ready",
        ladder=_prove_constant,
        expect={"Result1": 7},
        ready=lambda slot: slot.Cmd != 0,
        id="stuck",
    )
    results = run(Group("g", [stuck, _seven()]))
    assert [r.passed for r in results] == [False, True]
    assert not results[0].fired
    assert "reset did not complete" in results[0].note


def test_cases_that_share_memory_are_rejected() -> None:
    a = case("A", ladder=_prove_counts_runs, expect={"Result1": 1}, id="a")
    b = case("B", ladder=_prove_counts_runs, expect={"Result1": 1}, id="b")
    with pytest.raises(ValueError, match="g/a and g/b share DS4000"):
        run(Group("g", [a, b]))
    with pytest.raises(ValueError, match="g/a and h/b share DS4000"):
        build_program([Group("g", [a]), Group("h", [b])])


def _prove_calls_a_helper(slot):
    from pyrung.core import call, forloop, subroutine

    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with forloop(3):
            call("Conf_H_Inc")
    with Rung(slot.Cmd != 0):
        copy(1, slot.Fired)
    with subroutine("Conf_H_Inc"):
        with Rung():
            calc(slot.Result1 + 1, slot.Result1)


def test_an_in_main_group_runs_its_rungs_in_main_with_helper_subroutines(tmp_path) -> None:
    c = case("c", ladder=_prove_calls_a_helper, expect={"Result1": 3}, id="seven")
    plain = build_program(Group("g", [_seven()]))
    group = Group("h", [c], in_main=True)
    build = build_program(group)

    assert "Conf_H" not in build.program.subroutines
    assert "Conf_H_Inc" in build.program.subroutines
    out = build.export(tmp_path)
    main = (out / "main.csv").read_text(encoding="utf-8")
    assert "#,Case h/seven" in main
    assert "call(" in main and "Conf_H_Inc" in main
    assert sorted(p.name for p in (out / "subroutines").iterdir()) == ["Conf_H_Inc.csv"]
    assert_all_passed(run(group))
    assert plain.program_hash != build.program_hash
    assert build.groups[0].case_hashes["seven"]

    with pytest.raises(ValueError, match="in_main"):
        Group("h", [c], in_main=True, calls=2)


def _in_main_case(id: str, helper_step: int):
    from pyrung.core import call, forloop, subroutine

    def ladder(slot):
        with Rung(slot.Cmd != 0, slot.Fired == 0):
            with forloop(2):
                call(f"Conf_H_{id}")
        with Rung(slot.Cmd != 0):
            copy(1, slot.Fired)
        with subroutine(f"Conf_H_{id}"):
            with Rung():
                calc(slot.Result1 + helper_step, slot.Result1)

    return case("c", ladder=ladder, expect={"Result1": 2 * helper_step}, id=id)


def test_an_in_main_helper_changes_only_the_hash_of_the_case_that_defines_it() -> None:
    def hashes(step_one: int, step_two: int):
        group = Group(
            "h", [_in_main_case("one", step_one), _in_main_case("two", step_two)], in_main=True
        )
        return build_program(group).groups[0].case_hashes

    base = hashes(1, 1)
    edited_one = hashes(2, 1)
    assert edited_one["one"] != base["one"]
    assert edited_one["two"] == base["two"]
    edited_two = hashes(1, 2)
    assert edited_two["two"] != base["two"]
    assert edited_two["one"] == base["one"]
