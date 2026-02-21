"""Typed debug trace models used by PLCDebugger and DAP translation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

EnabledState = Literal["enabled", "disabled_local", "disabled_parent"]
ConditionStatus = Literal["true", "false", "skipped"]


@dataclass(frozen=True)
class SourceSpan:
    """Source location span."""

    source_file: str | None
    source_line: int | None
    end_line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "source_line": self.source_line,
            "end_line": self.end_line,
        }


@dataclass(frozen=True)
class ConditionTrace:
    """Condition evaluation details for debugger trace rendering."""

    source_file: str | None
    source_line: int | None
    expression: str
    status: ConditionStatus
    value: bool | None
    details: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    annotation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "source_line": self.source_line,
            "expression": self.expression,
            "status": self.status,
            "value": self.value,
            "details": list(self.details),
            "summary": self.summary,
            "annotation": self.annotation,
        }


@dataclass(frozen=True)
class TraceRegion:
    """One highlighted region (rung/branch/instruction) in a trace event."""

    kind: str
    source: SourceSpan
    enabled_state: EnabledState
    conditions: list[ConditionTrace] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = self.source.to_dict()
        payload.update(
            {
                "kind": self.kind,
                "enabled_state": self.enabled_state,
                "conditions": [condition.to_dict() for condition in self.conditions],
            }
        )
        return payload


@dataclass(frozen=True)
class TraceEvent:
    """Complete trace payload for a yielded debug step."""

    regions: list[TraceRegion] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"regions": [region.to_dict() for region in self.regions]}

