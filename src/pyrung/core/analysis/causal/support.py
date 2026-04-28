from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.sp_tree import SPNode, evaluate_sp

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


def _condition_tag_name(condition: Condition) -> str | None:
    """Extract the primary tag name from a leaf condition, or None."""
    tag = getattr(condition, "tag", None)
    if tag is None:
        return None
    # Handle ImmediateRef wrapping (check class name to avoid triggering
    # Tag.value property which requires an active runner)
    from pyrung.core.tag import ImmediateRef

    if isinstance(tag, ImmediateRef):
        inner = object.__getattribute__(tag, "value")
        return getattr(inner, "name", None)
    return getattr(tag, "name", None)


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
