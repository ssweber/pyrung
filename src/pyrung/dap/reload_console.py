"""Hot-reload console commands for the DAP adapter.

Provides ``reload`` and ``autoreload [off]`` commands that re-execute the
program while preserving PLC state (tags, memory, forces, time mode).  The
watcher covers every Python file under the program directory.
"""

from __future__ import annotations

import importlib
import os
import runpy
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from pyrsistent import pmap

from pyrung.core import PLC
from pyrung.core._source import clear_source_index
from pyrung.core.harness import (
    CoilWiring,
    Coupling,
    Harness,
    coil_feedback,
    link_problem,
    rename_coil_state,
    rename_coil_wiring,
)
from pyrung.core.site_memory import (
    PLANT_FEEDBACK_SITE,
    instruction_memory,
    remap_site_memory_report,
    seed_new_edge_memory,
)
from pyrung.core.state import SystemState
from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME
from pyrung.dap.breakpoints import BreakpointManager
from pyrung.dap.console import ConsoleResult, register
from pyrung.dap.handlers.lifecycle_launch import (
    ClickBinding,
    commit_click_project,
    stage_click_project,
)
from pyrung.dap.harness_console import StagedHarness, commit_harness, stage_harness
from pyrung.dap.links import BenchDrop, BenchLink, active_overrides, bench_active

HandlerResult = tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]


def _addresses(tag_map: Any) -> dict[str, str] | None:
    """Each mapped tag's CLICK address, or ``None`` when the program has no tag map."""
    if tag_map is None:
        return None
    return {slot.logical_name: slot.hardware_address for slot in tag_map.mapped_slots()}


@dataclass(frozen=True)
class ReloadPlan:
    """Where a reload puts each force, bench change and declared link, by old tag name.

    ``forces`` maps a forced tag to the tag it moves to, or to ``None`` to release
    it.  ``links`` maps a bench change's feedback to ``{"en": enable, "fb": feedback}``,
    to ``{"cut": True, "fb": feedback}`` for a cut, or to ``None`` to remove it.
    ``declared`` maps the feedback of a link the old program declares to the feedback
    of the link the new program declares in its place, so the coil keeps its state.  A
    force or bench change the plan leaves out follows its tag name.
    """

    forces: Mapping[str, Any] = field(default_factory=dict)
    links: Mapping[str, Any] = field(default_factory=dict)
    declared: Mapping[str, Any] = field(default_factory=dict)


@dataclass(eq=False)
class _Carry:
    """One force, bench change or declared link in a reload, with what became of it."""

    #: The old name of the forced tag, or of the link's feedback.
    tag: str
    kind: str
    planned: bool
    #: The name it goes to in the new program.  ``None`` when released or dropped.
    to: str | None = None
    value: Any = None
    entry: BenchLink | None = None
    new_entry: BenchLink | None = None
    #: The link a declared link's coil runs as in the new program.
    coupling: Coupling | None = None
    released: bool = False
    #: Why it was dropped, ``None`` while it is kept.
    reason: str | None = None
    #: The tag the reason is about.
    about: str | None = None
    detail: str | None = None

    @property
    def dropped(self) -> bool:
        return self.reason is not None

    def drop(self, reason: str, about: str | None = None, detail: str | None = None) -> None:
        self.reason, self.about, self.detail = reason, about, detail


_LABELS = {
    "force": "force",
    "link": "bench change",
    "cut": "bench change",
    "declared": "declared link",
}


def _place(targets: dict[_Carry, tuple[str, Any]]) -> None:
    """Give each force or link in *targets* its new name.

    Two forces or bench changes that end on one tag would fight over it, and neither
    is the right one to keep.  So each one whose tag another also takes is dropped,
    planned or not.  A force dropped this way is released.
    """
    by_name: dict[str, list[_Carry]] = {}
    for carry, (name, _new_entry) in targets.items():
        by_name.setdefault(name, []).append(carry)
    for carry, (name, new_entry) in targets.items():
        others = [other for other in by_name[name] if other is not carry]
        if others:
            names = " and ".join(f"the {_LABELS[other.kind]} on {other.tag}" for other in others)
            verb = "goes" if len(others) == 1 else "go"
            carry.drop("conflict", name, f"{names} also {verb} to {name}")
            carry.released = carry.kind == "force"
            continue
        carry.to, carry.new_entry = name, new_entry


