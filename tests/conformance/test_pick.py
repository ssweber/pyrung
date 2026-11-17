"""Picked cases as a project with every rung in main (``suite.build_main``), no network."""

from __future__ import annotations

import pytest

from pyrung.conformance import CaseResult, record_confirmations
from tests.conformance import suite


def _owners() -> dict[str, tuple[str, ...]]:
    return {**suite.PARTS, **{p: a.groups for p, a in suite.APART_PARTS.items()}}


def _pickable(part: str) -> list[list[str]]:
    """The picks that hold every case of *part* that can run in main."""
    known = {g.name: g for g in (*suite.ALL_GROUPS, *suite.APART_GROUPS)}
    held = [name for name in _owners()[part] if suite.main_refusal(known[name]) is None]
    alone = [[name] for name in held if name in suite.MAIN_ALONE]
    rest = [name for name in held if name not in suite.MAIN_ALONE]
    return [*alone, *([rest] if rest else [])]


@pytest.mark.parametrize("part", sorted(_owners()))
def test_a_picked_case_keeps_its_slot_and_the_hash_its_part_confirms(part: str) -> None:
    whole = suite.build(part)
    for entries in _pickable(part):
        picked = suite.build_main(entries)
        assert picked.program_hash != whole.program_hash
        assert {g.name for g in picked.groups} == set(entries)
        for g in picked.groups:
            in_part = whole.group(g.name)
            assert g.case_hashes == in_part.case_hashes
            assert [(i, c.id) for i, c in g.cases] == [(i, c.id) for i, c in in_part.cases]
            assert g.hash_register == in_part.hash_register


def test_a_pick_of_some_cases_runs_only_them_in_main() -> None:
    picked = suite.build_main(
        ["word_store/word_literals", "drum/events_are_edges", "word_store/word_below_zero"]
    )
    # ALL_GROUPS order and group order, whatever the order of the entries.
    assert [(g.name, [c.id for _, c in g.cases]) for g in picked.groups] == [
        ("drum", ["events_are_edges"]),
        ("word_store", ["word_below_zero", "word_literals"]),
    ]
    assert not picked.program.subroutines
    whole = suite.build("copy").group("word_store")
    for g in picked.groups:
        part = suite.build("drum1" if g.name == "drum" else "copy").group(g.name)
        for i, c in g.cases:
            assert g.register(i) == part.register(dict((c2.id, j) for j, c2 in part.cases)[c.id])
            assert g.case_hashes[c.id] == part.case_hashes[c.id]
    # Another pick of the same group is another layout, and another group hash.
    other = suite.build_main(["word_store/word_literals"])
    assert other.program_hash != picked.program_hash
    assert other.group("word_store").group_hash != picked.group("word_store").group_hash
    assert picked.group("word_store").group_hash != whole.group_hash


def test_a_pass_from_a_pick_confirms_the_hash_a_part_run_confirms(tmp_path) -> None:
    picked = suite.build_main(["word_store/word_literals"])
    part = suite.build("copy").group("word_store")
    g = picked.group("word_store")
    ((index, c),) = g.cases
    by_pick = tmp_path / "pick.toml"
    by_part = tmp_path / "part.toml"
    for path, hashes in ((by_pick, g.case_hashes), (by_part, part.case_hashes)):
        result = CaseResult(
            case=c,
            passed=True,
            actual={},
            slot_index=index,
            fired=True,
            group="word_store",
            case_hash=hashes[c.id],
        )
        record_confirmations(path, [result], on="2026-11-17")
    assert by_pick.read_text(encoding="utf-8") == by_part.read_text(encoding="utf-8")


def test_an_in_main_group_with_helper_subroutines_can_be_picked() -> None:
    picked = suite.build_main(["loop_call/call_inside_loop", "drum_idle"])
    part = suite.build("control").group("loop_call")
    g = picked.group("loop_call")
    assert g.case_hashes["call_inside_loop"] == part.case_hashes["call_inside_loop"]
    # The helpers stay subroutines that main calls.  No case runs in one of its own.
    assert picked.program.subroutines


@pytest.mark.parametrize(
    ("entries", "match"),
    [
        (["call_twice"], "2 times a scan"),
        (["call_more/called_twice"], "2 times a scan"),
        (["ret_branch"], "Return must end a subroutine"),
        (["boot_read"], "a person boots the PLC"),
        (["rtc_set"], "clock"),
        (["flag_scan"], "across a scan"),
        (["fixed_scan"], "fixed scan"),
        (["for_signed"], "watchdog"),
        (["modbus_raw"], "test_raw_requests"),
        (["past_write"], "past the DD bank"),
        (["past_clean"], "past the DD bank"),
        (["unit_pairs", "unit_minute", "word_store"], "are on memory of their own"),
        (["for_count"], "in-process only"),
        (["loop/negative_count_reads_unsigned"], "in-process only: "),
        (["for_wide", "coils/out_follows_rung"], "Pick for_wide alone"),
        (["flag_carry", "term_zero"], "parts follow2_carry, follow2_values are picked"),
        (["word_store", "unit_pairs"], "unit_pairs is on memory of its own"),
        (["unit_pairs", "text_compare"], "parts text_flasher, units are picked together"),
        (["word_store/nope", "nogroup"], "No conformance group or case word_store/nope, nogroup"),
        ([], "Pick at least one case"),
    ],
)
def test_a_case_that_cannot_stand_alone_in_main_is_refused(entries: list[str], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        suite.build_main(entries)


def test_every_refused_case_says_why() -> None:
    refused = suite.main_refusals()
    assert refused["ret_branch/branch_return_stops_subroutine"]
    assert refused["call_twice/rise_once_per_scan"].startswith("main calls Conf_CallTwice 2 times")
    assert "word_store/word_literals" not in refused
    assert all(reason.isascii() for reason in refused.values())


def test_a_pick_over_the_step_budget_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(suite, "PART_STEP_BUDGET", 100)
    with pytest.raises(ValueError, match=r"about \d+ CLICK steps, over the 100"):
        suite.build_main(["word_store"])


def test_a_pick_reads_entries_from_a_file(tmp_path) -> None:
    picks = tmp_path / "picks.txt"
    picks.write_text("# for the queue\nword_store/word_literals\ndrum, coils  # two\n")
    assert suite.parse_picks(f"rungs,@{picks}") == [
        "rungs",
        "word_store/word_literals",
        "drum",
        "coils",
    ]


def test_a_missing_pick_file_is_a_usage_error(tmp_path, capsys) -> None:
    missing = tmp_path / "none.txt"
    with pytest.raises(ValueError, match="Cannot read the pick file .*none.txt"):
        suite.parse_picks(f"@{missing}")
    assert suite.main(["--cases", f"@{missing}", str(tmp_path / "out")]) == 2
    assert "none.txt" in capsys.readouterr().err


def test_the_export_writes_main_and_no_case_subroutine(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "pick"
    assert suite.main(["--cases", "word_store/word_literals,drum/events_are_edges", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "cases in main" in printed and "steps" in printed
    assert "CASES=word_store/word_literals,drum/events_are_edges" in printed
    assert (out / "main.csv").is_file()
    assert not list(out.glob("subroutines/*.csv"))
    assert suite.main(["--cases", "ret_branch", str(out)]) == 2
    assert "Return must end a subroutine" in capsys.readouterr().err
