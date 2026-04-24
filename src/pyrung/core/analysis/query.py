"""Whole-program dynamic analysis surveys.

``QueryNamespace`` is exposed as ``plc.query`` and provides survey methods
that aggregate dynamic history across retained scans:

- ``cold_rungs()`` — rungs that never fired
- ``hot_rungs()`` — rungs that fired every scan
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


class QueryNamespace:
    """Survey namespace for whole-program dynamic analysis.

    Accessed via ``plc.query``.  Methods aggregate findings across all
    retained history scans.
    """

    def __init__(self, plc: PLC) -> None:
        self._plc = plc

    def cold_rungs(self) -> list[int]:
        """Rung indices that never fired across retained history.

        Backed by :class:`RungFiringTimelines` — a rung with no
        timeline (or an empty timeline) is cold.
        """
        plc = self._plc
        total_rungs = set(range(len(plc._logic)))
        ever_fired = plc._rung_firing_timelines.ever_fired()
        return sorted(total_rungs - ever_fired)

    def hot_rungs(self) -> list[int]:
        """Rung indices that fired every scan across retained history.

        A rung is "hot" if :meth:`RungFiringTimelines.fired_on` returns
        True for every retained scan_id (excluding the initial scan,
        which predates any rung evaluation).
        """
        plc = self._plc
        initial_scan_id = plc._initial_scan_id
        scan_ids = [sid for sid in plc._history.scan_ids() if sid != initial_scan_id]
        if not scan_ids:
            return []
        hot = set(range(len(plc._logic)))
        for scan_id in scan_ids:
            hot &= plc._rung_firing_timelines.fired_on(scan_id)
            if not hot:
                break
        return sorted(hot)

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

    cold_rungs: frozenset[int] = field(default_factory=frozenset)
    hot_rungs: frozenset[int] = field(default_factory=frozenset)
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