def _plan_forces(old_forces: Mapping[str, Any], plan: Mapping[str, Any]) -> list[_Carry]:
    carries: list[_Carry] = []
    targets: dict[_Carry, tuple[str, Any]] = {}
    for tag, value in old_forces.items():
        carry = _Carry(tag, "force", planned=tag in plan, value=value)
        carries.append(carry)
        raw = plan.get(tag, tag)
        if raw is None:
            carry.released = True
        elif isinstance(raw, str) and raw.strip():
            targets[carry] = (raw.strip(), None)
        else:
            carry.drop("bad plan", tag, "its plan is not a tag name or null")
    _place(targets)
    for tag in plan:
        if tag not in old_forces:
            carry = _Carry(tag, "force", planned=True)
            carry.drop("not forced", tag, f"{tag} is not forced")
            carries.append(carry)
    return carries


_PLAN_LINK_KEYS = frozenset({"en", "fb", "cut"})


def _planned_bench(fb: str, entry: BenchLink, raw: Any) -> tuple[str, BenchLink] | str:
    """The new feedback and bench entry *raw* plans for *fb*, or why the plan is wrong."""
    if not isinstance(raw, Mapping):
        return "its plan is not an object or null"
    unknown = sorted(str(key) for key in raw if key not in _PLAN_LINK_KEYS)
    if unknown:
        return f"its plan has unknown key(s) {', '.join(unknown)}"
    target, en, cut = raw.get("fb"), raw.get("en"), raw.get("cut", False)
    if not isinstance(target, str) or not target.strip():
        return "its plan names no feedback (fb)"
    if not isinstance(cut, bool):
        return "its plan's cut is not true or false"
    target = target.strip()
    if cut:
        if en is not None:
            return "its plan sets both en and cut"
        if entry.coupling is not None:
            return f"its plan is a cut, but {fb} is a bench link"
        return target, BenchLink(None, enabled=entry.enabled)
    if not isinstance(en, str) or not en.strip():
        return "its plan names no enable (en) and is not a cut"
    coupling = entry.coupling
    if coupling is None:
        return f'its plan names an enable, but {fb} is a bench cut (plan it as "cut": true)'
    physical = coupling.physical
    # A link made without physical= is named after its feedback, so it keeps that.
    if physical.name == fb:
        physical = replace(physical, name=target)
    moved = Coupling(en.strip(), target, physical, coupling.trigger_value)
    return target, BenchLink(moved, enabled=entry.enabled)


def _plan_links(
    old_bench: Mapping[str, BenchLink], plan: Mapping[str, Any]
) -> tuple[list[_Carry], dict[_Carry, tuple[str, Any]]]:
    """One entry for each bench change and plan item, and the feedback each one goes to."""
    carries: list[_Carry] = []
    targets: dict[_Carry, tuple[str, Any]] = {}
    for fb, entry in old_bench.items():
        kind = "cut" if entry.coupling is None else "link"
        carry = _Carry(fb, kind, planned=fb in plan, entry=entry)
        carries.append(carry)
        if fb not in plan:
            targets[carry] = (fb, entry)
            continue
        if plan[fb] is None:
            carry.released = True
            continue
        planned = _planned_bench(fb, entry, plan[fb])
        if isinstance(planned, str):
            carry.drop("bad plan", fb, planned)
        else:
            targets[carry] = planned
    for fb in plan:
        if fb not in old_bench:
            carry = _Carry(fb, "link", planned=True)
            carry.drop("no bench change", fb, f"{fb} has no bench change")
            carries.append(carry)
    return carries, targets


def _plan_declared(
    old_harness: Harness | None,
    old_active: Mapping[str, Coupling | None],
    plan: Mapping[str, Any],
) -> tuple[list[_Carry], dict[_Carry, tuple[str, Any]]]:
    """One entry for each item of the plan's ``declared``, and the feedback it goes to.

    Only a declared link that no bench change replaced ran a coil, so only that one
    has a state to move.
    """
    old_declared = {c.fb_name for c in old_harness.declared()} if old_harness else set()
    carries: list[_Carry] = []
    targets: dict[_Carry, tuple[str, Any]] = {}
    for fb, raw in plan.items():
        carry = _Carry(fb, "declared", planned=True)
        carries.append(carry)
        if not isinstance(raw, str) or not raw.strip():
            carry.drop("bad plan", fb, "its plan is not a tag name")
        elif fb not in old_declared:
            carry.drop("not declared", fb, f"the old program declares no link on {fb}")
        elif fb in old_active:
            carry.drop("not declared", fb, f"a bench change replaced the program's link on {fb}")
        else:
            targets[carry] = (raw.strip(), None)
    return carries, targets


def _taken(carry: _Carry, bench_on: bool) -> str | None:
    """The feedback a kept bench change replaces the program's link on after the reload.

    ``None`` when it was dropped or released, and when it waits disabled or with bench off.
    """
    if carry.dropped or carry.to is None or carry.new_entry is None:
        return None
    return carry.to if bench_active(carry.new_entry, bench_on) else None


