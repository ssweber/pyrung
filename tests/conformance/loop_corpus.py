"""The conformance suite's programs as a corpus for the guarded scan loop.

``run_for()`` and ``run_until()`` must equal stepping, in whole state and scan count, or decline.
The suite's in-process runner, ``pyrung.conformance.run``, drives its programs with ``plc.step()``,
and ``SimServer`` free-runs a realtime PLC, which is not fixed-step.  So no conformance program
reached the loop.  This drives them the way a case is driven (``Cmd`` written, run, read), by long
runs, each through ``tests/fold_check.py``'s ``_checked``: a stepped twin scan by scan, stops asked
on the twin, the runner around the state, history and the scans past the landing.  Where a case
records an expectation the run has to meet it too, as ``run()`` judges it, ``pending`` included: so
loop, stepping and recorded all agree.

:class:`Tally` counts what ran where, so a corpus that stopped reaching the loop says so.
"""

from __future__ import annotations

import warnings
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest

from pyrung.conformance._case import Case, Group, expectation_met, judge
from pyrung.conformance._ladder import case_label, emit_case, footprint
from pyrung.conformance._runner import DT, read_slot
from pyrung.conformance._slot import SLOT_FIELDS, make_slot
from pyrung.core import PLC, Program, Rung, call, compiled_run, fall, rise, scan_trace, subroutine
from pyrung.core.expression import randint_sites
from pyrung.core.scan_trace import DECLINING_SYSTEM_TAGS
from pyrung.core.tag import TagType
from tests import fold_check
from tests.conformance import suite

UNCUT_DRAW = "uncut draw"

#: The whole-state differences found, by the case that shows each.  Such a case stays out of the
#: corpus and in ``test_loop_corpus.py``'s ``test_a_hole_still_shows``, which fails the day it
#: closes.
HOLES = {
    "faults/rand_unequal_bounds": UNCUT_DRAW,
    "store_probe/rand_int_cuts": UNCUT_DRAW,
}

CaseFilter = Callable[[Group, Case], bool]


@contextmanager
def fine_loop() -> Iterator[None]:
    """As ``--fold-check`` runs the loop: every run in it, a few scans a firing, strictly."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(compiled_run, "INTERPRETED_PREFIX", 1)
        patch.setattr(scan_trace, "MIN_RUN_SCANS", 1)
        patch.setattr(scan_trace, "BURST_SIZES", fold_check._LOOP_BURSTS)
        patch.setattr(scan_trace, "STRICT_SELF_CHECK", True)
        patch.setattr(scan_trace, "SELF_CHECK_OBSERVERS", [fold_check._note_self_check_mismatch])
        yield


@contextmanager
def production_constants() -> Iterator[None]:
    """A run as a user gets it: an interpreted prefix, then the loop with no firing limit."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(compiled_run, "INTERPRETED_PREFIX", 1000)
        patch.setattr(scan_trace, "MIN_RUN_SCANS", 1000)
        patch.setattr(scan_trace, "BURST_SIZES", None)
        yield


@dataclass(frozen=True)
class Entry:
    """A case in a program, on its slot."""

    label: str
    case: Case
    fields: Any


@dataclass(frozen=True)
class Subject:
    """A program and the cases in it."""

    label: str
    program: Program
    entries: tuple[Entry, ...]


def subject_of(chosen: Sequence[tuple[Group, Sequence[Case]]], label: str) -> Subject:
    """*chosen* as one program, shaped as ``build_program`` shapes a part, on packed slots."""
    slots = {group.name: make_slot(len(cases), name=group.title) for group, cases in chosen}

    def emit(group: Group, cases: Sequence[Case]) -> None:
        for index, case in enumerate(cases, start=1):
            emit_case(group.name, case, slots[group.name], index)

    with Program(strict=False) as program:
        for group, _ in chosen:
            if not group.in_main:
                for _ in range(group.calls):
                    with Rung():
                        call(group.subroutine)
        for group, cases in chosen:
            if not group.in_main:
                with subroutine(group.subroutine):
                    emit(group, cases)
        for group, cases in chosen:
            if group.in_main:
                emit(group, cases)
    return Subject(
        label,
        program,
        tuple(
            Entry(case_label(group.name, case), case, slots[group.name][index])
            for group, cases in chosen
            for index, case in enumerate(cases, start=1)
        ),
    )


