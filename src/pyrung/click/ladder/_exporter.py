"""Click ladder CSV export helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NoReturn

from pyrung.click._topology import (
    Leaf,
    Series,
    SPNode,
    as_series_children,
    factor_outputs,
    make_compound,
)
from pyrung.core.condition import AllCondition, AnyCondition, Condition, text_window_of
from pyrung.core.rung import Rung

from ._solve_order import MovedRead, ReadOrder
from .instructions import _InstructionMixin
from .layout import _HEADER, _LayoutMixin, assert_no_column_a_vertical
from .translator import _TranslatorMixin
from .types import (
    ExportSummary,
    LadderBundle,
    LadderExportError,
    LadderRungSource,
    LadderSourceSpan,
    _RenderError,
)
from .validator import _RoundTripValidationMixin, _ValidationMixin

if TYPE_CHECKING:
    from collections.abc import Collection

    from pyrung.click.tag_map import TagMap
    from pyrung.core.program import Program


@dataclass(frozen=True)
class _ConditionLeafKey:
    """SP-tree leaf label that preserves the original condition for rebuilding."""

    condition: Condition = field(compare=False)
    signature: str


# ---- Public entrypoint ----
def build_ladder_bundle(
    tag_map: TagMap,
    program: Program,
    *,
    index: bool = False,
    validate: bool = True,
    allow: Collection[str] = (),
) -> LadderBundle:
    """Render a `Program` into deterministic Click ladder CSV row matrices."""
    return _LadderExporter(tag_map=tag_map, program=program, index=index).export(
        validate=validate, allow=allow
    )


# ---- Orchestrator ----
class _LadderExporter(
    _RoundTripValidationMixin,
    _ValidationMixin,
    _LayoutMixin,
    _InstructionMixin,
    _TranslatorMixin,
):
    """Facade that orchestrates validation, layout, and token rendering."""

    # DSL name → CSV token name (only entries where names differ)
    _RENAME_TABLE: tuple[tuple[str, str, str], ...] = (
        # (instruction_class_name, dsl_name, csv_name)
        ("ReturnInstruction", "return_early", "return"),
        ("ForLoopInstruction", "forloop", "for"),
        ("BreakInstruction", "break_loop", "break"),
        ("ContinueInstruction", "continue_loop", "continue"),
    )

    def __init__(self, *, tag_map: TagMap, program: Program, index: bool = False) -> None:
        self._tag_map = tag_map
        self._program = program
        self._index = index
        self._marker_counter = 0
        self._forloop_count = 0
        self._added_return_count = 0

    def _next_marker(self) -> str:
        if self._index:
            self._marker_counter += 1
            return f"R{self._marker_counter}"
        return "R"

    def _reset_marker_counter(self) -> None:
        self._marker_counter = 0

    def export(self, *, validate: bool = True, allow: Collection[str] = ()) -> LadderBundle:
        try:
            if validate:
                self._run_precheck(allow)

            # Main scope always ends with an explicit end() rung.
            rendered_main_rows, main_sources = self._render_scope(
                self._program.rungs, scope="main", subroutine_name=None
            )
            if validate:
                self._validate_scope_roundtrip(
                    source_rungs=self._program.rungs,
                    rendered_rows=rendered_main_rows,
                    scope="main",
                    subroutine_name=None,
                )

            main_rows: list[tuple[str, ...]] = [tuple(_HEADER)]
            main_rows.extend(rendered_main_rows)
            main_rows.extend(self._end_rung())
            main_sources.append(LadderRungSource(rung=len(main_sources) + 1))

            # Each subroutine matrix gets a deterministic return() tail.
            subroutine_rows: list[tuple[str, tuple[tuple[str, ...], ...]]] = []
            subroutine_sources: list[tuple[str, tuple[LadderRungSource, ...]]] = []
            for subroutine_name in sorted(self._program.subroutines):
                rendered_sub_rows, sources = self._render_scope(
                    self._program.subroutines[subroutine_name],
                    scope="subroutine",
                    subroutine_name=subroutine_name,
                )
                if validate:
                    self._validate_scope_roundtrip(
                        source_rungs=self._program.subroutines[subroutine_name],
                        rendered_rows=rendered_sub_rows,
                        scope="subroutine",
                        subroutine_name=subroutine_name,
                    )

                rows: list[tuple[str, ...]] = [tuple(_HEADER)]
                rows.extend(rendered_sub_rows)
                rows = self._ensure_subroutine_return_tail(rows, subroutine_name=subroutine_name)
                while len(sources) < self._count_rendered_rungs(rows[1:]):
                    sources.append(LadderRungSource(rung=len(sources) + 1))
                subroutine_rows.append((subroutine_name, tuple(rows)))
                subroutine_sources.append((subroutine_name, tuple(sources)))

            summary = self._build_summary()
            return LadderBundle(
                main_rows=tuple(main_rows),
                subroutine_rows=tuple(subroutine_rows),
                export_summary=summary,
                main_sources=tuple(main_sources),
                subroutine_sources=tuple(subroutine_sources),
            )
        except _RenderError as exc:
            raise LadderExportError([exc.issue]) from None

    def _render_scope(
        self,
        rungs: list[Rung],
        *,
        scope: str,
        subroutine_name: str | None,
    ) -> tuple[list[tuple[str, ...]], list[LadderRungSource]]:
        self._reset_marker_counter()
        rows: list[tuple[str, ...]] = []
        sources: list[LadderRungSource] = []
        for rung_index, rung in enumerate(rungs):
            base_path = (
                f"subroutine[{subroutine_name}].rung[{rung_index}]"
                if scope == "subroutine"
                else f"main.rung[{rung_index}]"
            )
            self._check_nop_alone(rung, path=base_path)
            rendered = self._render_source_rung(rung, path=base_path)
            try:
                assert_no_column_a_vertical(rendered)
            except ValueError as exc:
                self._raise_issue(path=base_path, message=str(exc), source=rung)
            rows.extend(rendered)
            source = LadderSourceSpan(
                source_file=rung.source_file,
                source_line=rung.source_line,
                end_line=rung.end_line,
            )
            marker_count = self._count_rendered_rungs(rendered)
            if marker_count:
                for _ in range(marker_count):
                    sources.append(LadderRungSource(rung=len(sources) + 1, sources=(source,)))
            elif sources:
                # A rung of empty branches draws nothing, so its lines join the CLICK rung above.
                prior = sources[-1]
                sources[-1] = LadderRungSource(
                    rung=prior.rung,
                    sources=(*prior.sources, source),
                )
        return rows, sources

    @staticmethod
    def _count_rendered_rungs(rows: list[tuple[str, ...]]) -> int:
        return sum(bool(row and row[0].startswith("R")) for row in rows)

    @staticmethod
    def _comment_rows(rung: Rung) -> list[tuple[str, ...]]:
        """Expand rung comments into '#' passthrough rows."""
        if rung.comment is None:
            return []
        return [("#", line) for line in rung.comment.splitlines()]

    def _normalize_branching_rung(self, rung: Rung, *, path: str | None = None) -> Rung:
        """Normalize a branching rung into an ordered shared-prefix tree.

        A contact is factored into a shared node only where that does not read
        it across a write of the rung (see :mod:`._solve_order`).  With *path*
        (export), the rung is refused when its drawing would read a contact at
        another point of the solve.
        """
        outputs = self._collect_instruction_trees(rung)
        if not outputs:
            return rung  # branches that hold no output draw no rows
        order = ReadOrder(self._program, rung, [instr for _tree, instr in outputs])
        normalized = self._build_normalized_rung_from_trees(
            outputs, order=order, members=[(k, 0) for k in range(len(outputs))]
        )
        normalized._use_prior_snapshot = rung._use_prior_snapshot
        normalized.comment = rung.comment
        normalized.source_file = rung.source_file
        normalized.source_line = rung.source_line
        normalized.end_line = rung.end_line
        if path is not None:
            moved = order.check(normalized)
            if moved is not None:
                self._refuse_moved_read(moved, outputs, path=path, source=rung)
        return normalized

    def _series_tree(self, *nodes: SPNode | None) -> SPNode | None:
        children = [node for node in nodes if node is not None]
        if not children:
            return None
        return make_compound(children, Series)

    def _local_condition_tree(self, rung: Rung) -> SPNode | None:
        local_conditions = rung._conditions[rung._branch_condition_start :]
        if not local_conditions:
            return None
        leaves = [
            Leaf(
                _ConditionLeafKey(
                    condition=condition,
                    signature=self._condition_signature(condition),
                )
            )
            for condition in local_conditions
        ]
        return make_compound(leaves, Series)

    def _collect_instruction_trees(
        self,
        rung: Rung,
        *,
        prefix_tree: SPNode | None = None,
    ) -> list[tuple[SPNode | None, Any]]:
        current_tree = self._series_tree(prefix_tree, self._local_condition_tree(rung))

        outputs: list[tuple[SPNode | None, Any]] = []
        for item in rung._execution_items:
            if isinstance(item, Rung):
                outputs.extend(self._collect_instruction_trees(item, prefix_tree=current_tree))
            else:
                outputs.append((current_tree, item))
        return outputs

    @staticmethod
    def _leaf_condition(node: SPNode) -> Condition:
        if not isinstance(node, Leaf) or not isinstance(node.label, _ConditionLeafKey):
            raise TypeError(f"Expected condition leaf, got {node!r}")
        return node.label.condition

    @staticmethod
    def _shared_count(
        trees: list[SPNode | None],
        members: list[tuple[int, int]] | None,
        order: ReadOrder | None,
    ) -> int:
        """How many leading contacts *trees* share and may read as one node.

        The node is read at the first member's position, so a contact another
        member reads later in the source stays out of it when a write in
        between reaches it.
        """
        shared = len(factor_outputs(trees).shared)
        if order is None or members is None or not shared:
            return shared
        at = members[0][0]
        for index in range(shared):
            for output, offset in members:
                if order.crossing(output, offset + index, at) is not None:
                    return index
        return shared

    def _build_normalized_rung_from_trees(
        self,
        outputs: list[tuple[SPNode | None, Any]],
        *,
        order: ReadOrder | None = None,
        members: list[tuple[int, int]] | None = None,
    ) -> Rung:
        """One rung for *outputs* (each with its whole path), prefixes factored.

        *members* gives each output's ``(position in the source rung, contacts
        of its path already placed above)``, so *order* can keep a contact
        from being read across a write.
        """
        trees = [tree for tree, _instruction in outputs]
        result = factor_outputs(trees)
        count = self._shared_count(trees, members, order)
        if not count and len(outputs) == 1:
            # A lone output reads its path where it stands; ReadOrder.check
            # refuses the rung if that crosses a write.
            count = len(result.shared)
        shared = result.shared[:count]
        normalized = Rung(*[self._leaf_condition(node) for node in shared])

        remaining_outputs = [
            (
                self._series_tree(*as_series_children(tree)[count:]),
                instruction,
            )
            for tree, instruction in outputs
        ]
        remaining_members = (
            None if members is None else [(output, offset + count) for output, offset in members]
        )

        index = 0
        while index < len(remaining_outputs):
            tree, instruction = remaining_outputs[index]
            if tree is None:
                normalized.add_instruction(instruction)
                index += 1
                continue

            stop = index + 1
            while stop < len(remaining_outputs):
                candidate_tree, _candidate_instruction = remaining_outputs[stop]
                if candidate_tree is None:
                    break
                group_members = (
                    None if remaining_members is None else remaining_members[index : stop + 1]
                )
                if not self._shared_count(
                    [candidate for candidate, _item in remaining_outputs[index : stop + 1]],
                    group_members,
                    order,
                ):
                    break
                stop += 1

            normalized.add_branch(
                self._build_normalized_rung_from_trees(
                    remaining_outputs[index:stop],
                    order=order,
                    members=None if remaining_members is None else remaining_members[index:stop],
                )
            )
            index = stop

        return normalized

    def _condition_signature(self, condition: Condition) -> str:
        condition = text_window_of(condition)
        if isinstance(condition, AllCondition):
            parts = ",".join(self._condition_signature(child) for child in condition.conditions)
            return f"AND({parts})"
        if isinstance(condition, AnyCondition):
            parts = ",".join(self._condition_signature(child) for child in condition.conditions)
            return f"OR({parts})"
        return self._condition_leaf_token(condition, path="normalize.condition")

    def _check_nop_alone(self, rung: Rung, *, path: str) -> None:
        """Refuse a contact in front of NOP, which CLICK refuses (C0003, no coil).

        CLICK also deletes a NOP row that shares its rung with another row.  The export draws an
        empty rung as a CLICK rung of its own, so that cannot happen.
        """
        if rung._instructions or rung._branches:
            return
        if rung._conditions:
            self._raise_issue(
                path=f"{path}.condition",
                message=(
                    "CLICK refuses an empty rung with conditions: NOP must sit on a "
                    "bare wire. Drop the conditions (they do nothing without an "
                    "instruction) or give the rung an instruction."
                ),
                source=rung,
            )

    def _export_rung(self, rung: Rung, *, path: str) -> Rung:
        """*rung* as its CLICK rung: a branching rung's leading contacts factored.

        A contact is factored into a shared node only where that does not read
        it across a write of the rung; the rung is refused when its drawing
        would read a contact at another point of the solve (see
        :mod:`._solve_order`).  A box with pins is alone on its rung (the
        program build refuses anything beside it), so nothing here splits.
        """
        if not rung._branches:
            return rung
        if any(type(instr).__name__ == "ForLoopInstruction" for instr in rung._instructions):
            return rung
        return self._normalize_branching_rung(rung, path=path)

    def _refuse_moved_read(
        self,
        moved: MovedRead,
        outputs: list[tuple[SPNode | None, Any]],
        *,
        path: str,
        source: Rung,
    ) -> NoReturn:
        contact = self._condition_signature(moved.contact)
        writer = type(outputs[moved.writer][1]).__name__
        tag = moved.tag
        try:
            address = self._tag_map.resolve(tag)
        except (KeyError, TypeError, ValueError):
            address = tag
        if address != tag:
            tag = f"{tag} ({address})"
        message = (
            f"Contact {contact} cannot be drawn on the row that reads it here: {writer} on "
            f"this rung writes {tag} between the two. To export it, move {writer} to a "
            "rung of its own, or give the rows their own contacts."
        )
        self._raise_issue(path=path, message=message, source=source)

    def _render_source_rung(self, rung: Rung, *, path: str) -> list[tuple[str, ...]]:
        """Render a program rung as its CLICK rung (:meth:`_export_rung`)."""
        return self._render_rung(self._export_rung(rung, path=path), path=path)

    def _render_rung(self, rung: Rung, *, path: str) -> list[tuple[str, ...]]:
        """Render one CLICK rung, already factored (:meth:`_export_rung`)."""
        comment_rows = self._comment_rows(rung)

        if not rung._instructions and not rung._branches:
            # Empty rung (comment-only or bare pass) → emit NOP in AF column.
            output_rows = self._render_network(
                rung._conditions, ["NOP"], path=path, source=rung, first_marker=self._next_marker()
            )
            self._assert_rung_height(rows=output_rows, path=path, source=rung)
            return comment_rows + output_rows

        if any(
            type(instruction).__name__ == "ForLoopInstruction" for instruction in rung._instructions
        ):
            if len(rung._instructions) != 1 or rung._branches:
                self._raise_issue(
                    path=f"{path}.instruction",
                    message=(
                        "Rungs that contain forloop() cannot include additional instructions "
                        "or branch(...) blocks "
                        "in Click ladder v1 export."
                    ),
                    source=rung,
                )
            self._forloop_count += 1
            output_rows = self._render_forloop_instruction(
                instruction=rung._instructions[0],
                conditions=rung._conditions,
                path=f"{path}.instruction[0](ForLoopInstruction)",
            )
            self._assert_rung_height(rows=output_rows, path=path, source=rung)
            return comment_rows + output_rows

        rows = self._render_rung_items(rung, path=path)
        if not rows:
            return []
        return comment_rows + rows

    def _ensure_subroutine_return_tail(
        self,
        rows: list[tuple[str, ...]],
        *,
        subroutine_name: str,
    ) -> list[tuple[str, ...]]:
        # CLICK needs the last rung to be an unconditional RET; a conditional
        # return() ending the subroutine still gets a tail.
        last = rows[-1]
        if len(rows) > 1 and last[0].startswith("R") and last[-1] == "return()":
            if all(cell == "-" for cell in last[1:-1]):
                return rows

        self._added_return_count += 1
        return_rows = self._render_network(
            [],
            [self._fn("return")],
            path=f"subroutine[{subroutine_name}].return",
            source=None,
            first_marker=self._next_marker(),
        )
        rows.extend(return_rows)
        return rows

    def _end_rung(self) -> list[tuple[str, ...]]:
        """Emit an unconditional ``end()`` rung for the main program tail."""
        return self._render_network(
            [], [self._fn("end")], path="main.end", source=None, first_marker=self._next_marker()
        )

    def _build_summary(self) -> ExportSummary:
        used_types = self._collect_instruction_types(self._program)
        renames: list[tuple[str, str]] = []
        for class_name, dsl_name, csv_name in self._RENAME_TABLE:
            if class_name in used_types:
                renames.append((dsl_name, csv_name))
        return ExportSummary(
            renames=tuple(renames),
            added_next=self._forloop_count,
            added_return=self._added_return_count,
            added_end=True,
        )

    @staticmethod
    def _collect_instruction_types(program: Program) -> set[str]:
        """Collect all instruction class names used across the program."""
        types: set[str] = set()
        all_rungs = list(program.rungs)
        for sub_rungs in program.subroutines.values():
            all_rungs.extend(sub_rungs)

        def _add(instructions: Any) -> None:
            for instr in instructions:
                types.add(type(instr).__name__)
                # ForLoopInstruction has nested instructions, nested loops included
                _add(getattr(instr, "instructions", ()))

        for rung in all_rungs:
            _add(rung._instructions)
            for branch_block in rung._branches:
                _add(branch_block._instructions)
        return types

    def _raise_issue(self, *, path: str, message: str, source: Any) -> NoReturn:
        source_file = getattr(source, "source_file", None) if source is not None else None
        source_line = getattr(source, "source_line", None) if source is not None else None
        raise _RenderError(
            {
                "path": path,
                "message": message,
                "source_file": source_file,
                "source_line": source_line,
            }
        )

    def _assert_rung_height(
        self,
        *,
        rows: list[tuple[str, ...]],
        path: str,
        source: Any,
    ) -> None:
        if len(rows) <= 32:
            return
        self._raise_issue(
            path=path,
            message="Rendered rung exceeds Click's 32-row limit.",
            source=source,
        )


__all__ = ["LadderBundle", "LadderExportError", "build_ladder_bundle"]
