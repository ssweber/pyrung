from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.sp_tree import SPNode, evaluate_sp

# Re-exported from its neutral home in sp_values (kept here for existing callers).
from pyrung.core.analysis.sp_values import _condition_tag_name as _condition_tag_name

if TYPE_CHECKING:
    from pyrung.core.condition import Condition


def _collect_sp_leaves(node: SPNode) -> list[Any]:
    """Collect all ``SPLeaf`` conditions from an SP tree, regardless of evaluation."""
    from pyrung.core.analysis.sp_tree import SPLeaf, SPParallel, SPSeries

    if isinstance(node, SPLeaf):
        return [node]
    result: list[SPLeaf] = []
    if isinstance(node, (SPSeries, SPParallel)):
        for child in node.children:
            result.extend(_collect_sp_leaves(child))
    return result


# ---------------------------------------------------------------------------
# Helpers for evaluating conditions against historical state
# ---------------------------------------------------------------------------


class _HistoricalView:
    """Duck-typed evaluator for conditions against a historical SystemState.

    Conditions call ``ctx.get_tag()`` and ``ctx.get_memory()``.  This provides
    those methods backed by a frozen SystemState snapshot.
    """

    __slots__ = ("_state",)

    def __init__(self, state: Any) -> None:
        self._state = state

    def get_tag(self, name: str, default: Any = None) -> Any:
        val = self._state.tags.get(name)
        return val if val is not None else default

    def get_memory(self, key: str, default: Any = None) -> Any:
        val = self._state.memory.get(key)
        return val if val is not None else default


class _TimelineView:
    """Evaluator that resolves tag values from timelines, no state replay.

    Drop-in replacement for ``_HistoricalView`` when no cached state is
    available.  Each ``get_tag`` call does an O(log S) bisect through the
    firing timelines (for writer tags) or ``ScanLog``'s derived input
    index (for writerless tags).
    """

    __slots__ = ("_scan_id", "_timelines", "_pdg", "_scan_log", "_initial_tags")

    def __init__(
        self,
        scan_id: int,
        *,
        timelines: Any,
        pdg: Any,
        scan_log: Any,
        initial_tags: Any,
    ) -> None:
        self._scan_id = scan_id
        self._timelines = timelines
        self._pdg = pdg
        self._scan_log = scan_log
        self._initial_tags = initial_tags

    def get_tag(self, name: str, default: Any = None) -> Any:
        from .history import resolve_tag_at_scan

        val = resolve_tag_at_scan(
            name,
            self._scan_id,
            timelines=self._timelines,
            pdg=self._pdg,
            scan_log=self._scan_log,
            initial_tags=self._initial_tags,
        )
        return val if val is not None else default

    def get_memory(self, key: str, default: Any = None) -> Any:
        return default


# ---------------------------------------------------------------------------
# History walking helpers
# ---------------------------------------------------------------------------


class _CounterfactualView:
    """Historical view with one tag's value overridden for counterfactual checks.

    Used by the forward walk to answer: "would this rung have evaluated
    the same way if tag X had not transitioned?"
    """

    __slots__ = ("_state", "_override_tag", "_override_value")

    def __init__(self, state: Any, override_tag: str, override_value: Any) -> None:
        self._state = state
        self._override_tag = override_tag
        self._override_value = override_value

    def get_tag(self, name: str, default: Any = None) -> Any:
        if name == self._override_tag:
            return self._override_value if self._override_value is not None else default
        val = self._state.tags.get(name)
        return val if val is not None else default

    def get_memory(self, key: str, default: Any = None) -> Any:
        val = self._state.memory.get(key)
        return val if val is not None else default


def _counterfactual_changes_outcome(
    sp_tree: SPNode,
    state: Any,
    cause_tag: str,
    from_value: Any,
) -> bool:
    """Check if reverting *cause_tag* to *from_value* changes the SP tree outcome.

    Evaluates the tree twice — once with the actual state, once with
    the tag reverted — and returns True if the results differ.
    """
    actual_view = _HistoricalView(state)
    cf_view = _CounterfactualView(state, cause_tag, from_value)

    def _eval_actual(cond: Condition, _v: Any = actual_view) -> bool:
        return cond.evaluate(_v)  # type: ignore[arg-type]

    def _eval_cf(cond: Condition, _v: Any = cf_view) -> bool:
        return cond.evaluate(_v)  # type: ignore[arg-type]

    actual_result = evaluate_sp(sp_tree, _eval_actual)
    cf_result = evaluate_sp(sp_tree, _eval_cf)
    return actual_result != cf_result


# ---------------------------------------------------------------------------
# Recorded forward walk
# ---------------------------------------------------------------------------