def case_alone(group: Group, case: Case) -> Subject:
    return subject_of([(group, [case])], case_label(group.name, case))


def on_hardware(group: Group, case: Case) -> bool:
    """A case a part as built holds, and whose program the corpus runs."""
    return not case.in_process_only and case_label(group.name, case) not in HOLES


def picks(part: str, keep: CaseFilter) -> list[tuple[Group, list[Case]]]:
    """The cases of *part* that *keep* takes, by group, for :func:`subject_of`."""
    kept = [(group, [c for c in group.cases if keep(group, c)]) for group in suite.groups(part)]
    return [(group, cases) for group, cases in kept if cases]


def subject_of_build(build: Any, label: str) -> Subject:
    """A ``ConformanceProgram``: the program a part loads on a CLICK."""
    return Subject(
        label,
        build.program,
        tuple(
            Entry(case_label(group.name, case), case, group.slot[index])
            for group in build.groups
            for index, case in group.cases
        ),
    )


def whole_run_decline(program: Program) -> str | None:
    """Why the loop refuses every run of *program* (``ScanLoop._program_blocker``), or None."""
    kernel = PLC(program, dt=DT)._compiled_replay_supported_kernel()
    assert kernel is not None
    if randint_sites(kernel.referenced_tags):
        return "randint"
    if DECLINING_SYSTEM_TAGS & set(kernel.referenced_tags):
        return "scan-derived system tag"
    return None


_REASONS = ("run declined: ", "decline: ", "interpreted: ")
_NOT_CHECKED = (
    "predicate skipped",
    "skipped:",
    "loop scans, landing only",
    "loop scans, not compared",
)


@dataclass
class Tally:
    """What the runs of one test did: where their scans ran, and what kept them out of the loop."""

    subjects: int = 0
    runs: int = 0
    scans: int = 0
    loop: int = 0
    real: int = 0
    reasons: Counter[str] = field(default_factory=Counter)
    not_checked: Counter[str] = field(default_factory=Counter)
    never_in_the_loop: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def interpreted(self) -> int:
        return self.scans - self.loop - self.real

    @property
    def loop_share(self) -> float:
        return self.loop / self.scans if self.scans else 0.0

    def add(self, subject: Subject, drill: Callable[[Driver], None], *, dt: float = DT) -> None:
        """Run *drill* on *subject*, noting a mismatch or a missed expectation to report later."""
        self.subjects += 1
        loop_before = self.loop
        driver = Driver(subject, self, dt=dt)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                drill(driver)
            except AssertionError as exc:
                self.failures.append(f"{subject.label}: {exc}")
        if self.loop == loop_before:
            self.never_in_the_loop.append(subject.label)

    def report(self) -> str:
        lines = [
            f"{self.subjects} programs, {self.runs} runs, {self.scans} scans: {self.loop} in the "
            f"loop ({self.loop_share:.0%}), {self.real} real compiled, {self.interpreted} "
            "interpreted"
        ]
        lines.extend(f"  {reason}: {n}" for reason, n in self.reasons.most_common())
        if self.never_in_the_loop:
            lines.append(f"  never in the loop: {', '.join(self.never_in_the_loop)}")
        return "\n".join(lines)

    def note_run(self, ran: int, before: Counter[str], checked_before: Counter[str]) -> None:
        after = scan_trace.TOTALS
        self.runs += 1
        self.scans += ran
        self.loop += after["loop scans"] - before["loop scans"]
        self.real += after["real scans"] - before["real scans"]
        for key in after:
            if key.startswith(_REASONS) and after[key] != before[key]:
                self.reasons[key] += after[key] - before[key]
        for key in fold_check.STATS:
            if key.startswith(_NOT_CHECKED) and fold_check.STATS[key] != checked_before[key]:
                self.not_checked[key] += fold_check.STATS[key] - checked_before[key]


