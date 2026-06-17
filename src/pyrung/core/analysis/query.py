"""Whole-program dynamic analysis surveys.

``QueryNamespace`` is exposed as ``plc.query`` and provides survey methods
that aggregate dynamic history across retained scans:

- ``cold_rungs()`` — rungs that never fired (1-indexed rung numbers)
- ``hot_rungs()`` — rungs that fired every scan (1-indexed rung numbers)
- ``stranded_bits()`` — persistent bits with no reachable clear path

These are compositions over the causal chain primitives (``cause``/``effect``)
and the per-scan ``rung_firings`` data.

Limitations
-----------
Persistent-bit detection currently considers only ``latch()``-written tags.
Tags written by ``out()`` inside conditionally-called subroutines can also
become stranded if the subroutine stops executing, but detecting that
requires call-graph analysis (not yet implemented).  Similarly, ``out()``
with mutually exclusive rung conditions can leave a tag stranded in
practice despite being structurally self-clearing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyrung.core.context import RungId

if TYPE_CHECKING:
    from pyrung.core.analysis.causal import CausalChain
    from pyrung.core.rung import Rung
    from pyrung.core.runner import PLC
    from pyrung.core.tag import Tag


def find_tag_object(logic: list[Rung], tag_name: str) -> Tag | None:
    """Find a ``Tag`` object by name from a program's rung instructions."""
    from pyrung.core.tag import ImmediateRef
    from pyrung.core.tag import Tag as TagClass

    for rung in logic:
        for instr in rung._instructions:
            target = getattr(instr, "target", None)
            if target is None:
                continue
            raw = target
            if isinstance(raw, ImmediateRef):
                raw = object.__getattribute__(raw, "value")
            if isinstance(raw, TagClass) and raw.name == tag_name:
                return raw
        # Also check conditions for tag references
        for cond in rung._conditions:
            tag_obj = getattr(cond, "tag", None)
            if tag_obj is not None:
                raw = tag_obj
                if isinstance(raw, ImmediateRef):
                    raw = object.__getattribute__(raw, "value")
                if isinstance(raw, TagClass) and raw.name == tag_name:
                    return raw
    return None


def _persistent_bits(logic: list[Rung]) -> list[Tag]:
    """Return tags written by ``latch()`` instructions.

    These are the tags that require an explicit ``reset()`` to clear.
    ``out()``-driven tags are self-clearing (the instruction writes False
    when disabled) and are excluded.

    See module docstring for known limitations (subroutines, mutually
    exclusive outs).
    """
    from pyrung.core.instruction.coils import LatchInstruction
    from pyrung.core.tag import ImmediateRef
    from pyrung.core.tag import Tag as TagClass

    seen: set[str] = set()
    result: list[TagClass] = []
    for rung in logic:
        for instr in rung._instructions:
            if not isinstance(instr, LatchInstruction):
                continue
            target = instr.target
            if isinstance(target, ImmediateRef):
                target = object.__getattribute__(target, "value")
            if isinstance(target, TagClass) and target.name not in seen:
                seen.add(target.name)
                result.append(target)
    return result


def _rung_label(subroutine: str | None, rung_index: int) -> str:
    """User-facing 1-indexed rung label.

    ``"3"`` for a main rung, ``"MySub:3"`` for a rung inside subroutine
    ``MySub`` — matching the ``--- SubName ---`` / ``r{n}`` rendering used
    by ``why()``/``cause()`` chains.
    """
    n = rung_index + 1
    return f"{subroutine}:{n}" if subroutine is not None else str(n)


def _rung_sort_key(ident: tuple[str | None, int]) -> tuple[int, str, int]:
    """Order main rungs first (ascending), then subroutine rungs grouped by name."""
    subroutine, rung_index = ident
    return (0 if subroutine is None else 1, subroutine or "", rung_index)


