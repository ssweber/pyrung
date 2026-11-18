"""why(), cause() and effect() follow a write through a pointer that lists no targets.

A write through a pointer with no declared bounds, into a block of more than 1000
elements, names none of its targets in ``writers_of``.  A recording shows which
element the write stored into.  A bare snapshot cannot, so ``why_cause`` with no
recording shows such a rung as a writer that may have stored the value, marked ``?``.
"""

from __future__ import annotations

from typing import Any

from pyrung import PLC, Block, Bool, Int, Program, copy, latch, math, out, run_function, rung
from pyrung.core.analysis.causal import CausalChain, why_cause
from pyrung.core.state import SystemState
from pyrung.core.tag import TagType

WRITE = Bool("Write", external=True)
SET = Bool("Set", external=True)
AT = Int("At", external=True)
OFF = Int("Off", external=True)
SOURCE = Int("Source", external=True)
HIGH = Bool("High")


def _program(*, listed: bool = False, dest: Any = None) -> tuple[Program, Block]:
    big = Block("Big", TagType.INT, 1, 2000)
    with Program(strict=False) as prog:
        if listed:
            with rung(SET):
                copy(3, big[5])
        with rung(WRITE):
            copy(SOURCE, big[AT] if dest is None else dest(big))
        with rung(big[5] > 50):
            out(HIGH)
    return prog, big


def _write_once(prog: Program, **pointer: int) -> PLC:
    plc = PLC(prog, dt=0.01)
    plc.step()
    plc.patch({"At": 5, "Source": 100, "Write": True, **pointer})
    plc.step()
    plc.patch({"Write": False})
    plc.step()
    assert plc.current_state.tags["Big5"] == 100
    return plc


def _writers(chain: CausalChain, tag_name: str) -> list[int]:
    return [s.rung_index for s in chain.steps if s.transition.tag_name == tag_name]


def _uncertain(chain: CausalChain) -> list[int]:
    return [s.rung_index for s in chain.steps if s.uncertain]


def _snapshot_why(plc: PLC, tag: Bool, **moved: int) -> CausalChain:
    """Run why() over the state alone, with no recording to read.

    *moved* sets tags in that state, as when a pointer has since moved on.  The trial
    scan starts from that state too.
    """
    state = plc.current_state.with_tags(moved)
    return why_cause(
        logic=plc._logic,
        state=state,
        tags=[tag.name],
        pdg=plc._ensure_pdg(),
        program=plc._program,
        forces=plc.forces,
        trial_scan=lambda: plc._trial_scan_runs(state),
    )


# ---------------------------------------------------------------------------
# why()
# ---------------------------------------------------------------------------


def test_why_reads_where_a_pointer_write_stored_from_the_recording() -> None:
    prog, _big = _program()
    chain = _write_once(prog).why(HIGH)

    assert _writers(chain, "Big5") == [0]
    assert not _uncertain(chain)
    assert "Big5" not in {t.tag_name for t in chain.ambiguous_roots + chain.conjunctive_roots}
    assert chain.confidence == 1.0
    assert "r1: copy(Big5)" in str(chain)


def test_why_leaves_out_a_pointer_write_that_stored_elsewhere() -> None:
    # The program may aim the write at Big5, but the run stored it in Big6.
    prog, _big = _program()
    plc = PLC(prog, dt=0.01)
    plc.patch({"At": 6, "Source": 100, "Write": True, "Big5": 60})
    plc.step()
    assert plc.current_state.tags["Big6"] == 100
    assert _writers(_snapshot_why(plc, HIGH), "Big5") == [0]

    chain = plc.why(HIGH)
    assert _writers(chain, "Big5") == []
    assert [t.tag_name for t in chain.conjunctive_roots] == ["Big5"]
    assert "?" not in str(chain)


def test_why_credits_an_earlier_pointer_write_that_stored_the_value() -> None:
    # The last write went to Big6.  The one before it stored the 100 that Big5 holds.
    prog, _big = _program()
    plc = PLC(prog, dt=0.01)
    plc.patch({"At": 5, "Source": 100, "Write": True})
    plc.step()
    plc.patch({"At": 6})
    plc.step()

    chain = plc.why(HIGH)
    assert _writers(chain, "Big5") == [0]
    assert not _uncertain(chain)