def _bench_wins(
    bench: list[_Carry], declared_targets: dict[_Carry, tuple[str, Any]], bench_on: bool
) -> dict[_Carry, tuple[str, Any]]:
    """The declared entries that no bench change takes the feedback of.

    A bench change the harness runs replaces the program's link on its feedback, before
    the reload and after it.  So a declared entry onto that feedback is dropped, and the
    bench change stays.
    """
    benched = {_taken(carry, bench_on) for carry in bench} - {None}
    kept: dict[_Carry, tuple[str, Any]] = {}
    for carry, (name, new_entry) in declared_targets.items():
        if name in benched:
            carry.drop(
                "not declared", name, f"a bench change replaces the program's link on {name}"
            )
        else:
            kept[carry] = (name, new_entry)
    return kept


def _declared_couplings(runner: PLC) -> dict[str, Coupling]:
    """The links *runner*'s program declares, by feedback.

    A reload needs them before it builds the new runner, because a moved coil's
    state goes into the state that runner starts from.  Finding them registers the
    linked tags on *runner*.  That does no harm, since the reload builds its own
    runner from *runner*'s program.
    """
    scratch = Harness(runner)
    scratch._discover_couplings()
    return {c.fb_name: c for c in scratch.declared()}


def _coil_moves(
    *,
    bench: list[_Carry],
    declared: list[_Carry],
    old_harness: Harness | None,
    old_active: Mapping[str, Coupling | None],
    bench_on: bool,
    new_runner: PLC,
    new_known: Mapping[str, Any],
) -> dict[str, Coupling]:
    """The coupling each moved coil runs as in the new program, by its old feedback.

    A planned bench link that the old harness ran and the new program accepts runs
    the same coil under its new names, so the coil keeps its memory and its count.
    So does the coil of a declared link the plan moves to a link the new program
    declares with the same kind and trigger.  A coil never moves onto a feedback
    whose own coil stays there, since that coil's state would be lost.  Such an
    entry is dropped as ``in use``.
    """
    moves: dict[str, tuple[_Carry, Coupling]] = {}
    for carry in bench:
        coupling = carry.new_entry.coupling if carry.new_entry is not None else None
        if not carry.planned or carry.dropped or coupling is None:
            continue
        if old_active.get(carry.tag) is not None and link_problem(new_runner, coupling) is None:
            moves[carry.tag] = (carry, coupling)
    old_declared = {c.fb_name: c for c in old_harness.declared()} if old_harness else {}
    # Whether a bench link may move onto a declared feedback depends on the new program too.
    onto_declared = any(c.fb_name in old_declared for _carry, c in moves.values())
    new_declared: dict[str, Coupling] = {}
    if onto_declared or any(carry.to is not None for carry in declared):
        new_declared = _declared_couplings(new_runner)
    for carry in declared:
        target = carry.to
        if target is None:
            continue
        coupling = new_declared.get(target)
        if coupling is None:
            if target in new_known:
                carry.drop("unlinked", target, f"the new program does not link {target}")
            else:
                carry.drop("gone", target, f"{target} is not in the new program")
            continue
        old = old_declared[carry.tag]
        # Another kind of coil keeps other timers, and another trigger fires on
        # other values.  So the old coil's state does not fit, and the new coil settles.
        new_kind, old_kind = coupling.physical.kind, old.physical.kind
        if new_kind != old_kind:
            carry.drop(
                "changed kind",
                target,
                f"{target} is {_article(new_kind)} {new_kind} link in the new program, "
                f"not {_article(old_kind)} {old_kind} link",
            )
        elif old.trigger_value != coupling.trigger_value:
            carry.drop(
                "changed kind", target, f"the new program's link on {target} has another trigger"
            )
        else:
            carry.coupling = coupling
            moves[carry.tag] = (carry, coupling)

    running = {c.fb_name for c in old_harness.couplings()} if old_harness else set()
    by_tag = {carry.tag: carry for carry in bench}

    def stays(fb: str, mover: _Carry, leaving: set[str]) -> bool:
        """Whether the coil the old harness ran on *fb* still runs there after the reload."""
        if fb not in running or fb in leaving:
            return False
        own = by_tag.get(fb)
        if fb in old_active:
            # The old coil on fb was its bench link.
            return own is not None and _taken(own, bench_on) == fb
        taken = any(carry is not mover and _taken(carry, bench_on) == fb for carry in bench)
        return fb in old_declared and fb in new_declared and not taken

    while True:
        leaving = {fb for fb, (_carry, c) in moves.items() if c.fb_name != fb}
        blocked = [
            fb
            for fb, (carry, c) in moves.items()
            if c.fb_name != fb and stays(c.fb_name, carry, leaving)
        ]
        if not blocked:
            break
        # A dropped move keeps its coil on its own feedback, so a move onto that
        # feedback may be blocked now.
        for fb in blocked:
            carry, coupling = moves.pop(fb)
            target = coupling.fb_name
            carry.drop("in use", target, f"a coil already runs on {target}")
    return {fb: coupling for fb, (_carry, coupling) in moves.items()}


