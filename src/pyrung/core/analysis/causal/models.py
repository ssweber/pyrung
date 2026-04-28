from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


@dataclass(frozen=True)
class Transition:
    """A tag value change at a specific scan."""

    tag_name: str
    scan_id: int
    from_value: Any
    to_value: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag_name,
            "scan": self.scan_id,
            "from": self.from_value,
            "to": self.to_value,
        }


@dataclass(frozen=True)
class EnablingCondition:
    """A contact that held the path open but didn't transition."""

    tag_name: str
    value: Any
    held_since_scan: int | None  # None if never changed in retained history

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag_name,
            "value": self.value,
            "held_since_scan": self.held_since_scan,
        }


class BlockerReason(Enum):
    """Why a projected path is unreachable."""

    NO_OBSERVED_TRANSITION = "NO_OBSERVED_TRANSITION"
    BLOCKED_UPSTREAM = "BLOCKED_UPSTREAM"
    STRUCTURAL_CONTRADICTION = "STRUCTURAL_CONTRADICTION"


@dataclass(frozen=True)
class BlockingCondition:
    """A contact that would need to transition but can't be reached.

    Populated when ``CausalChain.mode == 'unreachable'``.
    """

    rung_index: int
    blocked_tag: str
    needed_value: Any
    reason: BlockerReason
    sub_blockers: tuple[BlockingCondition, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "rung_index": self.rung_index,
            "blocked_tag": self.blocked_tag,
            "needed_value": self.needed_value,
            "reason": self.reason.value,
        }
        if self.sub_blockers:
            d["sub_blockers"] = [b.to_dict() for b in self.sub_blockers]
        return d


@dataclass(frozen=True)
class ChainStep:
    """One causal link: a rung fired and wrote a tag.

    ``transition`` is the tag change produced by this rung.
    ``proximate_causes`` are inputs that transitioned (what flipped the rung).
    ``enabling_conditions`` are inputs that held steady (required but didn't change).
    ``fidelity`` is ``"full"`` when SP-tree attribution was used (state
    was cached), or ``"timeline"`` when only structural + timeline
    data was available (cache miss — ``enabling_conditions`` will be
    empty and ``proximate_causes`` is a superset of the true set).
    """

    transition: Transition
    rung_index: int
    proximate_causes: tuple[Transition, ...]
    enabling_conditions: tuple[EnablingCondition, ...]
    fidelity: Literal["full", "timeline"] = "full"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "transition": self.transition.to_dict(),
            "rung_index": self.rung_index,
            "proximate_causes": [t.to_dict() for t in self.proximate_causes],
            "enabling_conditions": [e.to_dict() for e in self.enabling_conditions],
        }
        if self.fidelity != "full":
            d["fidelity"] = self.fidelity
        return d