def test_why_follows_a_pointer_aimed_by_a_force_upstream() -> None:
    # The program stores only 150 in Cmd, so it aims the write only at Big1500.  A force
    # on Cmd aims it at Big10.  The graph does not see that, but the recording does.
    big = Block("Big", TagType.INT, 1, 2000)
    cmd, ptr = Int("Cmd"), Int("Ptr")
    with Program(strict=False) as prog:
        with rung(SET):
            copy(150, cmd)
        with rung():
            math(cmd * 10, ptr)
        with rung(WRITE):
            copy(SOURCE, big[ptr])
        with rung(big[10] > 50):
            out(HIGH)
    plc = PLC(prog, dt=0.01)
    plc.force("Cmd", 1)
    plc.patch({"Source": 100, "Write": True})
    plc.step()
    assert plc.current_state.tags["Big10"] == 100

    for chain in (plc.why(HIGH), _snapshot_why(plc, HIGH)):
        assert _writers(chain, "Big10") == [2]
        assert not _uncertain(chain)


def _overwritten_then_restored(prog: Program, restore: dict[str, Any]) -> PLC:
    # r1 stores 100 in Big5, then 0, then moves to Big6.  *restore* puts 100 back.
    plc = PLC(prog, dt=0.01)
    plc.patch({"At": 5, "Source": 100, "Write": True})
    plc.step()
    plc.patch({"Source": 0})
    plc.step()
    plc.patch({"At": 6})
    plc.step()
    plc.patch({"Write": False, **restore})
    plc.step()
    plc.step()
    assert plc.current_state.tags["Big5"] == 100
    return plc


def test_why_leaves_out_a_pointer_write_a_patch_overwrote() -> None:
    prog, _big = _program()
    plc = _overwritten_then_restored(prog, {"Big5": 100})

    chain = plc.why(HIGH)
    assert _writers(chain, "Big5") == []
    assert [t.tag_name for t in chain.conjunctive_roots] == ["Big5"]


def test_why_names_the_pointer_write_that_stored_last() -> None:
    big = Block("Big", TagType.INT, 1, 2000)
    at2, k, w2 = Int("At2", external=True), Int("K", external=True), Bool("W2", external=True)
    with Program(strict=False) as prog:
        with rung(WRITE):
            copy(SOURCE, big[AT])
        with rung(w2):
            copy(k, big[at2])
        with rung(big[5] > 50):
            out(HIGH)
    plc = _overwritten_then_restored(prog, {"W2": True, "At2": 5, "K": 100})

    assert _writers(plc.why(HIGH), "Big5") == [1]


def test_why_leaves_out_a_last_pointer_write_a_patch_overwrote() -> None:
    # r1's last write stored 100 in Big5.  Its pointer moved on, and a patch stored 0 and
    # then 100.
    prog, _big = _program()
    plc = _write_once(prog)
    plc.patch({"Big5": 0, "At": 6})
    plc.step()
    plc.patch({"Big5": 100})
    plc.step()

    assert _writers(plc.why(HIGH), "Big5") == []


def test_why_marks_a_pointer_write_older_than_the_history() -> None:
    # The state says Big5 held 100 before the first scan, so no scan shows who stored it.
    prog, _big = _program()
    dump = {"Big5": 100, "At": 7, "Source": 3, "Write": True}
    plc = PLC(prog, dt=0.01, initial_state=SystemState().with_tags(dump))
    plc.step()

    chain = plc.why(HIGH)
    assert _uncertain(chain) == [0]
    assert chain.confidence < 1.0


def test_why_reads_a_pointer_write_from_scans_a_compiled_run_did_not_record() -> None:
    # The compiled run records nothing for its later scans.  The pointer moves off Big7
    # onto Big5 inside them.
    big = Block("Big", TagType.INT, 1, 2000)
    cnt, ptr = Int("Cnt"), Int("Ptr")
    with Program(strict=False) as prog:
        with rung(cnt < 30000):
            math(cnt + 1, cnt)
        with rung(cnt > 1500):
            math(AT - 2, ptr)
        with rung(cnt <= 1500):
            copy(AT, ptr)
        with rung(WRITE):
            copy(SOURCE, big[ptr])
        with rung(big[5] > 50):
            out(HIGH)
    plc = PLC(prog, dt=0.01)
    plc.patch({"Source": 100, "Write": True, "At": 7})
    plc.run_for(20.0, jit=True)
    assert plc.current_state.tags["Big5"] == 100
    assert plc._unrecorded_scans

    chain = plc.why(HIGH)
    assert _writers(chain, "Big5") == [3]
    assert not _uncertain(chain)