def _article(word: str) -> str:
    return "an" if word[0].upper() in "AEIOU" else "a"


def _type_name(tag: Any) -> str:
    """The tag's type with its article, such as ``"an Int"``."""
    name = tag.type.value.capitalize()
    return f"{_article(name)} {name}"


def _why(carry: _Carry) -> str:
    """Why *carry* was dropped, as the rest of "Dropped the force on A because ..."."""
    about = carry.about or carry.tag
    if carry.reason == "gone":
        return f"{about} is not in the new program"
    if carry.reason == "not forceable":
        return f"the new program cannot force {about}"
    if carry.reason == "unlinked":
        return f"the new program does not link {about}"
    if carry.reason == "in program":
        return f"the new program links {about} the same way"
    return carry.detail or str(carry.reason)


def _addr(table: dict[str, str] | None, name: str | None) -> str | None:
    return None if table is None or name is None else table.get(name)


def _carry_report(
    *,
    forces: list[_Carry],
    bench: list[_Carry],
    kept_bench: Mapping[str, BenchLink],
    old_addr: dict[str, str] | None,
    new_addr: dict[str, str] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """List each force and bench change a plain reload moved to another address or dropped.

    Forces and bench changes follow the tag name.  Returns one entry per change
    for clients, and a sentence for each force drop and each move.  A bench
    change that was dropped already has its own note from the harness fit.
    """
    entries: list[dict[str, Any]] = []
    lines: list[str] = []

    def bench_entry(fb: str, entry: BenchLink, tag: str, **fields: Any) -> dict[str, Any]:
        kind = "cut" if entry.coupling is None else "link"
        return {
            "tag": tag,
            "kind": kind,
            "feedback": fb,
            "old": _addr(old_addr, tag),
            "new": _addr(new_addr, tag),
            "dropped": False,
            "enabled": entry.enabled,
            **fields,
        }

    for carry in forces:
        tag = carry.tag
        old, new = _addr(old_addr, tag), _addr(new_addr, tag)
        if not carry.dropped and old == new:
            continue
        entry: dict[str, Any] = {
            "tag": tag,
            "kind": "force",
            "old": old,
            "new": None if carry.dropped else new,
            "dropped": carry.dropped,
            "value": carry.value,
        }
        if carry.dropped:
            entry["reason"] = carry.reason
            lines.append(f"Dropped the force on {tag} because {_why(carry)}.")
        elif old is None:
            lines.append(f"The force on {tag} is now at {new}.")
        elif new is None:
            lines.append(f"The force on {tag} moved from {old} to no address.")
        else:
            lines.append(f"The force on {tag} moved from {old} to {new}.")
        entries.append(entry)

    for carry in bench:
        if carry.dropped and carry.entry is not None:
            # The tag is the one the drop is about, the enable when the enable left.
            tag = carry.about or carry.tag
            entries.append(
                bench_entry(
                    carry.tag, carry.entry, tag, new=None, dropped=True, reason=carry.reason
                )
            )

    for fb, kept in kept_bench.items():
        label = "bench cut" if kept.coupling is None else "bench link"
        tags = [fb] if kept.coupling is None else [fb, kept.coupling.en_name]
        for tag in tags:
            item = bench_entry(fb, kept, tag)
            if item["old"] == item["new"]:
                continue
            entries.append(item)
            old, new = item["old"], item["new"]
            if old is None:
                moved = f"is now at {new}"
            elif new is None:
                moved = f"moved from {old} to no address"
            else:
                moved = f"moved from {old} to {new}"
            if tag == fb:
                lines.append(f"The {label} on {fb} {moved}.")
            else:
                lines.append(f"The {label} on {fb} reads {tag}, which {moved}.")
    return entries, lines


_WHAT = {"force": "force", "link": "bench link", "cut": "bench cut", "declared": "declared link"}


def _plan_report(
    *,
    carries: list[_Carry],
    old_addr: dict[str, str] | None,
    new_addr: dict[str, str] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """List every force and bench change of a planned reload, and every plan entry.

    Unlike a plain reload's report, this lists an entry that kept its name and
    address too.  The caller then sees each change its plan left out.
    """
    entries: list[dict[str, Any]] = []
    lines: list[str] = []
    for carry in carries:
        to = None if carry.dropped or carry.released else carry.to
        entry: dict[str, Any] = {
            "tag": carry.tag,
            "kind": carry.kind,
            "to": to,
            "old": _addr(old_addr, carry.tag),
            "new": _addr(new_addr, to),
            "planned": carry.planned,
            "released": carry.released,
            "dropped": carry.dropped,
        }
        what = _WHAT[carry.kind]
        if carry.kind == "force":
            entry["value"] = carry.value
        elif carry.kind == "declared":
            entry["feedback"] = carry.tag
            entry["enable"] = (
                carry.coupling.en_name if to is not None and carry.coupling is not None else None
            )
        else:
            kept = carry.new_entry if to is not None else None
            entry["feedback"] = carry.tag
            entry["enable"] = (
                kept.coupling.en_name if kept is not None and kept.coupling is not None else None
            )
            entry["enabled"] = carry.entry.enabled if carry.entry is not None else None
        if carry.dropped:
            entry["reason"] = carry.reason
            entry["detail"] = _why(carry)
        entries.append(entry)

        tag = carry.tag
        if carry.reason == "not forced":
            lines.append(f"The plan names a force on {tag}, but {tag} is not forced.")
        elif carry.reason == "no bench change":
            lines.append(f"The plan names a bench change on {tag}, but {tag} has none.")
        elif carry.dropped:
            lines.append(f"Dropped the {what} on {tag} because {_why(carry)}.")
        elif carry.released:
            verb = "Released" if carry.kind == "force" else "Removed"
            lines.append(f"{verb} the {what} on {tag}.")
        elif not carry.planned:
            lines.append(f"The {what} on {tag} is not in the plan, so it stays on {tag}.")
        else:
            if to != tag:
                lines.append(f"Moved the {what} on {tag} to {to}.")
            old_link = carry.entry.coupling if carry.entry is not None else None
            new_link = carry.new_entry.coupling if carry.new_entry is not None else None
            if old_link is not None and new_link is not None:
                if old_link.en_name != new_link.en_name:
                    lines.append(
                        f"The bench link on {to} reads {new_link.en_name} "
                        f"(it read {old_link.en_name})."
                    )
    return entries, lines


@dataclass(frozen=True)
class _StagedReload:
    """A reload built on objects the adapter does not hold yet."""

    runner: PLC
    breakpoints: BreakpointManager
    binding: ClickBinding
    harness: StagedHarness
    summary: str
    carried: list[dict[str, Any]]


def _reload_locked(
    adapter: Any, plan: ReloadPlan | None = None
) -> tuple[str, list[tuple[str, dict[str, Any] | None]], list[dict[str, Any]]]:
    """Re-execute the program file and swap the runner, preserving state.

    Must be called with ``adapter._state_lock`` held.  Returns
    ``(summary_text, events, carried)``.  The caller wraps the first two in a
    ``ConsoleResult`` or emits them via ``_enqueue_internal_event``, and
    ``carried`` lists the forces and bench changes that moved or were dropped.
    With a *plan*, ``carried`` lists every force and bench change and every plan
    entry.

    The reload is built first, and the adapter takes it in one step that cannot
    fail.  A reload that fails leaves the program, mode, forces, harness, bench
    links, tag map and breakpoints as they were.
    """
    if adapter._thread_running_locked():
        raise adapter.DAPAdapterError("Cannot reload while continue is running")
    if adapter._capture.recording:
        raise adapter.DAPAdapterError("Cannot reload while recording; stop recording first")
    try:
        staged = _stage_reload(adapter, plan if plan is not None else ReloadPlan(), plan is None)
    except adapter.DAPAdapterError:
        raise
    except Exception as exc:
        # The new program is wrong (a bad link= trigger, say), so the old one stays.
        raise adapter.DAPAdapterError(f"Reload failed: {exc}") from exc
    _commit_reload(adapter, staged)
    events: list[tuple[str, dict[str, Any] | None]] = [("stopped", adapter._stopped_body("entry"))]
    return staged.summary, events, staged.carried


def _stage_reload(adapter: Any, plan: ReloadPlan, plain: bool) -> _StagedReload:
    """Build the new runner, harness, bench links, forces, tag map and report.

    Reads the adapter and changes nothing on it.  A *plain* reload reports only
    what moved address or was dropped.
    """
    old_runner: PLC = adapter._require_runner_locked()
    old_state: SystemState = old_runner.current_state
    old_forces = dict(old_runner.forces)
    old_time_mode = old_runner._time_mode
    old_dt = old_runner._dt
    old_known_tags = dict(old_runner._known_tags_by_name)
    old_rtc = old_runner._system_runtime._rtc_now(old_state)
    old_addr = _addresses(adapter._tag_map)
    old_harness: Harness | None = adapter._harness
    old_active = active_overrides(adapter)
    force_carries = _plan_forces(old_forces, plan.forces)
    bench_carries, bench_targets = _plan_links(adapter._bench_links, plan.links)
    declared_carries, declared_targets = _plan_declared(old_harness, old_active, plan.declared)
    _place(bench_targets)
    _place(_bench_wins(bench_carries, declared_targets, adapter._bench_links_on))

    program_path = adapter._program_path
    if not program_path:
        raise adapter.DAPAdapterError("No program loaded; cannot reload")
    path = Path(program_path)
    if not path.is_file():
        raise adapter.DAPAdapterError(f"Program file not found: {path}")

    program_dir = os.path.normcase(os.path.abspath(str(path.parent))) + os.sep
    stale = [
        name
        for name, mod in sys.modules.items()
        if isinstance(mod_file := getattr(mod, "__file__", None), str)
        and os.path.normcase(os.path.abspath(mod_file)).startswith(program_dir)
    ]
    for name in stale:
        del sys.modules[name]
    importlib.invalidate_caches()
    clear_source_index()

    previous_dap_flag = os.environ.get("PYRUNG_DAP_ACTIVE")
    os.environ["PYRUNG_DAP_ACTIVE"] = "1"
    try:
        namespace = runpy.run_path(str(path), run_name="__main__")
    except Exception as exc:
        raise adapter.DAPAdapterError(f"Reload failed: {exc}") from exc
    finally:
        if previous_dap_flag is None:
            os.environ.pop("PYRUNG_DAP_ACTIVE", None)
        else:
            os.environ["PYRUNG_DAP_ACTIVE"] = previous_dap_flag

    try:
        new_runner = adapter._discover_runner(namespace)
    except adapter.DAPAdapterError as exc:
        raise adapter.DAPAdapterError(
            "Reload failed: could not discover runner in new program. Old runner preserved."
        ) from exc

    new_known_tags = dict(new_runner._known_tags_by_name)
    warnings: list[str] = []
    tags_to_drop: set[str] = set()
    for name, old_tag in old_known_tags.items():
        new_tag = new_known_tags.get(name)
        if new_tag is None:
            continue
        if old_tag.type != new_tag.type:
            warnings.append(
                f"  {name}: type changed {old_tag.type.name} -> {new_tag.type.name}, "
                f"using new default"
            )
            tags_to_drop.add(name)

    moved_coils = _coil_moves(
        bench=bench_carries,
        declared=declared_carries,
        old_harness=old_harness,
        old_active=old_active,
        bench_on=adapter._bench_links_on,
        new_runner=new_runner,
        new_known=new_known_tags,
    )
    renames = {fb: c.fb_name for fb, c in moved_coils.items() if c.fb_name != fb}
    # A feedback value moves only into a tag of the same type.  A plain reload drops a
    # value whose tag changed type, too.
    retyped = {
        fb
        for fb, new_fb in renames.items()
        if (old := old_known_tags.get(fb)) is None
        or (new := new_known_tags.get(new_fb)) is None
        or old.type != new.type
    }

    patched_tags = {k: v for k, v in old_state.tags.items() if k not in tags_to_drop}
    # Site memory (one-shots, drum and shift edges, edge contacts) is keyed by
    # site, and site keys are ordinal: carry each site's memory to the site of
    # the new program with the same fingerprint, drop what no longer matches.
    remap = remap_site_memory_report(
        old_state.memory, old_runner._site_table, new_runner._site_table
    )
    patched_memory = remap.memory
    if remap.dropped:
        warnings.append(
            f"  {len(remap.dropped)} site memory value(s) had no matching site "
            "in the new program, reset: " + ", ".join(sorted(remap.dropped))
        )
    if renames:
        patched_tags, patched_memory = rename_coil_state(
            patched_tags, patched_memory, renames, retyped
        )
    # An edge contact with no match in the old program (new, or edited) starts
    # from its tag's current value, so a held bit does not rise on the first
    # scan after reload.  A matched contact keeps its memory, written or not.
    patched_memory, seeded_edges = seed_new_edge_memory(
        patched_memory, old_runner._site_table, new_runner._site_table, patched_tags
    )
    if seeded_edges:
        warnings.append(
            f"  {len(seeded_edges)} edge contact(s) new or edited start from "
            "their tag's current value: "
            + ", ".join(
                f"{site.kind}({site.tag})"
                + (f" line {site.source_line}" if site.source_line is not None else "")
                + f" = {'on' if patched_memory[site.memory[0][0]] else 'off'}"
                for site in seeded_edges
            )
        )
    patched_state = SystemState(
        scan_id=old_state.scan_id,
        timestamp=old_state.timestamp,
        tags=pmap(patched_tags),
        memory=pmap(patched_memory),
    )

    new_logic = new_runner._program if new_runner._program is not None else list(new_runner._logic)
    reloaded = PLC(
        logic=new_logic,
        initial_state=patched_state,
        history=new_runner._history_retention_scans,
        cache=new_runner._cache_retention_scans,
        history_budget=new_runner._recent_state_cache_budget,
        checkpoint_interval=new_runner._checkpoint_interval,
        record_all_tags=new_runner._record_all_tags,
        keep_forces_on_run=(
            new_runner.keep_forces_on_run
            if adapter._keep_forces_on_run is None
            else adapter._keep_forces_on_run
        ),
    )
    if old_runner.mode == "stop":
        reloaded.stop()
    reloaded._set_time_mode(old_time_mode, dt=old_dt)
    reloaded._set_rtc_internal(old_rtc, reloaded.current_state.timestamp)

    # A force goes to the tag its plan names, or follows its tag name.  One whose tag
    # is not in the new program is dropped, since PLC.force would accept it and keep it.
    for carry in force_carries:
        target = carry.to
        if target is None:
            continue
        old_tag, new_tag = old_known_tags.get(carry.tag), new_known_tags.get(target)
        if old_tag is not None and new_tag is not None and old_tag.type != new_tag.type:
            carry.drop(
                "type changed", target, f"{target} is {_type_name(new_tag)} in the new program"
            )
        elif (
            new_tag is None
            and reloaded._tag_known_or_reached(target) is None
            and target not in SYSTEM_TAGS_BY_NAME
        ):
            carry.drop("gone", target)
        else:
            try:
                reloaded.force(target, carry.value)
            except Exception:
                carry.drop("not forceable", target)

    binding = stage_click_project(adapter, namespace, path.parent)
    breakpoints = BreakpointManager()
    breakpoints.rebuild_index(reloaded)

    # Each coil the old harness ran carries on from the memory the reload kept.  A coil
    # new to the program, or one of a harness removed before the reload, settles.  A
    # coil the plan moved carries on under its new feedback and enable only, so a coil
    # the new program runs on the old feedback settles.
    carry_coils: set[CoilWiring] = set()
    for wiring in old_harness._coil_wiring() if old_harness is not None else ():
        moved = moved_coils.get(coil_feedback(wiring))
        if moved is None:
            carry_coils.add(wiring)
        else:
            carry_coils.add(rename_coil_wiring(wiring, moved.fb_name, moved.en_name))
    placed = {
        carry.to: carry
        for carry in bench_carries
        if carry.to is not None and carry.new_entry is not None and not carry.dropped
    }
    bench_drops: list[BenchDrop] = []
    staged = stage_harness(
        adapter,
        reloaded,
        {fb: carry.new_entry for fb, carry in placed.items() if carry.new_entry is not None},
        carry_coils,
        bench_drops,
    )
    for fb, entry, reason, tag in bench_drops:
        problem = link_problem(reloaded, entry.coupling) if entry.coupling is not None else None
        placed[fb].drop(reason, tag, problem)
    stale = _drop_coil_memory_no_coupling_owns(reloaded)
    if stale:
        warnings.append(
            f"  {len(stale)} harness coil memory value(s) had no matching coupling, "
            "reset: " + ", ".join(stale)
        )

    # The report reads only what was staged, so it says what the adapter takes below.
    new_addr = _addresses(binding.tag_map)
    if plain:
        carried, carry_lines = _carry_report(
            forces=force_carries,
            bench=bench_carries,
            kept_bench=staged.bench_links,
            old_addr=old_addr,
            new_addr=new_addr,
        )
    else:
        carried, carry_lines = _plan_report(
            carries=[*force_carries, *bench_carries, *declared_carries],
            old_addr=old_addr,
            new_addr=new_addr,
        )
    warnings.extend(f"  {line}" for line in carry_lines)

    scan_id = reloaded.current_state.scan_id
    n_tags = len(reloaded._known_tags_by_name)
    parts = [f"Reloaded at scan {scan_id} ({n_tags} tag(s))"]
    if warnings:
        parts.append("Warnings:")
        parts.extend(warnings)
    if staged.banner:
        parts.append(staged.banner)

    summary = "\n".join(parts)
    return _StagedReload(reloaded, breakpoints, binding, staged, summary, carried)


def _commit_reload(adapter: Any, staged: _StagedReload) -> None:
    """Give the adapter a staged reload.  Nothing here raises, so it takes all of it."""
    adapter._clear_debug_registrations_locked()
    adapter._runner = staged.runner
    adapter._scan_gen = None
    adapter._current_scan_id = None
    adapter._current_step = None
    adapter._current_rung_index = None
    adapter._current_rung = None
    adapter._current_ctx = None
    adapter._breakpoints = staged.breakpoints
    adapter._pending_predicate_pause = False
    adapter._pending_stop = False
    commit_click_project(adapter, staged.binding)
    adapter._bounds_accumulator.clear()
    adapter._notes.clear()
    adapter._action_log.clear()
    adapter._miner_candidates.clear()
    adapter._miner_accepted.clear()
    adapter._miner_suppressed.clear()
    commit_harness(adapter, staged.harness)


def _drop_coil_memory_no_coupling_owns(runner: PLC) -> list[str]:
    """Drop each harness coil memory that no coil of the new harness keeps.

    A reload keeps a coil's memory for the coil of the same kind on the same
    feedback.  Kept for a renamed feedback or another kind, the memory would wait in
    the state.  A later reload back to it would then read a stale value and skip the
    settle at install.
    """
    owned: set[str] = set()
    synthesis = runner._synthesis
    for rung in synthesis.plant if synthesis is not None else ():
        for instr in rung._instructions:
            key = instr._state_key
            if key is not None and key.startswith(PLANT_FEEDBACK_SITE):
                owned.update(f"{prefix}:{key}" for prefix, _default in instruction_memory(instr))
    stale = sorted(
        name
        for name in runner.current_state.memory
        if f":{PLANT_FEEDBACK_SITE}" in name and name not in owned
    )
    if stale:
        runner._edit_tip_tags({}, drop_memory=stale)
    return stale


@register("reload", usage="reload", group="execution")
def _cmd_reload(adapter: Any, _expression: str) -> ConsoleResult:
    summary, events, carried = _reload_locked(adapter)
    return ConsoleResult(summary, events=events, body={"carried": carried})


def _collect_py_mtimes(directory: Path) -> dict[str, float]:
    mtimes: dict[str, float] = {}
    for root, _dirs, files in os.walk(directory):
        for f in files:
            if f.endswith(".py"):
                full = os.path.join(root, f)
                try:
                    mtimes[full] = os.stat(full).st_mtime
                except OSError:
                    pass
    return mtimes


def _autoreload_loop(adapter: Any, program_dir: Path, stop_event: threading.Event) -> None:
    last_mtimes = _collect_py_mtimes(program_dir)
    while not stop_event.wait(timeout=1.0):
        current_mtimes = _collect_py_mtimes(program_dir)
        if current_mtimes == last_mtimes:
            continue
        stop_event.wait(timeout=0.3)
        current_mtimes = _collect_py_mtimes(program_dir)
        last_mtimes = current_mtimes

        with adapter._state_lock:
            if adapter._thread_running_locked():
                adapter._enqueue_internal_event(
                    "output",
                    {
                        "category": "console",
                        "output": "[autoreload] Skipped: continue is running\n",
                    },
                )
                continue
            try:
                summary, events, carried = _reload_locked(adapter)
            except Exception as exc:
                adapter._enqueue_internal_event(
                    "output",
                    {
                        "category": "console",
                        "output": f"[autoreload] Reload failed: {exc}\n",
                    },
                )
                continue

        adapter._enqueue_internal_event(
            "output",
            {
                "category": "console",
                "output": f"[autoreload] {summary}\n",
                "data": {"carried": carried},
            },
        )
        for event_name, event_body in events:
            adapter._enqueue_internal_event(event_name, event_body)
        adapter._request_state_frame({"origin": "autoreload", "command": "reload"})


def start_autoreload(adapter: Any) -> str | None:
    if getattr(adapter, "_autoreload_thread", None) is not None:
        return None
    program_path = adapter._program_path
    if not program_path:
        return None

    program_dir = Path(program_path).parent
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_autoreload_loop,
        args=(adapter, program_dir, stop_event),
        daemon=True,
        name="pyrung-dap-autoreload",
    )
    adapter._autoreload_stop_event = stop_event
    adapter._autoreload_thread = thread
    thread.start()
    return f"Auto-reload enabled for {program_dir.name}/"


def stop_autoreload(adapter: Any) -> None:
    stop_event: threading.Event | None = getattr(adapter, "_autoreload_stop_event", None)
    thread: threading.Thread | None = getattr(adapter, "_autoreload_thread", None)
    if thread is None or stop_event is None:
        return
    stop_event.set()
    thread.join(timeout=2.0)
    adapter._autoreload_thread = None
    adapter._autoreload_stop_event = None


@register("autoreload", usage="autoreload [off]", group="execution")
def _cmd_autoreload(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) >= 2 and parts[1].lower() == "off":
        stop_autoreload(adapter)
        return ConsoleResult("Auto-reload disabled")
    msg = start_autoreload(adapter)
    if msg is None:
        return ConsoleResult("Already auto-reloading")
    return ConsoleResult(msg)
