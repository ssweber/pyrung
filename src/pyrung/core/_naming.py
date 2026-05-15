"""Tag/Block name inference from assignment targets via the ``executing`` library."""

from __future__ import annotations

import warnings
from types import FrameType


class PyrungNameError(ValueError):
    """Tag/Block name could not be inferred and none was provided."""


class PyrungNameWarning(UserWarning):
    """Explicit tag name doesn't match the inferred variable name."""


def _infer_assignment_name(frame: FrameType) -> str | None:
    """Return the variable/attribute name from the assignment that invoked
    the constructor in *frame*, or ``None`` if it can't be determined.

    Handles ``Name = Bool()``, ``Name: Bool = Bool()``, and
    ``obj.attr = Bool()``.
    """
    try:
        import ast

        from executing import Source

        src = Source.for_frame(frame)
        node = src.executing(frame).node
        if node is None:
            return None

        parent_map: dict[int, ast.AST] | None = getattr(src.tree, "_pyrung_parent_map", None)
        if parent_map is None:
            parent_map = {}
            for n in ast.walk(src.tree):
                for child in ast.iter_child_nodes(n):
                    parent_map[id(child)] = n
            src.tree._pyrung_parent_map = parent_map  # type: ignore[attr-defined]

        parent = parent_map.get(id(node))
        if isinstance(parent, ast.Assign) and len(parent.targets) == 1:
            t = parent.targets[0]
            if isinstance(t, ast.Name):
                return t.id
            if isinstance(t, ast.Attribute):
                return t.attr
        if isinstance(parent, ast.AnnAssign):
            if isinstance(parent.target, ast.Name):
                return parent.target.id
            if isinstance(parent.target, ast.Attribute):
                return parent.target.attr
        return None
    except Exception:
        return None


def _resolve_name(
    cls_name: str,
    explicit_name: str | None,
    frame: FrameType,
    *,
    stacklevel: int = 2,
) -> str:
    """Resolve the tag/block name from an explicit string and/or inference.

    Returns the resolved name.  Raises `PyrungNameError` when neither an
    explicit name nor inference succeeds.  Emits `PyrungNameWarning` when
    both are present and disagree (explicit wins).
    """
    inferred = _infer_assignment_name(frame)
    if explicit_name is None:
        if inferred is None:
            raise PyrungNameError(
                f"{cls_name}() requires a name. Assign to a variable "
                f"(e.g. `Foo = {cls_name}()`) or pass an explicit name."
            )
        return inferred
    if inferred is not None and inferred != explicit_name:
        warnings.warn(
            f"Tag name {explicit_name!r} doesn't match variable {inferred!r}",
            PyrungNameWarning,
            stacklevel=stacklevel,
        )
    return explicit_name