def _rewrites(*, pointer: bool, listed_first: bool) -> PLC:
    # Something else stores 100 in Big5 first.  Then r2 (or r1) stores 100 again every scan.
    big = Block("Big", TagType.INT, 1, 2000)
    first = Bool("First", external=True)
    with Program(strict=False) as prog:
        if listed_first:
            with rung(first):
                copy(100, big[5])
        with rung(WRITE):
            copy(SOURCE, big[AT] if pointer else big[5])
        with rung(big[5] > 50):
            out(HIGH)
    plc = PLC(prog, dt=0.01)
    plc.patch({"First": True} if listed_first else {"Big5": 100})
    plc.step()
    plc.patch({"First": False, "At": 5, "Source": 100, "Write": True})
    for _ in range(5):
        plc.step()
    return plc


def test_why_names_a_pointer_write_that_stores_the_value_again_as_a_listed_one() -> None:
    for listed_first in (False, True):
        chain = _rewrites(pointer=True, listed_first=listed_first).why(HIGH)
        twin = _rewrites(pointer=False, listed_first=listed_first).why(HIGH)
        assert str(chain) == str(twin), listed_first
        assert _writers(chain, "Big5") == ([0, 1] if listed_first else [0])
        assert not _uncertain(chain)


def _blocked(*, pointer: bool, fired_before: bool) -> PLC:
    # A reset stores 0 in Big5.  Then the pointer aims at Big5, with Write off.
    big = Block("Big", TagType.INT, 1, 2000)
    reset = Bool("Reset", external=True)
    with Program(strict=False) as prog:
        with rung(reset):
            copy(0, big[5])
        with rung(WRITE):
            copy(SOURCE, big[AT] if pointer else big[5])
        with rung(big[5] > 50):
            out(HIGH)
    plc = PLC(prog, dt=0.01)
    if fired_before:
        plc.patch({"At": 6, "Source": 100, "Write": True})
        plc.step()
        plc.patch({"Write": False})
        plc.step()
    plc.patch({"Big5": 7})
    plc.step()
    plc.patch({"Reset": True})
    plc.step()
    plc.patch({"Reset": False, "At": 5, "Source": 100})
    plc.step()
    assert plc.current_state.tags["Big5"] == 0
    return plc


def test_why_shows_a_pointer_write_aimed_at_the_tag_blocked_as_a_listed_one() -> None:
    for fired_before in (True, False):
        plc = _blocked(pointer=True, fired_before=fired_before)
        twin = str(_blocked(pointer=False, fired_before=fired_before).why(HIGH))
        assert str(plc.why(HIGH)) == twin, fired_before
        assert "blocked Write(False)" in twin
        # With no recording, the pointer's aim in the state is enough.
        assert str(_snapshot_why(plc, HIGH)) == twin, fired_before


LOG = Bool("Log", external=True)
VALUE = Int("Value", external=True)


def _logged(prog: Program, **patch: Any) -> PLC:
    plc = PLC(prog, dt=0.01)
    plc.patch({"Idx": 5, "Value": 7, "Log": True, **patch})
    plc.step()
    return plc


def test_why_does_not_credit_a_logger_with_the_element_after_the_one_it_wrote() -> None:
    # The copy stores into Big5, and then the same rung moves Idx on to 6.  A patch put
    # Big6's value there.
    big, idx = Block("Big", TagType.INT, 1, 2000), Int("Idx")
    with Program(strict=False) as prog:
        with rung(LOG):
            copy(VALUE, big[idx])
            math(idx + 1, idx)
        with rung(big[6] > 50):
            out(HIGH)
    plc = _logged(prog, Big6=60)
    assert plc.current_state.tags["Big5"] == 7

    assert _writers(plc.why(HIGH), "Big6") == []
    with_no_history = _snapshot_why(plc, HIGH)
    assert _writers(with_no_history, "Big6") == [0]
    assert _uncertain(with_no_history) == [0]


def test_why_does_not_trust_a_pointer_a_later_rung_moves() -> None:
    big, idx = Block("Big", TagType.INT, 1, 2000), Int("Idx")
    with Program(strict=False) as prog:
        with rung(LOG):
            copy(VALUE, big[idx])
        with rung(LOG):
            math(idx + 1, idx)
        with rung(big[6] > 50):
            out(HIGH)
    plc = _logged(prog, Big6=60)

    assert _writers(plc.why(HIGH), "Big6") == []
    assert _uncertain(_snapshot_why(plc, HIGH)) == [0]


def test_why_does_not_trust_a_pointer_a_pending_force_moves() -> None:
    big, cmd = Block("Big", TagType.INT, 1, 2000), Int("Cmd")
    go = Bool("Go", external=True)
    with Program(strict=False) as prog:
        with rung(go):
            copy(150, cmd)
        with rung(WRITE):
            copy(SOURCE, big[cmd * 10])
        with rung(big[10] > 50):
            out(HIGH)
    plc = PLC(prog, dt=0.01)
    plc.patch({"Source": 3, "Go": True, "Write": True, "Big10": 60})
    plc.step()
    plc.force("Cmd", 1)

    # In the next scan r1 stores 150 over the force, so the write stores into Big1500.
    for chain in (plc.why(HIGH), _snapshot_why(plc, HIGH)):
        assert _writers(chain, "Big10") == []


