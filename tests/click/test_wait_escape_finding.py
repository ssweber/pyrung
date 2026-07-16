"""Click validation surfaces the hang-forever survey as an advisory finding."""

from __future__ import annotations

from pyrung.click import ClickBlocks, TagMap
from pyrung.click.validation import (
    CLK_WAIT_STEP_NO_ESCAPE,
    validate_click_program,
)
from pyrung.core import Bool, Int, Program, Rung, calc, copy

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()


def _build_wait_no_escape() -> tuple[Program, TagMap]:
    """Step 1 waits on an external input; the only escapes are a config-dead
    timeout and a wrong-step error rung."""
    Step = Int("Step")
    Acc = Int("Acc")
    Limit = Int("Limit")
    EnableLimit = Int("EnableLimit")  # never written → constant 0
    Err = Int("Err")
    FB = Bool("FB")

    prog = Program(strict=False)
    with prog:
        with Rung(Step == 1, Acc > 2, FB):
            calc(Step + 1, Step)
        with Rung(Acc >= Limit, EnableLimit == 1):
            copy(1, Err)
        with Rung(Step == 3, ~FB):
            copy(1, Err)

    tag_map = TagMap(
        [
            Step.map_to(ds[10]),
            Acc.map_to(ds[11]),
            Limit.map_to(ds[12]),
            EnableLimit.map_to(ds[13]),
            Err.map_to(ds[14]),
            FB.map_to(x[1]),
        ],
        include_system=False,
    )
    return prog, tag_map


def test_wait_no_escape_is_a_warning() -> None:
    prog, tag_map = _build_wait_no_escape()
    report = validate_click_program(prog, tag_map, mode="warn")

    matches = [f for f in report.warnings if f.code == CLK_WAIT_STEP_NO_ESCAPE]
    assert len(matches) == 1
    finding = matches[0]
    assert "step 1 waits on FB with no escape" in finding.message
    assert "EnableLimit = 0" in finding.message
    assert finding.suggestion is not None


def test_wait_no_escape_stays_a_warning_in_strict_mode() -> None:
    """A design decision, not a portability violation — never routed to error."""
    prog, tag_map = _build_wait_no_escape()
    report = validate_click_program(prog, tag_map, mode="strict")

    assert not any(f.code == CLK_WAIT_STEP_NO_ESCAPE for f in report.errors)
    assert any(f.code == CLK_WAIT_STEP_NO_ESCAPE for f in report.warnings)


def test_covering_escape_produces_no_finding() -> None:
    Step = Int("Step")
    Acc = Int("Acc")
    Err = Int("Err")
    FB = Bool("FB")

    prog = Program(strict=False)
    with prog:
        with Rung(Step == 1, Acc > 2, FB):
            calc(Step + 1, Step)
        with Rung(Step >= 1, ~FB):
            copy(1, Err)

    tag_map = TagMap(
        [
            Step.map_to(ds[10]),
            Acc.map_to(ds[11]),
            Err.map_to(ds[14]),
            FB.map_to(x[1]),
        ],
        include_system=False,
    )
    report = validate_click_program(prog, tag_map, mode="warn")
    assert not any(f.code == CLK_WAIT_STEP_NO_ESCAPE for f in report.warnings)