@dataclass
class CausalChain:
    """Result of causal chain analysis.

    ``effect`` is the transition being explained (or projected/shown unreachable).
    ``mode`` is ``'recorded'``, ``'projected'``, or ``'unreachable'``.
    ``steps`` are ordered from effect backward toward root causes.
    ``conjunctive_roots`` are root inputs that fired together (AND — joint causation).
    ``ambiguous_roots`` are root inputs we can't disambiguate (OR — genuine uncertainty).
    ``blockers`` are populated when ``mode == 'unreachable'`` — the contacts
    that would need to transition but can't be reached.
    ``confidence`` is 1.0 when unambiguous; ``1 / len(ambiguous_roots)`` otherwise.
    """

    effect: Transition
    mode: Literal["recorded", "projected", "unreachable"]
    steps: list[ChainStep] = field(default_factory=list)
    conjunctive_roots: list[Transition] = field(default_factory=list)
    ambiguous_roots: list[Transition] = field(default_factory=list)
    blockers: list[BlockingCondition] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        if not self.ambiguous_roots:
            return 1.0
        return 1.0 / len(self.ambiguous_roots)

    @property
    def duration_scans(self) -> int:
        if not self.steps:
            return 0
        scan_ids = [s.transition.scan_id for s in self.steps]
        all_scans = scan_ids + [self.effect.scan_id]
        return max(all_scans) - min(all_scans)

    def tags(self) -> list[str]:
        """All unique tag names appearing in the chain."""
        seen: set[str] = set()
        result: list[str] = []

        def _add(name: str) -> None:
            if name not in seen:
                seen.add(name)
                result.append(name)

        _add(self.effect.tag_name)
        for step in self.steps:
            _add(step.transition.tag_name)
            for pc in step.proximate_causes:
                _add(pc.tag_name)
            for ec in step.enabling_conditions:
                _add(ec.tag_name)
        for t in self.conjunctive_roots:
            _add(t.tag_name)
        for t in self.ambiguous_roots:
            _add(t.tag_name)
        return result

    def rungs(self) -> list[int]:
        """Unique rung indices in chain order."""
        seen: set[int] = set()
        result: list[int] = []
        for step in self.steps:
            if step.rung_index not in seen:
                seen.add(step.rung_index)
                result.append(step.rung_index)
        return result

    def to_dict(self) -> dict[str, Any]:
        """Rich serialization for UI / LLM consumption."""
        d: dict[str, Any] = {
            "effect": self.effect.to_dict(),
            "mode": self.mode,
            "steps": [s.to_dict() for s in self.steps],
            "conjunctive_roots": [t.to_dict() for t in self.conjunctive_roots],
            "ambiguous_roots": [t.to_dict() for t in self.ambiguous_roots],
            "confidence": self.confidence,
            "duration_scans": self.duration_scans,
        }
        if self.blockers:
            d["blockers"] = [b.to_dict() for b in self.blockers]
        return d

    def to_config(self) -> dict[str, Any]:
        """Round-trippable compact serialization for DAP / presets."""
        steps: list[dict[str, Any]] = []
        for s in self.steps:
            entry: dict[str, Any] = {
                "tag": s.transition.tag_name,
                "scan": s.transition.scan_id,
                "rung": s.rung_index,
            }
            if s.fidelity != "full":
                entry["fidelity"] = s.fidelity
            steps.append(entry)
        return {
            "effect": self.effect.tag_name,
            "scan": self.effect.scan_id,
            "mode": self.mode,
            "steps": steps,
            "confidence": self.confidence,
        }

    def __str__(self) -> str:
        """Human-readable chain report."""
        e = self.effect
        lines: list[str] = []

        if self.mode == "unreachable":
            lines.append(f"{e.tag_name} → {e.to_value!r}  [unreachable]")
            for b in self.blockers:
                lines.append(
                    f"  Rung {b.rung_index} would clear, but {b.blocked_tag} is unreachable"
                )
                lines.append(f"    reason: {b.reason.value}")
            return "\n".join(lines)

        mode_label = self.mode
        if self.mode == "projected":
            lines.append(f"{e.tag_name} → {e.to_value!r}  [{mode_label}]")
        else:
            lines.append(
                f"{e.tag_name} {e.from_value!r}→{e.to_value!r} at scan {e.scan_id}  [{mode_label}]"
            )

        for step in self.steps:
            t = step.transition
            fidelity_note = ""
            if step.fidelity == "timeline":
                fidelity_note = "  (partial; re-run with scan_id for full fidelity)"
            lines.append(f"  Rung {step.rung_index}: {t.tag_name} → {t.to_value!r}{fidelity_note}")
            for pc in step.proximate_causes:
                lines.append(f"    proximate: {pc.tag_name} {pc.from_value!r}→{pc.to_value!r}")
            if step.fidelity == "full":
                for ec in step.enabling_conditions:
                    lines.append(f"    enabling:  {ec.tag_name} = {ec.value!r}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# SP tree leaf collection (for projected walks)
# ---------------------------------------------------------------------------