def test_why_does_not_step_a_long_compiled_run_again() -> None:
    from pyrung.core.rung_firings import WHY_SETTLE_SCAN_LIMIT

    prog, _big = _program()
    plc = PLC(prog, dt=0.01)
    plc.patch({"At": 5, "Source": 100, "Write": True})
    plc.run_for((WHY_SETTLE_SCAN_LIMIT + 2000) * 0.01, jit=True)
    plc.patch({"At": 6, "Write": False})
    plc.step()
    unrecorded = list(plc._unrecorded_scans)
    assert unrecorded

    chain = plc.why(HIGH)
    assert _uncertain(chain) == [0]
    assert plc._unrecorded_scans == unrecorded


def test_why_with_no_recording_names_a_pointer_write_that_may_have_stored_the_value() -> None:
    prog, _big = _program()
    chain = _snapshot_why(_write_once(prog), HIGH, At=9)

    (step,) = [s for s in chain.steps if s.transition.tag_name == "Big5"]
    assert step.rung_index == 0
    assert step.uncertain
    assert step.instruction == "copy"
    # The value may be one no rung stored, so the tag is a root that may not apply.
    assert "Big5" in {t.tag_name for t in chain.ambiguous_roots}
    assert "Big5" not in {t.tag_name for t in chain.conjunctive_roots}
    assert chain.confidence < 1.0
    text = str(chain)
    assert "r1: copy(Big5)?" in text
    assert "r3" not in text


def test_why_keeps_a_pointer_write_beside_a_listed_writer() -> None:
    prog, _big = _program(listed=True)
    chain = _snapshot_why(_write_once(prog), HIGH, At=9)

    assert sorted(_writers(chain, "Big5")) == [0, 1]
    by_rung = {s.rung_index: s for s in chain.steps if s.transition.tag_name == "Big5"}
    assert not by_rung[0].uncertain
    assert by_rung[1].uncertain


def test_why_puts_the_contacts_of_a_pointer_write_among_the_uncertain_roots() -> None:
    prog, _big = _program()
    plc = PLC(prog, dt=0.01)
    plc.patch({"At": 5, "Source": 100, "Write": True})
    plc.step()
    chain = _snapshot_why(plc, HIGH, At=9)

    # The rung fires, yet its pointer has moved on, so it may have stored elsewhere.
    assert "Write" in {t.tag_name for t in chain.ambiguous_roots}
    assert "Write" not in {t.tag_name for t in chain.conjunctive_roots}


# ---------------------------------------------------------------------------
# cause() and effect()
# ---------------------------------------------------------------------------


def test_cause_names_a_pointer_write_through_an_index_sum() -> None:
    prog, _big = _program(listed=True, dest=lambda big: big[AT + OFF])
    plc = _write_once(prog, Off=0)

    for chain in (plc.cause("Big5"), plc.cause(HIGH)):
        assert chain is not None
        assert _writers(chain, "Big5") == [1]
        assert "Big5" not in {r.tag_name for r in chain.roots}


def test_effect_follows_a_pointer_write() -> None:
    for listed in (False, True):
        prog, _big = _program(listed=listed)
        chain = _write_once(prog).effect("Source")
        assert chain is not None
        assert [s.transition.tag_name for s in chain.steps] == ["Big5", "High"]


def test_cause_does_not_call_a_tag_a_pointer_may_write_never_written() -> None:
    prog, _big = _program()
    plc = _write_once(prog)
    plc.patch({"Big5": 7})
    plc.step()

    chain = plc.cause("Big5")
    assert chain is not None
    assert not chain.steps
    assert [(r.tag_name, r.kind) for r in chain.roots] == [("Big5", "unattributed")]


def test_cause_does_not_credit_a_pointer_write_to_a_tag_of_another_block() -> None:
    # Other5 has address 5, inside Big's range, but no write into Big reaches it.
    big = Block("Big", TagType.INT, 1, 2000)
    other = Block("Other", TagType.INT, 1, 10)
    with Program(strict=False) as prog:
        with rung(WRITE):
            copy(SOURCE, big[AT])
        with rung(other[5] > 50):
            out(HIGH)
    plc = PLC(prog, dt=0.01)
    plc.step()
    plc.patch({"At": 1500, "Source": 100, "Write": True, "Other5": 99})
    plc.step()

    chain = plc.cause("Other5")
    assert chain is not None
    assert not chain.steps