def assert_nothing_found(tally: Tally) -> None:
    assert not tally.failures, "\n".join(tally.failures)


def assert_every_run_checked(tally: Tally) -> None:
    """A run the twin left out, or a stop it could not be asked about, would be a run not checked."""
    assert not tally.not_checked, tally.not_checked


def assert_the_loop_ran(tally: Tally, at_least: float) -> None:
    """A corpus outside the loop proves nothing."""
    assert tally.loop_share >= at_least, (
        f"only {tally.loop_share:.0%} of the scans ran in the loop, at least {at_least:.0%} "
        f"expected:\n{tally.report()}"
    )


#: Each is a program whose scans trace again and again, for seconds (a phase change every few
#: scans).  Alone they would cost as much as their whole part, which runs them anyway.
LEFT_TO_THEIR_PART = {"out_more/preset_mid_phase", "for_more/timers_at_limits"}

#: What each case alone reaches in the loop, by part.
MIN_LOOP_SHARE_ALONE = 0.5

#: The parts of ``test_loop_corpus.py``, about half the time of each case alone: the rest are in
#: ``test_loop_corpus_later_parts.py``, on another xdist worker.
FIRST_PARTS = ("A", "B", "C", "E")


def cases_of(part: str) -> list[tuple[Group, Case]]:
    return [
        (group, case)
        for group in suite.groups(part)
        for case in group.cases
        if case_label(group.name, case) not in HOLES
    ]


def check_each_case_alone(part: str) -> None:
    """Loop, stepping and ``expect`` agree on each case of *part*, and the loop did run."""
    tally = Tally()
    with fine_loop():
        for group, case in cases_of(part):
            if case_label(group.name, case) not in LEFT_TO_THEIR_PART:
                subject = case_alone(group, case)
                tally.add(subject, lambda driver: driver.judged_passes(idle=40))
    print(f"part {part}, each case alone: {tally.report()}")
    assert_nothing_found(tally)
    assert_the_loop_ran(tally, MIN_LOOP_SHARE_ALONE)
    assert_every_run_checked(tally)