class QueryNamespace:
    """Survey namespace for whole-program dynamic analysis.

    Accessed via ``plc.query``.  Methods aggregate findings across all
    retained history scans.
    """

    def __init__(self, plc: PLC) -> None:
        self._plc = plc

    def _subroutine_rung_ids(self) -> set[RungId]:
        """All subroutine rungs in the program (the cold/hot universe for subs).

        Drawn from the PDG so subroutine rungs are visible to coverage even
        when they never fire.  Branch rungs (``branch_path != ()``) are
        excluded — branch coverage needs a separate "powered" signal the
        write-firing log can't provide.
        """
        plc = self._plc
        pdg = plc._ensure_pdg() if plc._logic else None
        if pdg is None:
            return set()
        return {
            RungId(node.subroutine, node.rung_index)
            for node in pdg.rung_nodes
            if node.scope == "subroutine" and not node.branch_path
        }

    def cold_rungs(self) -> list[str]:
        """Rung labels that never fired across retained history.

        Backed by :class:`RungFiringTimelines` — a rung with no timeline
        (or an empty timeline) is cold.  Covers main rungs (the int firing
        log) and subroutine rungs (the node firing log), so a subroutine
        that was never called is reported as cold.

        Labels are **1-indexed** to match ``why()``/``cause()`` and the
        debugger: ``"3"`` for a main rung, ``"MySub:3"`` for a subroutine
        rung.
        """
        plc = self._plc
        idents: list[tuple[str | None, int]] = []
        ever_main = plc._rung_firing_timelines.ever_fired()
        idents.extend((None, i) for i in range(len(plc._logic)) if i not in ever_main)
        sub_universe = self._subroutine_rung_ids()
        if sub_universe:
            ever_sub = plc._node_firing_timelines.ever_fired()
            idents.extend(
                (rid.subroutine, rid.rung_index) for rid in sub_universe if rid not in ever_sub
            )
        idents.sort(key=_rung_sort_key)
        return [_rung_label(sub, idx) for sub, idx in idents]

    def hot_rungs(self) -> list[str]:
        """Rung labels that fired every scan across retained history.

        A rung is "hot" if it fired on every retained scan_id (excluding
        the initial scan, which predates any rung evaluation).  Covers main
        rungs (int firing log) and subroutine rungs (node firing log).

        Labels are **1-indexed**: ``"3"`` for a main rung, ``"MySub:3"``
        for a subroutine rung.
        """
        plc = self._plc
        initial_scan_id = plc._initial_scan_id
        scan_ids = [sid for sid in plc._history.scan_ids() if sid != initial_scan_id]
        if not scan_ids:
            return []
        hot_main = set(range(len(plc._logic)))
        hot_sub = self._subroutine_rung_ids()
        for scan_id in scan_ids:
            if hot_main:
                hot_main &= plc._rung_firing_timelines.fired_on(scan_id)
            if hot_sub:
                hot_sub &= plc._node_firing_timelines.fired_on(scan_id)
            if not hot_main and not hot_sub:
                break
        idents: list[tuple[str | None, int]] = [(None, i) for i in hot_main]
        idents.extend((rid.subroutine, rid.rung_index) for rid in hot_sub)
        idents.sort(key=_rung_sort_key)
        return [_rung_label(sub, idx) for sub, idx in idents]

    def stranded_bits(self) -> list[CausalChain]:
        """Persistent bits with no reachable clear path from current state.

        Returns a list of ``CausalChain`` objects with ``mode='unreachable'``,
        one per stranded bit.  The chains carry blocker information explaining
        *why* each bit is stranded.

        Only considers ``latch()``-written tags (see module docstring for
        limitations).
        """
        persistent = _persistent_bits(self._plc._logic)
        stranded: list[CausalChain] = []
        for tag in persistent:
            chain = self._plc.cause(tag, to=tag.default)
            if chain is not None and chain.mode == "unreachable":
                stranded.append(chain)
        return stranded

    def report(self) -> CoverageReport:
        """Emit a per-test coverage report for merge across a test suite."""
        return CoverageReport(
            cold_rungs=frozenset(self.cold_rungs()),
            hot_rungs=frozenset(self.hot_rungs()),
            stranded_chains=frozenset(_chain_identity(c) for c in self.stranded_bits()),
        )


# ---------------------------------------------------------------------------
# Coverage report & merge
# ---------------------------------------------------------------------------


def _chain_identity(chain: CausalChain) -> tuple[str, tuple[Any, ...]]:
    """Fingerprint a stranded chain by (effect tag, blocker signature).

    Two chains with the same identity are "stranded for the same reason."
    Different blocker signatures surface refactors that silently changed
    the recovery path.
    """
    effect_tag = chain.effect.tag_name
    blocker_sig = tuple(
        (b.rung_index, b.blocked_tag, b.needed_value, b.reason.value)
        for b in sorted(chain.blockers, key=lambda b: (b.rung_index, b.blocked_tag))
    )
    return (effect_tag, blocker_sig)


@dataclass(frozen=True)
class CoverageReport:
    """Aggregated coverage findings from one test (or merged across tests).

    Merge semantics:
    - **Negative findings** (cold_rungs, stranded_chains) merge by
      **intersection** — a rung is only cold in the suite if *no* test
      fired it.
    - **Positive findings** (hot_rungs) merge by **intersection** — a
      rung is only hot in the suite if *every* test shows it hot.

    Stranded chains merge by chain identity (effect tag + blocker
    fingerprint), so "stranded for a different reason" is a distinct
    CI signal from "still stranded."
    """

    cold_rungs: frozenset[str] = field(default_factory=frozenset)
    hot_rungs: frozenset[str] = field(default_factory=frozenset)
    stranded_chains: frozenset[tuple[str, tuple[Any, ...]]] = field(default_factory=frozenset)

    def merge(self, other: CoverageReport) -> CoverageReport:
        """Merge two reports (intersection for negative, intersection for hot)."""
        return CoverageReport(
            cold_rungs=self.cold_rungs & other.cold_rungs,
            hot_rungs=self.hot_rungs & other.hot_rungs,
            stranded_chains=self.stranded_chains & other.stranded_chains,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON output."""
        return {
            "cold_rungs": sorted(self.cold_rungs),
            "hot_rungs": sorted(self.hot_rungs),
            "stranded_chains": sorted(
                {"tag": tag, "blockers": list(blockers)} for tag, blockers in self.stranded_chains
            ),
        }