def test_cause_finds_a_pointer_write_into_a_tag_mapped_onto_two_banks() -> None:
    # The tag's own address is the one the last bank to read it gave it.  The write
    # into the first bank still reaches it.
    first = Block("First", TagType.INT, 1, 2000)
    second = Block("Second", TagType.INT, 3001, 5000)
    twice = Int("Twice")
    twice.map_to(first[100])
    twice.map_to(second[3500])
    at_second = Int("AtSecond", external=True)
    with Program(strict=False) as prog:
        with rung(SET):
            copy(3, twice)
        with rung(WRITE):
            copy(SOURCE, first[AT])
        with rung():
            copy(second[at_second], Int("Shown"))
    plc = PLC(prog, dt=0.01)
    plc.step()
    plc.patch({"At": 100, "AtSecond": 3500, "Source": 100, "Write": True})
    plc.step()
    assert plc.current_state.tags["Twice"] == 100

    chain = plc.cause("Twice")
    assert chain is not None
    assert _writers(chain, "Twice") == [1]


def test_a_patched_pointer_reaches_past_what_the_program_stores_in_it() -> None:
    # The program never writes Ptr, so the static facts hold it at 0, outside the block.
    # A patch stores 5 there all the same.
    from pyrung.core.analysis.pdg import build_program_graph

    ptr = Int("Ptr")
    prog, _big = _program(listed=True, dest=lambda big: big[ptr])
    assert not build_program_graph(prog).unlisted_writers_of("Big5")
    plc = _write_once(prog, Ptr=5)

    chain = plc.cause("Big5")
    assert chain is not None
    assert _writers(chain, "Big5") == [1]
    # The recording shows that the write stored the value.
    chain = plc.why(HIGH)
    assert _writers(chain, "Big5") == [0, 1]
    assert not _uncertain(chain)


def test_why_follows_a_forced_pointer_past_what_the_program_stores_in_it() -> None:
    # The program stores only 1500 in Ptr.  Only a force can aim the write at 5.
    big = Block("Big", TagType.INT, 1, 2000)
    ptr = Int("Ptr")
    with Program(strict=False) as prog:
        with rung(SET):
            copy(1500, ptr)
        with rung(WRITE):
            copy(SOURCE, big[ptr])
        with rung(big[5] > 50):
            out(HIGH)
    plc = PLC(prog, dt=0.01)
    plc.patch({"Source": 100, "Write": True})
    plc.step()
    assert not _uncertain(_snapshot_why(plc, HIGH))
    plc.force("Ptr", 5)
    plc.step()
    assert plc.current_state.tags["Big5"] == 100
    for chain in (plc.why(HIGH), _snapshot_why(plc, HIGH)):
        assert _writers(chain, "Big5") == [1]
        assert not _uncertain(chain)


# ---------------------------------------------------------------------------
# A rung that does not conduct: where its write would aim in the next scan
# ---------------------------------------------------------------------------


FAR = Bool("Far")


def _moved_between_scans(*, pointer: bool) -> PLC:
    # Each scan r1 aims Ptr at Big5 and r3 moves it on to Big9.  So the state shows
    # Big9, while r2's write would store into Big5.  An input sets the aim, so the graph
    # lists no targets for the write.
    big, ptr = Block("Big", TagType.INT, 1, 2000), Int("Ptr")
    with Program(strict=False) as prog:
        with rung():
            copy(AT, ptr)
        with rung(WRITE):
            copy(SOURCE, big[ptr] if pointer else big[5])
        with rung():
            math(ptr + 4, ptr)
        with rung(big[5] > 50):
            out(HIGH)
        with rung(big[9] > 50):
            out(FAR)
    plc = PLC(prog, dt=0.01)
    plc.patch({"At": 5, "Source": 100})
    plc.step()
    plc.step()
    assert plc.current_state.tags["Ptr"] == 9
    return plc


def test_why_aims_a_blocked_pointer_write_where_it_sits_in_the_scan() -> None:
    plc = _moved_between_scans(pointer=True)
    twin = str(_moved_between_scans(pointer=False).why(HIGH))
    assert "copy(Big5) -- blocked Write(False)" in twin

    for chain in (plc.why(HIGH), _snapshot_why(plc, HIGH)):
        assert str(chain) == twin
        assert "Big9" not in str(chain)
    # The pointer in the state aims at Big9, but the write would not store there.
    for chain in (plc.why(FAR), _snapshot_why(plc, FAR)):
        assert "copy(Big9) --" not in str(chain)


