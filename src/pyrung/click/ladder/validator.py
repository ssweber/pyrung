"""Strict prevalidation helpers for Click ladder export."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn

from pyrung.click._topology import Leaf, Parallel, Series, SPNode, make_compound, trees_equal
from pyrung.click.codegen.analyzer import _analyze_rungs
from pyrung.click.codegen.constants import _PIN_RE
from pyrung.click.codegen.models import _AnalyzedRung, _PinInfo
from pyrung.click.codegen.parser import _parse_rows
from pyrung.core.condition import AllCondition, AnyCondition, Condition
from pyrung.core.instruction import ForLoopInstruction
from pyrung.core.rung import Rung

from .layout import _HEADER
from .types import Issue, LadderExportError

if TYPE_CHECKING:
    from pyrung.click.tag_map import TagMap
    from pyrung.core.program import Program


@dataclass(frozen=True)
class _ScopeOutput:
    af_token: str
    condition_tree: SPNode | None
    pins: tuple[_PinInfo, ...]


# ---- Strict prevalidation mixin ----
class _ValidationMixin:
    """Run strict checks before lowering ladder logic into Click CSV rows."""

    _tag_map: TagMap
    _program: Program

    if TYPE_CHECKING:

        def _raise_issue(self, *, path: str, message: str, source: Any) -> NoReturn: ...

    def _run_precheck(self) -> None:
        report = self._tag_map.validate(self._program, mode="strict")
        # We intentionally fail on warnings and hints to keep export deterministic.
        findings = [*report.errors, *report.warnings, *report.hints]
        if findings:
            issues: list[Issue] = []
            for finding in findings:
                issues.append(
                    {
                        "path": finding.location,
                        "message": f"{finding.code}: {finding.message}",
                        "source_file": None,
                        "source_line": None,
                    }
                )
            raise LadderExportError(issues)

        # Click ladder v1 does not support nested call instructions.
        for subroutine_name in sorted(self._program.subroutines):
            for rung_index, rung in enumerate(self._program.subroutines[subroutine_name]):
                self._assert_no_nested_subroutine_calls(
                    rung,
                    path=f"subroutine[{subroutine_name}].rung[{rung_index}]",
                )

    def _assert_no_nested_subroutine_calls(self, rung: Rung, *, path: str) -> None:
        for instruction_index, instruction in enumerate(rung._instructions):
            instruction_path = (
                f"{path}.instruction[{instruction_index}]({type(instruction).__name__})"
            )
            self._assert_no_nested_subroutine_calls_in_instruction(
                instruction,
                path=instruction_path,
            )

        for branch_index, branch in enumerate(rung._branches):
            self._assert_no_nested_subroutine_calls(
                branch,
                path=f"{path}.branch[{branch_index}]",
            )

    def _assert_no_nested_subroutine_calls_in_instruction(
        self,
        instruction: Any,
        *,
        path: str,
    ) -> None:
        if type(instruction).__name__ == "CallInstruction":
            self._raise_issue(
                path=path,
                message="Nested subroutine calls are not supported for Click export.",
                source=instruction,
            )

        if type(instruction).__name__ != "ForLoopInstruction":
            return

        for child_index, child_instruction in enumerate(getattr(instruction, "instructions", ())):
            child_path = f"{path}.instruction[{child_index}]({type(child_instruction).__name__})"
            self._assert_no_nested_subroutine_calls_in_instruction(
                child_instruction,
                path=child_path,
            )


class _RoundTripValidationMixin:
    """Round-trip emitted ladder rows back through the analyzer and compare semantics."""

    if TYPE_CHECKING:
        _program: Program
        _tag_map: TagMap

        def _raise_issue(self, *, path: str, message: str, source: Any) -> NoReturn: ...
        def _fn(self, name: str, *args: str, **kwargs: str) -> str: ...
        def _render_operand(
            self,
            value: Any,
            *,
            path: str,
            source: Any,
            allow_immediate: bool = False,
            immediate_context: str = "",
        ) -> str: ...
        def _pin_specs(
            self, instruction: Any, *, path: str
        ) -> list[tuple[str, Condition, str]]: ...
        def _instruction_token(self, instruction: Any, *, path: str) -> str: ...
        def _condition_leaf_token(self, condition: Condition, *, path: str) -> str: ...
        def _normalize_branching_rung(self, rung: Rung) -> Rung: ...
        def _series_tree(self, *nodes: SPNode | None) -> SPNode | None: ...

    def _validate_scope_roundtrip(
        self,
        *,
        source_rungs: list[Rung],
        rendered_rows: list[tuple[str, ...]],
        scope: str,
        subroutine_name: str | None,
    ) -> None:
        """Fail loudly when emitted CSV rows lose rung semantics."""
        actual_rungs = _analyze_rungs(_parse_rows([tuple(_HEADER), *rendered_rows]))
        expected_comments = [rung.comment for rung in source_rungs if rung.comment is not None]
        actual_comments = [rung.comment for rung in actual_rungs if rung.comment is not None]

        if actual_comments != expected_comments:
            scope_path = f"subroutine[{subroutine_name}]" if scope == "subroutine" else "main"
            self._raise_issue(
                path=scope_path,
                message=(
                    "CSV round-trip validation failed: "
                    f"comment sequence mismatch: expected {expected_comments!r}, got {actual_comments!r}"
                ),
                source=source_rungs[0] if source_rungs else None,
            )

        expected = self._expected_scope_outputs(
            source_rungs,
            scope=scope,
            subroutine_name=subroutine_name,
        )
        actual = self._flatten_analyzed_outputs(actual_rungs)

        if len(actual) != len(expected):
            scope_path = f"subroutine[{subroutine_name}]" if scope == "subroutine" else "main"
            source = (
                source_rungs[min(len(source_rungs) - 1, len(expected) - 1)]
                if source_rungs
                else None
            )
            self._raise_issue(
                path=scope_path,
                message=(
                    "CSV round-trip validation failed: output object count mismatch: "
                    f"expected {len(expected)}, got {len(actual)}"
                ),
                source=source,
            )

        for output_index, (expected_output, actual_output) in enumerate(
            zip(expected, actual, strict=True)
        ):
            output_path = (
                f"subroutine[{subroutine_name}].output[{output_index}]"
                if scope == "subroutine"
                else f"main.output[{output_index}]"
            )
            source = (
                source_rungs[min(output_index, len(source_rungs) - 1)] if source_rungs else None
            )
            self._compare_scope_output(
                expected=expected_output,
                actual=actual_output,
                path=output_path,
                source=source,
            )

    def _expected_scope_outputs(
        self,
        rungs: list[Rung],
        *,
        scope: str,
        subroutine_name: str | None,
    ) -> list[_ScopeOutput]:
        outputs: list[_ScopeOutput] = []
        for rung_index, rung in enumerate(rungs):
            base_path = (
                f"subroutine[{subroutine_name}].rung[{rung_index}]"
                if scope == "subroutine"
                else f"main.rung[{rung_index}]"
            )
            outputs.extend(self._expected_outputs_for_rung(rung, path=base_path))
        return outputs

    def _expected_outputs_for_rung(self, rung: Rung, *, path: str) -> list[_ScopeOutput]:
        if any(
            type(instruction).__name__ == "ForLoopInstruction" for instruction in rung._instructions
        ):
            instruction = rung._instructions[0]
            if not isinstance(instruction, ForLoopInstruction):
                self._raise_issue(
                    path=f"{path}.instruction[0]",
                    message="Internal error: expected ForLoopInstruction.",
                    source=instruction,
                )
            for_token = self._fn(
                "for",
                self._render_operand(
                    instruction.count,
                    path=f"{path}.instruction[0](ForLoopInstruction).count",
                    source=instruction,
                ),
                **({"oneshot": "1"} if getattr(instruction, "oneshot", False) else {}),
            )
            outputs = [
                _ScopeOutput(
                    af_token=for_token,
                    condition_tree=self._conditions_to_tree(
                        list(rung._conditions),
                        path=f"{path}.condition",
                    ),
                    pins=(),
                )
            ]
            for child_index, child_instruction in enumerate(
                getattr(instruction, "instructions", ())
            ):
                child_path = f"{path}.instruction[0](ForLoopInstruction).instruction[{child_index}]({type(child_instruction).__name__})"
                outputs.append(
                    _ScopeOutput(
                        af_token=self._instruction_token(child_instruction, path=child_path),
                        condition_tree=None,
                        pins=tuple(self._expected_pin_infos(child_instruction, path=child_path)),
                    )
                )
            outputs.append(_ScopeOutput(af_token=self._fn("next"), condition_tree=None, pins=()))
            return outputs

        normalized = self._normalize_branching_rung(rung) if rung._branches else rung
        if not normalized._instructions and not normalized._branches:
            return [
                _ScopeOutput(
                    af_token="NOP",
                    condition_tree=self._conditions_to_tree(
                        list(normalized._conditions),
                        path=f"{path}.condition",
                    ),
                    pins=(),
                )
            ]

        outputs: list[_ScopeOutput] = []
        for tree, instruction, instruction_path in self._collect_expected_output_trees(
            normalized,
            path=path,
        ):
            outputs.append(
                _ScopeOutput(
                    af_token=self._instruction_token(instruction, path=instruction_path),
                    condition_tree=tree,
                    pins=tuple(self._expected_pin_infos(instruction, path=instruction_path)),
                )
            )
        return outputs

    def _flatten_analyzed_outputs(self, rungs: list[_AnalyzedRung]) -> list[_ScopeOutput]:
        outputs: list[_ScopeOutput] = []
        for rung in rungs:
            for instruction in rung.instructions:
                outputs.append(
                    _ScopeOutput(
                        af_token=instruction.af_token,
                        condition_tree=self._series_tree(
                            rung.condition_tree, instruction.branch_tree
                        ),
                        pins=tuple(instruction.pins),
                    )
                )
        return outputs

    def _compare_scope_output(
        self,
        *,
        expected: _ScopeOutput,
        actual: _ScopeOutput,
        path: str,
        source: Any,
    ) -> None:
        if expected.af_token != actual.af_token:
            self._raise_issue(
                path=path,
                message=(
                    "CSV round-trip validation failed: "
                    f"AF mismatch: expected {expected.af_token!r}, got {actual.af_token!r}"
                ),
                source=source,
            )

        if not trees_equal(expected.condition_tree, actual.condition_tree):
            self._raise_issue(
                path=f"{path}.condition",
                message=(
                    "CSV round-trip validation failed: "
                    f"condition tree mismatch: expected {self._tree_debug(expected.condition_tree)}, "
                    f"got {self._tree_debug(actual.condition_tree)}"
                ),
                source=source,
            )

        if len(expected.pins) != len(actual.pins):
            self._raise_issue(
                path=f"{path}.pin",
                message=(
                    "CSV round-trip validation failed: "
                    f"pin count mismatch: expected {len(expected.pins)}, got {len(actual.pins)}"
                ),
                source=source,
            )

        for pin_index, (expected_pin, actual_pin) in enumerate(
            zip(expected.pins, actual.pins, strict=True)
        ):
            self._compare_pin_info(
                expected=expected_pin,
                actual=actual_pin,
                path=f"{path}.pin[{pin_index}]",
                source=source,
            )

    def _collect_expected_output_trees(
        self,
        rung: Rung,
        *,
        path: str,
        prefix_tree: SPNode | None = None,
    ) -> list[tuple[SPNode | None, Any, str]]:
        local_conditions = list(rung._conditions[rung._branch_condition_start :])
        local_tree = self._conditions_to_tree(local_conditions, path=f"{path}.condition")
        current_tree = self._series_tree(prefix_tree, local_tree)

        outputs: list[tuple[SPNode | None, Any, str]] = []
        instruction_index_by_id = {id(item): idx for idx, item in enumerate(rung._instructions)}
        branch_index_by_id = {id(item): idx for idx, item in enumerate(rung._branches)}

        for item in rung._execution_items:
            if isinstance(item, Rung):
                branch_idx = branch_index_by_id[id(item)]
                outputs.extend(
                    self._collect_expected_output_trees(
                        item,
                        path=f"{path}.branch[{branch_idx}]",
                        prefix_tree=current_tree,
                    )
                )
                continue

            instruction_idx = instruction_index_by_id[id(item)]
            outputs.append(
                (
                    current_tree,
                    item,
                    f"{path}.instruction[{instruction_idx}]({type(item).__name__})",
                )
            )

        return outputs

    def _conditions_to_tree(
        self,
        conditions: list[Condition],
        *,
        path: str,
    ) -> SPNode | None:
        nodes = [
            self._condition_to_tree(condition, path=f"{path}[{index}]")
            for index, condition in enumerate(conditions)
        ]
        if not nodes:
            return None
        return make_compound(nodes, Series)

    def _condition_to_tree(self, condition: Condition, *, path: str) -> SPNode:
        if isinstance(condition, AllCondition):
            children = [
                self._condition_to_tree(child, path=f"{path}.all[{index}]")
                for index, child in enumerate(condition.conditions)
            ]
            return make_compound(children, Series)
        if isinstance(condition, AnyCondition):
            children = [
                self._condition_to_tree(child, path=f"{path}.any[{index}]")
                for index, child in enumerate(condition.conditions)
            ]
            return make_compound(children, Parallel)
        return Leaf(self._condition_leaf_token(condition, path=path))

    def _expected_pin_infos(self, instruction: Any, *, path: str) -> list[_PinInfo]:
        pins: list[_PinInfo] = []
        for pin_index, (pin_name, condition, pin_token) in enumerate(
            self._pin_specs(instruction, path=path)
        ):
            match = _PIN_RE.match(pin_token)
            if match is None:
                self._raise_issue(
                    path=f"{path}.pin[{pin_index}]",
                    message=f"Internal error: invalid pin token {pin_token!r}.",
                    source=instruction,
                )
            pins.append(
                _PinInfo(
                    name=pin_name,
                    arg=match.group(2),
                    conditions=[],
                    condition_tree=self._condition_to_tree(
                        condition,
                        path=f"{path}.pin[{pin_name}]",
                    ),
                )
            )
        return pins

    def _compare_pin_info(
        self,
        *,
        expected: _PinInfo,
        actual: _PinInfo,
        path: str,
        source: Any,
    ) -> None:
        if expected.name != actual.name or expected.arg != actual.arg:
            self._raise_issue(
                path=path,
                message=(
                    "CSV round-trip validation failed: "
                    f"pin mismatch: expected .{expected.name}({expected.arg}), "
                    f"got .{actual.name}({actual.arg})"
                ),
                source=source,
            )

        if not trees_equal(expected.condition_tree, actual.condition_tree):
            self._raise_issue(
                path=path,
                message=(
                    "CSV round-trip validation failed: "
                    f"pin condition mismatch: expected {self._tree_debug(expected.condition_tree)}, "
                    f"got {self._tree_debug(actual.condition_tree)}"
                ),
                source=source,
            )

    @staticmethod
    def _tree_debug(tree: SPNode | None) -> str:
        if tree is None:
            return "None"
        if isinstance(tree, Leaf):
            return repr(tree.label)
        kind = "AND" if isinstance(tree, Series) else "OR"
        inner = ", ".join(_RoundTripValidationMixin._tree_debug(child) for child in tree.children)
        return f"{kind}({inner})"


__all__ = ["_RoundTripValidationMixin", "_ValidationMixin"]
