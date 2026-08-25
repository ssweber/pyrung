"""Immutable facts describing one observed loss of bearing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.navigation_contracts import _ActionPair


@dataclass(frozen=True)
class BearingDeparture:
    """One fact that held at the incident anchor and later departed."""

    tag: str
    value: Any
    scan: int | None


@dataclass(frozen=True)
class DeviationIncident:
    """The bounded window where verify observed a loss of bearing."""

    anchor_scan: int
    departure_scan: int | None
    end_scan: int
    action: tuple[_ActionPair, ...]
    bearing: tuple[_ActionPair, ...]
    before_snap: Mapping[str, Any]
    after_snap: Mapping[str, Any]
    # Complete factual movement set: every timeline transition plus every
    # before/after endpoint difference. Consumers filter it locally.
    changed_tags: tuple[str, ...]
    departures: tuple[BearingDeparture, ...]
    # The macro-state register whose departure IS the incident (the bearing /
    # terminal-letrun channel tag) — other departures downstream of it are
    # collateral. Hypothesis ranking keys causal primacy off its cause chain.
    channel_tag: str | None = None
    # The recorded session events inside the window (CoastTriggerEvents, ordered,
    # same-scan groups preserved). This is the incident's evidence: a
    # fire-then-reset pulse is two transitions here, never a net no-op.
    timeline: tuple[Any, ...] = ()
    # Exact conditions on the retained writer occurrence. Retained-prefix
    # recovery projects only the corrected direct conjuncts out of this tuple;
    # the remaining terms are the correction's executable lifetime.
    occurrence_conditions: tuple[Any, ...] = ()