def test_why_aims_a_blocked_pointer_write_with_the_pending_forces() -> None:
    # The program stores only 150 in Cmd, so it aims the write only at Big1500.  A force
    # set since the last scan aims it at Big10, on Ptr or on Cmd above it.
    aim = Bool("Aim", external=True)
    for forced in ("Ptr", "Cmd"):
        big, cmd, ptr = Block("Big", TagType.INT, 1, 2000), Int("Cmd"), Int("Ptr")
        with Program(strict=False) as prog:
            with rung(SET):
                copy(150, cmd)
            with rung(aim):
                math(cmd * 10, ptr)
            with rung(WRITE):
                copy(SOURCE, big[ptr])
            with rung(big[10] > 50):
                out(HIGH)
        plc = PLC(prog, dt=0.01)
        plc.patch({"Set": True, "Aim": True})
        plc.step()
        # A rung that writes the forced tag would store over the force.
        plc.patch({"Set": False, "Aim": forced == "Cmd"})
        plc.step()
        assert plc.current_state.tags["Ptr"] == 1500
        plc.force(forced, 10 if forced == "Ptr" else 1)

        for chain in (plc.why(HIGH), _snapshot_why(plc, HIGH)):
            assert _writers(chain, "Big10") == [2], forced
            assert not _uncertain(chain), forced
            assert "copy(Big10) -- blocked Write(False)" in str(chain), forced


def test_why_marks_a_blocked_pointer_write_its_own_rung_moves_first() -> None:
    # Turned on, r1 moves Idx on to 6 before its copy reads it.  The trial scan reads
    # Idx as the rung starts, at 5, so it cannot say where the copy would aim.
    big, idx = Block("Big", TagType.INT, 1, 2000), Int("Idx")
    with Program(strict=False) as prog:
        with rung(WRITE):
            math(idx + 1, idx)
            copy(SOURCE, big[idx])
        with rung(big[5] > 50):
            out(HIGH)
    plc = PLC(prog, dt=0.01)
    plc.patch({"Idx": 5})
    plc.step()

    for chain in (plc.why(HIGH), _snapshot_why(plc, HIGH)):
        assert _writers(chain, "Big5") == [0]
        assert _uncertain(chain) == [0]
        assert "copy(Big5)?" in str(chain)


def test_why_leaves_the_plc_as_it_was_after_its_trial_scan() -> None:
    def build() -> PLC:
        plc = _moved_between_scans(pointer=True)
        plc.force("Source", 7)
        plc.patch({"Write": True})
        return plc

    plc, untouched = build(), build()
    state = plc.current_state
    scans = list(plc.history.scan_ids())
    firings = plc.debug.rung_firings()
    unrecorded = list(plc._unrecorded_scans)
    known = dict(plc._known_tags_by_name)

    first = str(plc.why(HIGH))
    assert "copy(Big5)" in first
    assert str(plc.why(HIGH)) == first
    assert plc.current_state is state
    assert list(plc.history.scan_ids()) == scans
    assert [plc.history.at(i) for i in scans] == [untouched.history.at(i) for i in scans]
    assert plc.debug.rung_firings() == firings
    assert plc._unrecorded_scans == unrecorded
    assert plc._known_tags_by_name == known
    assert dict(plc.forces) == {"Source": 7}
    assert dict(plc.pending_patches) == {"Write": True}

    # The next scan runs as if why() had never been asked.
    plc.step()
    untouched.step()
    assert plc.current_state == untouched.current_state
    assert plc.debug.rung_firings() == untouched.debug.rung_firings()


def test_why_reads_a_blocked_write_pointer_where_the_next_scan_starts() -> None:
    # A pending patch turns the logger on.  Its copy reads Idx as 5 and stores into Big5,
    # and then the rung moves Idx on to 6.  Big6's 60 is a patch.
    big, idx = Block("Big", TagType.INT, 1, 2000), Int("Idx")
    with Program(strict=False) as prog:
        with rung(LOG):
            copy(VALUE, big[idx])
            math(idx + 1, idx)
        with rung(big[6] > 50):
            out(HIGH)
    plc = PLC(prog, dt=0.01)
    plc.patch({"Idx": 5, "Value": 7, "Big6": 60})
    plc.step()
    plc.patch({"Log": True})

    for chain in (plc.why(HIGH), _snapshot_why(plc, HIGH)):
        assert "copy(Big6) --" not in str(chain)