class Driver:
    """One PLC on one subject, driven by long runs each compared with a stepped twin."""

    def __init__(self, subject: Subject, tally: Tally, dt: float = DT) -> None:
        self.subject = subject
        self.tally = tally
        self.plc = PLC(subject.program, dt=dt)
        self.dt = dt

    def _checked(self, call: Callable[[], Any], predicate: Any, budget: int) -> None:
        start = self.plc.current_state.scan_id
        before = scan_trace.TOTALS.copy()
        checked_before = fold_check.STATS.copy()
        fold_check._checked(
            "compiled", self.plc, call, predicate, compiled_predicate=True, budget=budget
        )
        self.tally.note_run(self.plc.current_state.scan_id - start, before, checked_before)

    def run_scans(self, scans: int) -> None:
        """``run_for()``, for exactly *scans*: half a scan short so float drift cannot add one."""
        if scans <= 0:
            return
        seconds = (scans - 0.5) * self.dt
        now = self.plc.current_state.timestamp
        assert compiled_run._stepped_scans_until(now, now + seconds, self.dt) == scans
        self._checked(lambda: self.plc.run_for(seconds), None, scans)

    def run_until(self, scans: int, *stop: Any) -> None:
        if scans <= 0:
            return
        predicate = self.plc._compile_condition_predicate(*stop, method="run_until")
        self._checked(lambda: self.plc.run_until(*stop, max_cycles=scans), predicate, scans)

    def _ready(self) -> list[Entry]:
        tags = self.plc.current_state.tags
        return [e for e in self.subject.entries if tags.get(e.fields.Ready.name, 0)]

    def _reset(self) -> list[Entry]:
        """Zero every slot and run until each is ``Ready``, up to ``run()``'s 10 s of scans."""
        entries = self.subject.entries
        self.plc.patch({getattr(e.fields, f).name: 0 for e in entries for f in SLOT_FIELDS})
        self.run_until(round(10.0 / self.dt), *[e.fields.Ready != 0 for e in entries])
        return self._ready()

    def _command(self, ready: Iterable[Entry]) -> None:
        self.plc.patch({e.fields.Cmd.name: 1 for e in ready})

    def _run_to_each_fired(self, ready: Sequence[Entry], scans: int) -> None:
        """*scans* in all, by a ``run_until()`` on the ``Fired`` of each case not yet fired."""
        left = scans
        while left > 0:
            tags = self.plc.current_state.tags
            waiting = [e for e in ready if not tags.get(e.fields.Fired.name, 0)]
            if not waiting:
                self.run_scans(left)
                return
            start = self.plc.current_state.scan_id
            self.run_until(left, waiting[0].fields.Fired != 0)
            left -= self.plc.current_state.scan_id - start

    # --- The drills --------------------------------------------------------------------

    def judged_passes(self, idle: int = 0) -> None:
        """``run()``'s two passes and its verdict, on long runs: ``run_for()``, then the stops.

        With *idle*, a settled stretch after each read and an idle one before each ``Cmd``,
        as over the wire, where a case is read however late the scans come.
        """
        outcomes: dict[str, list[tuple[dict[str, int], bool]]] = {}
        for run in (self.run_scans, None):
            ready = self._reset()
            self.run_scans(idle)
            scans = max(e.case.scans for e in ready)
            self._command(ready)
            if run is None:
                self._run_to_each_fired(ready, scans)
            else:
                run(scans)
            for e in ready:
                outcomes.setdefault(e.label, []).append(read_slot(self.plc, e.fields, e.case))
            self.run_scans(idle)
        missed = []
        for e in self.subject.entries:
            read = outcomes.get(e.label, [])
            met = [fired and expectation_met(e.case.checks, got) for got, fired in read]
            passed, _, note = judge(
                e.case, met, len(read) == 2, read[-1][0] if read else {}, pending=True
            )
            if not passed:
                missed.append(f"{e.label}: expect {e.case.checks}, read {read}. {note}")
        assert not missed, "the loop run misses what is recorded:\n" + "\n".join(missed)

    def chopped_and_cut_short(self, settle: int = 100, idle: int = 100) -> None:
        """Long stretches, a run cut in pieces (each a new loop), and ``Cmd`` taken away early."""
        self.run_scans(settle)
        self._reset()
        self.run_scans(idle)
        ready = self._ready()
        scans = max(e.case.scans for e in ready)
        self._command(ready)
        left = scans
        for size in (2, 3, 5, 8):
            self.run_scans(min(size, left))
            left -= min(size, left)
        self.run_scans(left)
        self._reset()
        self._command(ready)
        self.run_scans(max(2, scans // 2))
        self._reset()

    def stop_on_what_it_writes(self, limit: int = 3) -> None:
        """``run_until()`` on the tags a case writes, in every form a stop can take."""
        known = self.plc._known_tags_by_name
        written = {name for e in self.subject.entries for name in footprint(e.case)[1]}
        tags = [known[name] for name in sorted(written) if name in known]
        numeric = (TagType.BOOL, TagType.INT, TagType.DINT, TagType.WORD)
        for tag in [t for t in tags if t.type in numeric][:limit]:
            if tag.type == TagType.BOOL:
                for form in (tag, ~tag):
                    self._stop_on(form)
                for edge in (rise(tag), fall(tag)):
                    self._stop_on(edge, on_an_edge=True)
            else:
                for form in (tag != 0, tag >= 1):
                    self._stop_on(form)

    def _stop_on(self, form: Any, *, on_an_edge: bool = False) -> None:
        ready = self._reset()
        self._command(ready)
        scans = max(e.case.scans for e in ready) + 5
        twin = fold_check._twin(self.plc) if on_an_edge else None
        self.run_until(scans, form)
        if twin is not None:
            # An edge's memory is the predicate's, so ``_checked`` cannot ask the twin about it
            # scan by scan.  It gets a stepped run of its own, which has to land on the same scan.
            twin.run_until(form, max_cycles=scans, jit=False)
            assert twin.current_state.scan_id == self.plc.current_state.scan_id, (
                f"a stop on {form} landed at scan {self.plc.current_state.scan_id}, "
                f"stepping at {twin.current_state.scan_id}"
            )
        self.run_scans(3)

    def restarted(self) -> None:
        """A run after STOP -> RUN, and after a reboot: a first scan the loop has to refuse."""
        for restart in (self._stop_and_run, self.plc.reboot):
            restart()
            self.run_scans(20)
            ready = self._reset()
            self._command(ready)
            self.run_scans(max(e.case.scans for e in ready))

    def _stop_and_run(self) -> None:
        self.plc.stop()
        self.plc.start()

    def across_rtc_rollovers(self) -> None:
        """The year's last second, then a leap day's: every RTC field moves in the run."""
        self.plc.set_rtc(datetime(2031, 12, 31, 23, 59, 59))
        self.run_scans(round(2.0 / self.dt))
        ready = self._reset()
        self.plc.set_rtc(datetime(2032, 2, 29, 23, 59, 59))
        self._command(ready)
        self.run_scans(round(1.5 / self.dt))

    def long_wait(self, scans: int) -> None:
        """All slots idle, then every case on, for *scans* each."""
        ready = self._reset()
        self.run_scans(scans)
        self._command(ready)
        self.run_scans(scans)
        self._reset()


def every_drill(subject: Subject, tally: Tally) -> None:
    """What ``PYRUNG_LOOP_CORPUS_DEEP`` runs: other time steps cross a timer's limits sooner."""
    with fine_loop():
        tally.add(subject, lambda d: d.judged_passes(idle=100))
        tally.add(subject, Driver.chopped_and_cut_short)
        tally.add(subject, Driver.stop_on_what_it_writes)
        tally.add(subject, Driver.restarted)
        tally.add(subject, Driver.across_rtc_rollovers)
        tally.add(subject, lambda d: d.long_wait(400), dt=0.1)
        tally.add(subject, lambda d: d.long_wait(300), dt=0.007)
    with production_constants():
        tally.add(subject, lambda d: d.long_wait(2500))


def not_checked_by_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under ``--fold-check``, leave this test's runs to it: a hole's own test says what differs.

    A mismatch in the session's twin would fail the session, xfail or no xfail.
    """
    monkeypatch.setattr(fold_check, "_depth", 1)


def assert_equals_stepping(plc: PLC, run: Callable[[], Any], times: int = 1) -> None:
    """*run*, *times* over, on *plc* against the same scans stepped on a twin.

    A compiled run is compared where it lands, and a draw over few values can land on what it
    should be by chance: *times* is what keeps such a difference from passing.
    """
    twin = fold_check._twin(plc)
    for _ in range(times):
        start = plc.current_state.scan_id
        run()
        for _ in range(plc.current_state.scan_id - start):
            twin._run_single_scan(consume_pause_request=False)
        ours, stepped = fold_check._image(plc.current_state), fold_check._image(twin.current_state)
        differs = {k: (ours.get(k), stepped.get(k)) for k in ours.keys() | stepped.keys()}
        differs = {k: v for k, v in differs.items() if v[0] != v[1]}
        assert not differs, f"at scan {plc.current_state.scan_id}, (run, stepped): {differs}"
        assert plc.current_state.timestamp == twin.current_state.timestamp
