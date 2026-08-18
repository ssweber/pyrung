"""Query recorded cause chains for PILOT attribution and investigation.

The helpers consume the deep ``cause()`` result, including held supports,
absence links, reset-blocked steps, and classified roots. They expose relevant
root actions and chain tags without independently reconstructing history.

The module also derives empirical evidence that a tag is program-written; that
evidence may remove a presumed steering input but never invent one.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.causal.models import CausalChain
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)


def action_caused_change(
    fork: PLC,
    action_tag: str,
    changed_tag: str,
    steerable: frozenset[str],
    *,
    scan: int | None,
    start_scan: int | None = None,
    timeline: tuple[Any, ...] = (),
) -> bool:
    """Whether one pulse caused a change, using only its executed suffix.

    The inherited prefix is established context. Exact occurrences inside the
    pulse may cross execution epochs transparently through ``PLC.cause()``, but
    this optional agency observation never asks why the window-entry state was
    already true.
    """
    if action_tag not in steerable:
        return False

    end_scan = fork.state.scan_id if scan is None else scan
    first_scan = end_scan if start_scan is None else start_scan
    if first_scan > end_scan:
        return False

    occurrence_scan: int | None = None
    for event in reversed(timeline):
        if event.scan < first_scan or event.scan > end_scan:
            continue
        if any(
            tag == changed_tag and not _values_match(before, after)
            for tag, before, after in event.transitions
        ):
            occurrence_scan = event.scan
            break

    if occurrence_scan is None:
        try:
            states = fork.history.range(
                max(fork.history.oldest_scan_id, first_scan - 1),
                end_scan + 1,
            )
        except Exception:  # noqa: BLE001
            return False
        pairs = zip(states, states[1:], strict=False)
        for before, after in reversed(tuple(pairs)):
            if not _values_match(before.tags.get(changed_tag), after.tags.get(changed_tag)):
                occurrence_scan = after.scan_id
                break
    if occurrence_scan is None or occurrence_scan < first_scan:
        return False

    pending = [(changed_tag, occurrence_scan)]
    visited: set[tuple[str, int]] = set()
    while pending:
        tag, exact_scan = pending.pop()
        key = (tag, exact_scan)
        if key in visited:
            continue
        visited.add(key)
        try:
            local = fork.cause(tag, scan=exact_scan, deep=False, since=first_scan)
        except Exception:  # noqa: BLE001
            continue
        if local is None:
            continue
        for step in local.steps:
            for trigger in step.triggers:
                if trigger.scan_id < first_scan or trigger.scan_id > end_scan:
                    continue
                if trigger.tag_name == action_tag:
                    return True
                pending.append((trigger.tag_name, trigger.scan_id))
            for enabler in step.enablers:
                held_since = enabler.held_since_scan
                if held_since is None or held_since < first_scan or held_since > end_scan:
                    continue
                if enabler.tag_name == action_tag:
                    return True
                pending.append((enabler.tag_name, held_since))
    return False


def _reference_constants(plc: PLC) -> frozenset[str]:
    """Lookup-table reference constants for *plc*'s program, cached per fork.

    ``never_written`` roots include the never-written copy sources that feed a
    jump table's pointer chain (``compute_reference_constants``,
    ``program_facts.py``) —
    declared program constants, not field levers.  Consuming them as nogoods or
    folding them into chain membership would flood the pilot's avoid/nogood
    logic with lookup-table plumbing, so both walkers demote them.

    Computed once per fork (program-static: it depends only on the program shape
    and the external flags, both fixed for a fork's lifetime) and cached on the
    fork's ``__dict__`` alongside the chase memo.  ``frozenset()`` for a
    logic-list PLC with no ``Program`` (the reference-constant shape needs the
    ``Program`` rung/subroutine structure).
    """
    cached = plc.__dict__.get("_pilot_ref_constants")
    if cached is not None:
        return cached
    result: frozenset[str] = frozenset()
    program = getattr(plc, "_program", None)
    if program is not None and hasattr(program, "rungs"):
        try:
            from pyrung.core.analysis.pilot.program_facts import compute_reference_constants

            result = compute_reference_constants(
                plc._ensure_pdg(), program, plc._known_tags_by_name
            )
        except Exception:  # noqa: BLE001
            logger.debug("pilot causal: reference-constant computation failed", exc_info=True)
            result = frozenset()
    plc.__dict__["_pilot_ref_constants"] = result
    return result


def _program_written_changes(
    plc: PLC,
    start_scan: int,
    end_scan: int,
    relevant: frozenset[str],
) -> frozenset[str]:
    """Subset whose transitions have an exact non-PILOT recorded writer.

    State changes locate the relevant scans; the rung and node timelines then
    identify the writer that actually supplied the committed value.  User
    main/subroutine rungs and ``plant`` count as program-owned evidence.
    ``PILOT`` writes, patches, forces, and unattributed changes do not.
    """
    if not relevant:
        return frozenset()
    try:
        states = plc.history.range(start_scan, end_scan + 1)
    except Exception:  # noqa: BLE001
        return frozenset()
    written: set[str] = set()
    log = plc._scan_log.snapshot()
    for prev, cur in zip(states, states[1:], strict=False):
        for tag in relevant:
            if tag in written or _values_match(prev.tags.get(tag), cur.tags.get(tag)):
                continue
            value = cur.tags.get(tag)
            scan_id = cur.scan_id

            # A force is re-applied after user logic and therefore owns the
            # committed value even when an earlier rung happened to write it.
            force_map = plc._replay_force_map_at_scan(scan_id, log)
            if tag in force_map and _values_match(force_map[tag], value):
                continue

            # User logic follows patches and both synthesis brackets.
            main_firings = plc.rung_firings(scan_id)
            if any(
                tag in writes and _values_match(writes[tag], value)
                for writes in main_firings.values()
            ):
                written.add(tag)
                continue

            # A patch drains after synthesis but before user logic.
            patch = log.patches_by_scan.get(scan_id, {})
            if tag in patch and _values_match(patch[tag], value):
                continue

            node_firings = plc._node_firings_at(scan_id)
            matching_nodes = [
                rung_id
                for rung_id, writes in node_firings.items()
                if tag in writes and _values_match(writes[tag], value)
            ]
            if any(rung_id.subroutine == "PILOT" for rung_id in matching_nodes):
                continue
            if any(rung_id.subroutine != "PILOT" for rung_id in matching_nodes):
                written.add(tag)
                continue

            # Main timelines deliberately filter terminal/unread writes.  The
            # recorded cause resolver can recover those from an exact
            # interpreted at-fire replay; consume that writer identity rather
            # than falling back to a tag-name ownership guess.
            try:
                chain = plc.cause(tag, scan=scan_id, deep=False)
            except Exception:  # noqa: BLE001
                chain = None
            if chain is not None:
                writer = next(
                    (
                        step
                        for step in chain.steps
                        if step.transition.tag_name == tag
                        and _values_match(step.transition.to_value, value)
                    ),
                    None,
                )
                if writer is not None and writer.subroutine != "PILOT":
                    written.add(tag)
    return frozenset(written)


def empirical_program_writes(
    plc: PLC,
    candidates: frozenset[str],
    *,
    start_scan: int,
    end_scan: int,
) -> frozenset[str]:
    """Steerable *candidates* the RECORDED RUN testifies the PROGRAM wrote.

    Static steerability is a *hypothesis*; the recorded run is *testimony*.  This
    is **"Verify is the sole source of CONFIRMED" applied to classification**: a
    candidate that changed value inside ``[start_scan, end_scan]`` is demoted
    only when its recorded final writer was a user/subroutine rung or ``plant``.
    A recorded ``PILOT`` writer is exact negative evidence, so guarded holds no
    longer taint later plant/user restoration merely because they share a tag.

    **Fail-safe: positive evidence only.**  A candidate with no attributable
    non-PILOT transition keeps its static verdict unchanged.  The function only
    ever demotes; it never promotes anything.
    """
    return _program_written_changes(plc, start_scan, end_scan, frozenset(candidates))


def chase_cause_roots(
    plc: PLC,
    tag: str,
    steerable: frozenset[str],
    *,
    scan: int | None = None,
    since: int | None = None,
    empirical_writes: frozenset[str] | None = None,
) -> tuple[set[str], list[tuple[str, Any]]]:
    """Chase the deep ``cause()`` chain to steerable-input roots.

    Returns ``(nogoods, holds)`` where:
    - *nogoods*: steerable inputs whose transition caused the regression
    - *holds*: ``(tag, value)`` pairs for inputs that must stay at their
      pre-transition value to prevent the regression

    The single deep chain already crossed every held-support hop (temporal +
    absence) and classified its terminals, so the walk here is a graph traversal
    over the chain's steps that stops at the *nearest* steerable lever — no
    history re-walking.  When no steerable lever *moved*, the absence causes are
    read off the chain's held/reset-blocked steps and ``roots`` (external /
    never-written, minus lookup-table reference constants): a sensor that never
    moved starving a watchdog is the hold, even with no mover.

    *empirical_writes* (opt-in) is the **empirical steerable veto**
    (:func:`empirical_program_writes`): tags that look steerable to the static
    classifier but that the recorded run shows the *program* wrote in the incident
    window.  Such a tag must not be a terminal nogood — the walk descends through
    it toward the real root.  ``None`` or an empty set applies no empirical veto;
    positive evidence only ever demotes, never promotes.
    """
    # Empirical veto: demote statically-steerable tags the recorded run shows the
    # PROGRAM wrote, so the walk descends through them instead of nogood-stopping
    # (see ``empirical_program_writes``).  Purely a subtraction from ``steerable``.
    steerable_eff = steerable - empirical_writes if empirical_writes else steerable

    # Cross-chase result memo, stored on the fork.  chase_cause_roots is pure for
    # a fixed fork — ``cause()`` is pure for a fixed fork (see ``_cause``) and a
    # fork's recorded history at a *past* scan is immutable — so
    # ``(tag, scan, since, steerable_eff) -> (nogoods, holds)`` is stable for the fork's
    # lifetime.  The verify loops re-chase the same key dozens of times, so the
    # memo saves ~95% of ``cause()`` calls.  ``fork()`` / ``load_world()`` hand
    # back a fresh fork with an empty memo, so it is invalidated by construction.
    # Only a resolved historical ``scan`` is memoized: ``scan is None`` resolves
    # against the moving tip.
    memo: dict[Any, Any] | None = None
    memo_key: tuple[Any, ...] | None = None
    if scan is not None:
        memo = plc.__dict__.get("_pilot_chase_memo")
        if memo is None:
            memo = plc.__dict__["_pilot_chase_memo"] = {}
        memo_key = (tag, scan, since, steerable_eff)
        cached = memo.get(memo_key)
        if cached is not None:
            return cached

    chain = _shared_cause(plc, tag, scan, since=since)
    if chain is None:
        result: tuple[set[str], list[tuple[str, Any]]] = (set(), [])
    else:
        result = _roots_from_chain(chain, plc, steerable_eff)
    if memo is not None:
        memo[memo_key] = result
    return result


def chase_chain_tags(
    plc: PLC,
    tag: str,
    *,
    scan: int | None = None,
    since: int | None = None,
) -> set[str]:
    """Every meaningful tag on the deep cause chain of *tag*'s transition.

    Causal-primacy ranking needs chain *membership* (is this watchdog Done part
    of why the channel register moved?), which :func:`chase_cause_roots` alone
    cannot answer. The fired transition spine itself remains meaningful even
    when none of its terminals is steerable.

    The deep walk crosses the opaque-pipeline hop natively (the held
    ``StateRequested`` / enable-flag enabler is chased to the requester's guard
    chain), so the watchdog Done sits in the chain without any route inversion.

    Membership is the chain's **spine** — step transitions, their triggers, and
    classified roots. Steady enablers remain enablers rather than gaining
    trigger standing merely through recursion. System tags (``sys.*`` /
    ``rtc.*``) and lookup-table reference constants are dropped.
    """
    chain = _shared_cause(plc, tag, scan, since=since)
    if chain is None:
        return set()
    return chain_tags(plc, chain)


def chain_tags(plc: PLC, chain: CausalChain) -> set[str]:
    """Read meaningful spine membership from an already-built chain."""
    ref_consts = _reference_constants(plc)
    spine: set[str] = {chain.effect.tag_name}
    for step in chain.steps:
        spine.add(step.transition.tag_name)
        for trig in step.triggers:
            spine.add(trig.tag_name)
    for root in chain.roots:
        spine.add(root.tag_name)
    for tr in chain.conjunctive_roots:
        spine.add(tr.tag_name)
    tags = {t for t in spine if not t.startswith(("sys.", "rtc."))}
    return tags - ref_consts


def _shared_cause(
    plc: PLC,
    tag: str,
    scan: int | None = None,
    cache: dict[tuple[str, int | None, int | None], Any] | None = None,
    *,
    since: int | None = None,
) -> CausalChain | None:
    """Shared deep ``cause()`` for PILOT consumers on one fixed fork.

    The same ``(tag, scan, since)`` reappears across overlapping chases, and each call
    can fork+replay a historical view, so a per-chase cache avoids re-resolving
    the same registers dozens of times (``cause()`` is pure for a fixed fork).
    ``deep=True`` (the default) recursively explains enablers on fired rungs
    through observed value origins and classifies terminals in ``chain.roots``.
    """
    key = (tag, scan, since)
    if cache is not None and key in cache:
        return cache[key]
    # An explicit historical scan is immutable on a fixed fork. Keep the
    # completed causal chain—not its thousands of replay captures—so separate
    # investigation passes over the same incident do not reconstruct identical
    # per-scan RungRun evidence. A tip-relative query remains uncached.
    shared: dict[tuple[str, int | None, int | None], CausalChain | None] | None = None
    if scan is not None:
        # The execution epoch owns historical truth for this scan.  Several
        # counterfactual PILOT forks can share that immutable prefix while
        # remaining distinct worlds after their fork boundary; caching on the
        # transient child makes every sibling reconstruct the same exact
        # RungRun occurrences.  Resolve the owner first so only genuinely
        # shared history shares a completed causal chain.
        owner = plc._causal_lineage.owner_at(scan)
        if owner is None:
            shared = plc.__dict__.setdefault("_pilot_cause_memo", {})
        else:
            shared = owner.cause_memo
        if key in shared:
            result = shared[key]
            if cache is not None:
                cache[key] = result
            return result
    try:
        if scan is not None:
            result = (
                owner.cause(plc._causal_lineage, tag, scan=scan, since=since)
                if owner is not None
                else plc.cause(tag, scan=scan, since=since)
            )
        else:
            result = plc.cause(tag)
    except Exception:  # noqa: BLE001
        logger.debug("pilot causal: cause(%s) raised", tag, exc_info=True)
        result = None
    if shared is not None:
        shared[key] = result
    if cache is not None:
        cache[key] = result
    return result


def occurrence_external_supports(
    chain: CausalChain | None,
    producer_rungs: frozenset[int],
    steerable: frozenset[str],
    accomplishments: frozenset[str],
) -> tuple[tuple[str, Any], ...]:
    """External supports on an exact producer occurrence's recorded branch.

    ``cause()`` has already selected the fired writers and recursively explained
    their steady enablers.  This reader only partitions that existing graph:
    target-owned accomplishment tags are boundaries, while the first steerable
    support on every remaining branch is returned.  It performs no history
    lookup, writer search, or guard reconstruction.
    """
    if chain is None or not producer_rungs:
        return ()
    steps_by_tag: dict[str, list[Any]] = {}
    for step in chain.steps:
        steps_by_tag.setdefault(step.transition.tag_name, []).append(step)

    supports: list[tuple[str, Any]] = []
    seen_supports: set[tuple[str, str]] = set()
    visited: set[tuple[str, str, int, int | None]] = set()

    def _add(tag: str, value: Any) -> None:
        key = (tag, repr(value))
        if key not in seen_supports:
            seen_supports.add(key)
            supports.append((tag, value))

    def _precedes(candidate: Any, consumer: Any) -> bool:
        """Whether a supplying transition was visible to the consumer."""
        if candidate.scan_id != consumer.scan_id:
            return candidate.scan_id < consumer.scan_id
        candidate_ordinal = getattr(candidate, "occurrence_ordinal", None)
        consumer_ordinal = getattr(consumer, "occurrence_ordinal", None)
        return (
            True
            if candidate_ordinal is None or consumer_ordinal is None
            else candidate_ordinal < consumer_ordinal
        )

    def _walk(tag: str, value: Any, consumer: Any) -> None:
        if tag in accomplishments:
            return
        if tag in steerable:
            _add(tag, value)
            return
        visit_key = (
            tag,
            repr(value),
            consumer.scan_id,
            getattr(consumer, "occurrence_ordinal", None),
        )
        if visit_key in visited:
            return
        visited.add(visit_key)
        for step in steps_by_tag.get(tag, ()):
            if not _values_match(step.transition.to_value, value) or not _precedes(
                step.transition, consumer
            ):
                continue
            for trigger in step.triggers:
                _walk(trigger.tag_name, trigger.to_value, step.transition)
            for enabler in step.enablers:
                _walk(enabler.tag_name, enabler.value, step.transition)

    for step in chain.steps:
        if step.rung_index not in producer_rungs:
            continue
        for trigger in step.triggers:
            _walk(trigger.tag_name, trigger.to_value, step.transition)
        for enabler in step.enablers:
            _walk(enabler.tag_name, enabler.value, step.transition)
    return tuple(supports)


def _roots_from_chain(
    chain: CausalChain,
    plc: PLC,
    steerable: frozenset[str],
) -> tuple[set[str], list[tuple[str, Any]]]:
    """Nogoods + holds from a single deep chain, stopping at the nearest lever.

    Two passes traverse the flattened chain:

    1. **Mover pass** — descend the trigger graph from the effect (``step
       transition -> step.triggers``), stopping at the first steerable tag on
       each path.  A steerable tag that *moved* becomes a nogood plus a hold at
       its pre-transition value.
    2. **Absence fallback** — only when the mover pass found no steerable lever:
       the cause is a held / never-moved support (a stuck sensor).  Descend the
       held / reset-blocked steps' enablers the same way, and read the chain's
       classified ``roots`` (external / never-written, minus reference
       constants) for the never-moved externals the trigger graph can't reach.
    """
    ref_consts = _reference_constants(plc)

    # Index the flattened chain: written tag -> its steps, and the
    # pre-transition value of every tag that moved (for the hold value).
    steps_by_tag: dict[str, list[Any]] = {}
    from_value: dict[str, Any] = {}
    for step in chain.steps:
        steps_by_tag.setdefault(step.transition.tag_name, []).append(step)
        for tr in step.triggers:
            if not _values_match(tr.from_value, tr.to_value):
                from_value.setdefault(tr.tag_name, tr.from_value)
    for tr in (*chain.conjunctive_roots, *chain.ambiguous_roots):
        if not _values_match(tr.from_value, tr.to_value):
            from_value.setdefault(tr.tag_name, tr.from_value)

    nogoods: set[str] = set()
    holds: list[tuple[str, Any]] = []
    seen_holds: set[tuple[str, Any]] = set()
    visited: set[str] = set()

    def add_hold(name: str, value: Any) -> None:
        if value is None:
            return
        hold = (name, value)
        if hold not in seen_holds:
            seen_holds.add(hold)
            holds.append(hold)

    def take_lever(name: str, hold_value: Any) -> None:
        nogoods.add(name)
        add_hold(name, hold_value)

    def descend(name: str) -> None:
        if name in visited:
            return
        visited.add(name)
        for step in steps_by_tag.get(name, ()):
            for tr in step.triggers:
                if tr.tag_name in steerable:
                    moved = not _values_match(tr.from_value, tr.to_value)
                    take_lever(tr.tag_name, tr.from_value if moved else None)
                else:
                    descend(tr.tag_name)

    # Mover pass. The effect is the departure being explained, not its own
    # corrective lever. When a recorded writer exists, step behind it even if
    # static steerability also happens to classify the destination as writable
    # by a user. This is crucial for state flags: entering Execute is intended
    # progress; the newly conductive input behind its departure is what Pilot
    # must correct. A genuinely external effect has no writer step, so it can
    # still terminate at itself.
    effect = chain.effect.tag_name
    if steps_by_tag.get(effect):
        descend(effect)
    elif effect in steerable:
        take_lever(effect, from_value.get(effect))
    else:
        descend(effect)

    # Absence fallback — no steerable mover, so the cause is a held support.
    if not nogoods:
        for step in chain.steps:
            if step.triggers:
                continue  # a moved-trigger step is the mover pass's business
            for ec in step.enablers:
                name = ec.tag_name
                if name in ref_consts:
                    continue
                if name in steerable:
                    take_lever(name, getattr(ec, "value", None))
                else:
                    descend(name)
        for root in chain.roots:
            name = root.tag_name
            if (
                root.kind in ("external", "never_written")
                and name in steerable
                and name not in ref_consts
            ):
                take_lever(name, root.value)

    return nogoods, holds