def test_why_reads_a_blocked_write_pointer_before_a_later_rung_moves_it() -> None:
    # In the next scan the copy reads Ptr as 5, and then r2 moves it to 9.
    big, ptr = Block("Big", TagType.INT, 1, 2000), Int("Ptr")
    with Program(strict=False) as prog:
        with rung(WRITE):
            copy(SOURCE, big[ptr])
        with rung():
            copy(AT, ptr)
        with rung(big[9] > 50):
            out(HIGH)
    plc = PLC(prog, dt=0.01)
    plc.patch({"At": 5, "Big9": 60})
    plc.step()
    plc.patch({"At": 9})

    for chain in (plc.why(HIGH), _snapshot_why(plc, HIGH)):
        assert "copy(Big9) --" not in str(chain)


def test_why_does_not_credit_a_write_whose_forced_pointer_a_rung_above_moves() -> None:
    # The force on Ptr applies before and after the rungs, so r1 aims the copy at Big9
    # every scan.  Big5's 60 is a patch.
    big, ptr = Block("Big", TagType.INT, 1, 2000), Int("Ptr")
    with Program(strict=False) as prog:
        with rung():
            copy(9, ptr)
        with rung(WRITE):
            copy(SOURCE, big[ptr])
        with rung(big[5] > 50):
            out(HIGH)
    plc = PLC(prog, dt=0.01)
    plc.force("Ptr", 5)
    plc.patch({"Big5": 60, "Source": 3, "Write": True})
    plc.step()
    assert plc.current_state.tags["Big9"] == 3

    for chain in (plc.why(HIGH), _snapshot_why(plc, HIGH)):
        assert _writers(chain, "Big5") == []


def test_why_credits_a_write_that_stores_the_value_again_in_the_trial() -> None:
    # r1 stores 100 in Big5 every scan.  With no recording, the trial scan shows it.
    plc = PLC(_program()[0], dt=0.01)
    plc.patch({"At": 5, "Source": 100, "Write": True})
    plc.step()
    plc.step()

    chain = _snapshot_why(plc, HIGH)
    assert _writers(chain, "Big5") == [0]
    assert not _uncertain(chain)
    # A pending patch makes the next scan store 7.  So the trial cannot say who stored 100.
    plc.patch({"Source": 7})
    assert _uncertain(_snapshot_why(plc, HIGH)) == [0]


def test_why_answers_without_a_trial_scan_that_raises() -> None:
    raising: list[bool] = []

    def poll() -> dict[str, Any]:
        if raising:
            raise RuntimeError("device gone")
        return {}

    big = Block("Big", TagType.INT, 1, 2000)
    with Program(strict=False) as prog:
        with rung():
            run_function(poll)
        with rung(WRITE):
            copy(SOURCE, big[AT])
        with rung(big[5] > 50):
            out(HIGH)
    plc = PLC(prog, dt=0.01)
    plc.patch({"At": 5, "Big5": 60})
    plc.step()
    raising.append(True)

    for chain in (plc.why(HIGH), _snapshot_why(plc, HIGH)):
        assert _uncertain(chain) == [1]
        assert "copy(Big5)?" in str(chain)


# ---------------------------------------------------------------------------
# A pointer the program aims outside the element
# ---------------------------------------------------------------------------

GO = Bool("Go", external=True)
HIT = Bool("Hit")


def _aimed_away() -> Program:
    big = Block("Big", TagType.INT, 1, 2000)
    ptr = Int("Ptr")
    with Program(strict=False) as prog:
        with rung():
            copy(1500, ptr)
        with rung(GO):
            copy(SOURCE, big[ptr])
        with rung(big[5] > 50):
            out(HIT)
    return prog


def _aimed_away_run() -> PLC:
    plc = PLC(_aimed_away(), dt=0.01)
    plc.step()
    plc.patch({"Go": True, "Source": 100, "Big5": 60})
    plc.step()
    assert plc.current_state.tags["Big1500"] == 100
    return plc


def test_cause_calls_an_element_the_program_never_aims_at_never_written() -> None:
    plc = _aimed_away_run()

    chain = plc.cause("Big5")
    assert chain is not None
    assert not chain.steps
    assert [(r.tag_name, r.kind) for r in chain.roots] == [("Big5", "never_written")]


def test_why_leaves_out_a_pointer_write_the_program_aims_elsewhere() -> None:
    plc = _aimed_away_run()

    for chain in (plc.why(HIT), _snapshot_why(plc, HIT)):
        assert [s.transition.tag_name for s in chain.steps] == ["Hit"]
        assert "Go" not in {t.tag_name for t in chain.ambiguous_roots + chain.conjunctive_roots}
        assert "?" not in str(chain)


# ---------------------------------------------------------------------------
# why() roots: contact order and repeats
# ---------------------------------------------------------------------------

MID = Bool("Mid")
X = Bool("X", external=True)


def _order_program(*, big_first: bool) -> PLC:
    big = Block("Big", TagType.INT, 1, 2000)
    with Program(strict=False) as prog:
        with rung(WRITE, MID):
            copy(SOURCE, big[AT])
        with rung(X):
            out(MID)
        with rung(*((big[5] > 50, MID) if big_first else (MID, big[5] > 50))):
            out(HIGH)
    plc = PLC(prog, dt=0.01)
    plc.patch({"At": 5, "Source": 100, "Write": True, "X": True})
    plc.step()
    plc.step()
    assert plc.current_state.tags["High"]
    return plc


def test_why_finds_a_certain_root_whatever_the_contact_order() -> None:
    for big_first in (True, False):
        chain = _snapshot_why(_order_program(big_first=big_first), HIGH, At=9)
        # High needs Mid, and Mid needs X, so X is certain.  Big5 and Write sit under a
        # write that may have stored elsewhere.
        assert [t.tag_name for t in chain.conjunctive_roots] == ["X"], big_first
        assert sorted(t.tag_name for t in chain.ambiguous_roots) == ["Big5", "Write"]
        assert chain.confidence == 0.5
        assert len([s for s in chain.steps if s.transition.tag_name == "Mid"]) == 1


def test_why_counts_each_uncertain_root_once() -> None:
    big = Block("Big", TagType.INT, 1, 2000)
    other = Bool("W2", external=True)
    with Program(strict=False) as prog:
        with rung(WRITE):
            copy(SOURCE, big[AT])
        with rung(WRITE, other):
            copy(SOURCE, big[AT])
        with rung(big[5] > 50, big[6] > 50, big[7] > 50):
            out(HIGH)
    plc = PLC(prog, dt=0.01)
    for at in (5, 6, 7):
        plc.patch({"At": at, "Source": 100, "Write": True, "W2": True})
        plc.step()

    chain = _snapshot_why(plc, HIGH, At=9)
    names = [t.tag_name for t in chain.ambiguous_roots]
    assert sorted(names) == ["Big5", "Big6", "Big7", "W2", "Write"]
    assert chain.confidence == 0.2


def test_why_keeps_every_trigger_that_may_have_set_a_latch() -> None:
    # No pointer here.  Either A or B set L, so both stay ambiguous, as before.
    a, b = Bool("A", external=True), Bool("B", external=True)
    latched, shown = Bool("L"), Bool("Out")
    with Program(strict=False) as prog:
        with rung(a):
            latch(latched)
        with rung(b):
            latch(latched)
        with rung(latched, ~b):
            out(shown)
    plc = PLC(prog, dt=0.01)
    plc.patch({"B": True})
    plc.step()
    plc.patch({"B": False})
    plc.step()

    chain = plc.why(shown)
    assert [t.tag_name for t in chain.ambiguous_roots] == ["A", "B"]
    assert chain.confidence == 0.5


def test_why_counts_a_trigger_shared_by_two_latches_twice() -> None:
    a, c, d = (Bool(name, external=True) for name in "ACD")
    first, second, shown = Bool("L1"), Bool("L2"), Bool("Out")
    with Program(strict=False) as prog:
        with rung(a):
            latch(first)
        with rung(c):
            latch(first)
        with rung(a):
            latch(second)
        with rung(d):
            latch(second)
        with rung(first, second):
            out(shown)
    plc = PLC(prog, dt=0.01)
    plc.patch({"C": True, "D": True})
    plc.step()
    plc.patch({"C": False, "D": False})
    plc.step()

    chain = plc.why(shown)
    assert [t.tag_name for t in chain.ambiguous_roots] == ["A", "C", "A", "D"]
    assert chain.confidence == 0.25


# ---------------------------------------------------------------------------
# cause() credits a write through a pointer only where the run wrote the tag
# ---------------------------------------------------------------------------


def test_cause_does_not_credit_a_pointer_write_whose_rung_did_not_run() -> None:
    prog, _big = _program()
    plc = PLC(prog, dt=0.01)
    plc.step()
    # The rung stays off.  The patch stores into Big5 and moves the copy's source.
    plc.patch({"At": 5, "Source": 100, "Big5": 100})
    plc.step()

    chain = plc.cause("Big5")
    assert chain is not None
    assert not _writers(chain, "Big5")


def test_cause_does_not_credit_a_pointer_write_that_stored_elsewhere() -> None:
    prog, _big = _program()
    plc = PLC(prog, dt=0.01)
    plc.step()
    plc.patch({"At": 7, "Source": 100, "Write": True, "Big5": 100})
    plc.step()
    assert plc.current_state.tags["Big7"] == 100

    chain = plc.cause("Big5")
    assert chain is not None
    assert not _writers(chain, "Big5")
