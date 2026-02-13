"""Tests for the generic validation walker (Stage 1).

Covers all 8 test cases from the click-validation-stage-1-generic-walker-plan.
"""

from pyrung.core import (
    Block,
    Bool,
    Dint,
    Int,
    Program,
    Rung,
    TagType,
    branch,
    copy,
    math,
    out,
    subroutine,
)
from pyrung.core.condition import Condition
from pyrung.core.instruction import Instruction
from pyrung.core.time_mode import TimeUnit
from pyrung.core.validation.walker import (
    OperandFact,
    ProgramFacts,
    walk_program,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DS = Block("DS", TagType.INT, 1, 100)
DD = Block("DD", TagType.DINT, 1, 100)
Result = Dint("Result")
A = Int("A")
B = Int("B")


def _facts_at(facts: ProgramFacts, arg_path: str) -> list[OperandFact]:
    """Return all facts matching the given arg_path."""
    return [f for f in facts.operands if f.location.arg_path == arg_path]


def _first(facts: ProgramFacts, arg_path: str) -> OperandFact:
    """Return the first fact matching arg_path, or raise."""
    matches = _facts_at(facts, arg_path)
    assert matches, f"no fact with arg_path={arg_path!r}"
    return matches[0]


# ---------------------------------------------------------------------------
# 1. Expression in copy source
# ---------------------------------------------------------------------------


class TestExpressionSource:
    """copy(DS[1] * 2, Result) produces instruction.source as expression."""

    def test_expression_source(self):
        with Program() as prog:
            with Rung():
                copy(DS[1] * 2, Result)

        facts = walk_program(prog)
        src = _first(facts, "instruction.source")
        assert src.value_kind == "expression"
        assert "MulExpr" in src.summary or src.metadata.get("expr_type") == "MulExpr"

    def test_expression_metadata_has_expr_type(self):
        with Program() as prog:
            with Rung():
                copy(DS[1] * 2, Result)

        facts = walk_program(prog)
        src = _first(facts, "instruction.source")
        assert src.metadata["expr_type"] == "MulExpr"


# ---------------------------------------------------------------------------
# 2. IndirectRef for source and target
# ---------------------------------------------------------------------------


class TestIndirectRef:
    """copy(DD[Index], DD[Dst]) produces indirect_ref for both."""

    def test_indirect_ref_source_and_target(self):
        Index = Dint("Index")
        Dst = Dint("Dst")

        with Program() as prog:
            with Rung():
                copy(DD[Index], DD[Dst])

        facts = walk_program(prog)

        src = _first(facts, "instruction.source")
        assert src.value_kind == "indirect_ref"
        assert src.metadata["block_name"] == "DD"
        assert src.metadata["pointer_name"] == "Index"

        tgt = _first(facts, "instruction.target")
        assert tgt.value_kind == "indirect_ref"
        assert tgt.metadata["block_name"] == "DD"
        assert tgt.metadata["pointer_name"] == "Dst"


# ---------------------------------------------------------------------------
# 3. IndirectExprRef
# ---------------------------------------------------------------------------


class TestIndirectExprRef:
    """copy(DD[idx + 1], Result) produces indirect_expr_ref."""

    def test_indirect_expr_ref(self):
        idx = Dint("idx")

        with Program() as prog:
            with Rung():
                copy(DD[idx + 1], Result)

        facts = walk_program(prog)
        src = _first(facts, "instruction.source")
        assert src.value_kind == "indirect_expr_ref"
        assert src.metadata["block_name"] == "DD"
        assert src.metadata["expr_type"] == "AddExpr"


# ---------------------------------------------------------------------------
# 4. Expression in rung condition
# ---------------------------------------------------------------------------


class TestExpressionCondition:
    """with Rung((A + B) > 100) captures expression facts under condition."""

    def test_expr_condition_children(self):
        with Program() as prog:
            with Rung((A + B) > 100):
                out(Bool("Light"))

        facts = walk_program(prog)

        # Top-level condition
        cond = _first(facts, "condition")
        assert cond.value_kind == "condition"
        assert cond.metadata["condition_type"] == "ExprCompareGt"

        # Left child is the (A + B) expression
        left = _first(facts, "condition.left")
        assert left.value_kind == "expression"
        assert left.metadata["expr_type"] == "AddExpr"

        # Right child is the literal 100
        right = _first(facts, "condition.right")
        assert right.value_kind == "expression"
        assert right.metadata["expr_type"] == "LiteralExpr"


class TestIntTruthyCondition:
    """with Rung(IntTag) captures IntTruthyCondition and child tag metadata."""

    def test_int_truthy_condition_fact(self):
        with Program() as prog:
            with Rung(A):
                out(Bool("Light"))

        facts = walk_program(prog)
        cond = _first(facts, "condition")
        assert cond.value_kind == "condition"
        assert cond.metadata["condition_type"] == "IntTruthyCondition"

    def test_int_truthy_condition_child_tag(self):
        with Program() as prog:
            with Rung(A):
                out(Bool("Light"))

        facts = walk_program(prog)
        tag_fact = _first(facts, "condition.tag")
        assert tag_fact.value_kind == "tag"
        assert tag_fact.metadata["tag_type"] == "INT"


# ---------------------------------------------------------------------------
# 5. Branch path correctness
# ---------------------------------------------------------------------------


class TestBranchPath:
    """Root rung has branch_path=(), nested branches get tuple indexes."""

    def test_root_rung_branch_path(self):
        Light = Bool("Light")

        with Program() as prog:
            with Rung():
                out(Light)

        facts = walk_program(prog)
        f = _first(facts, "instruction.target")
        assert f.location.branch_path == ()

    def test_nested_branch_path(self):
        Button = Bool("Button")
        Light = Bool("Light")
        Motor = Bool("Motor")

        with Program() as prog:
            with Rung(Button):
                out(Light)
                with branch():
                    out(Motor)

        facts = walk_program(prog)

        # Light is in root rung
        light_facts = [
            f
            for f in facts.operands
            if f.location.arg_path == "instruction.target" and f.metadata.get("tag_name") == "Light"
        ]
        assert light_facts
        assert light_facts[0].location.branch_path == ()

        # Motor is in first branch
        motor_facts = [
            f
            for f in facts.operands
            if f.location.arg_path == "instruction.target" and f.metadata.get("tag_name") == "Motor"
        ]
        assert motor_facts
        assert motor_facts[0].location.branch_path == (0,)


# ---------------------------------------------------------------------------
# 6. Subroutine coverage
# ---------------------------------------------------------------------------


class TestSubroutineFacts:
    """Facts from subroutines include scope='subroutine' and subroutine name."""

    def test_subroutine_scope(self):
        Light = Bool("Light")

        with Program() as prog:
            with subroutine("my_sub"):
                with Rung():
                    out(Light)

        facts = walk_program(prog)
        sub_facts = [f for f in facts.operands if f.location.scope == "subroutine"]
        assert sub_facts
        assert all(f.location.subroutine == "my_sub" for f in sub_facts)

    def test_main_scope(self):
        Light = Bool("Light")

        with Program() as prog:
            with Rung():
                out(Light)

        facts = walk_program(prog)
        main_facts = [f for f in facts.operands if f.location.scope == "main"]
        assert main_facts
        assert all(f.location.subroutine is None for f in main_facts)


# ---------------------------------------------------------------------------
# 7. Deterministic ordering
# ---------------------------------------------------------------------------


class TestDeterministicOrdering:
    """Repeated walk_program calls return equal tuples."""

    def test_deterministic(self):
        Button = Bool("Button")
        Light = Bool("Light")

        with Program() as prog:
            with Rung(Button):
                out(Light)
                copy(DS[1] * 2, Result)

        facts1 = walk_program(prog)
        facts2 = walk_program(prog)
        assert facts1.operands == facts2.operands


# ---------------------------------------------------------------------------
# 8. Unknown object resilience
# ---------------------------------------------------------------------------


class TestUnknownResilience:
    """Custom instruction with nonstandard fields yields unknown fact, no exception."""

    def test_unknown_instruction(self):
        class CustomInstruction(Instruction):
            def __init__(self):
                self.mystery = 42

            def execute(self, ctx):
                pass

        with Program() as prog:
            with Rung():
                pass
        # Manually add the custom instruction to the rung
        prog.rungs[0].add_instruction(CustomInstruction())

        facts = walk_program(prog)
        unknown = [f for f in facts.operands if f.value_kind == "unknown"]
        assert unknown
        assert unknown[0].metadata["class_name"] == "CustomInstruction"
        assert unknown[0].location.arg_path == "instruction"

    def test_unknown_condition(self):
        class CustomCondition(Condition):
            def __init__(self):
                self.mystery_field = 99

            def evaluate(self, ctx):
                return True

        with Program() as prog:
            with Rung():
                out(Bool("Light"))
        # Manually add the custom condition to the rung
        prog.rungs[0]._conditions.append(CustomCondition())

        facts = walk_program(prog)
        cond_facts = [
            f
            for f in facts.operands
            if f.value_kind == "condition" and f.metadata.get("condition_type") == "CustomCondition"
        ]
        assert cond_facts
        # Should also have recursed into the mystery_field as literal
        child_facts = [f for f in facts.operands if "mystery_field" in f.location.arg_path]
        assert child_facts
        assert child_facts[0].value_kind == "literal"


# ---------------------------------------------------------------------------
# 9. expr_dsl metadata for IndirectExprRef and Expression
# ---------------------------------------------------------------------------


class TestExprDslMetadata:
    """Walker adds expr_dsl key to metadata for expressions."""

    def test_indirect_expr_ref_has_expr_dsl(self):
        idx = Dint("idx")

        with Program() as prog:
            with Rung():
                copy(DD[idx + 1], Result)

        facts = walk_program(prog)
        src = _first(facts, "instruction.source")
        assert src.value_kind == "indirect_expr_ref"
        assert "expr_dsl" in src.metadata
        assert "idx" in str(src.metadata["expr_dsl"])

    def test_expression_has_expr_dsl(self):
        with Program() as prog:
            with Rung():
                copy(DS[1] * 2, Result)

        facts = walk_program(prog)
        src = _first(facts, "instruction.source")
        assert src.value_kind == "expression"
        assert "expr_dsl" in src.metadata


# ---------------------------------------------------------------------------
# 10. Missing instruction fields now captured
# ---------------------------------------------------------------------------


class TestMissingFieldsCaptured:
    """Verify time_unit, mode, continuous fields are now captured."""

    def test_math_mode_captured(self):
        Dest = Int("Dest")

        with Program() as prog:
            with Rung():
                math(A * 2, Dest)

        facts = walk_program(prog)
        mode_facts = _facts_at(facts, "instruction.mode")
        assert mode_facts
        assert mode_facts[0].value_kind == "literal"
        assert mode_facts[0].summary == "'decimal'"

    def test_on_delay_time_unit_captured(self):
        from pyrung.core.program import on_delay

        done = Bool("T1")
        acc = Int("TD1")

        with Program() as prog:
            with Rung(Bool("Enable")):
                on_delay(done, acc, 100, TimeUnit.Ts)

        facts = walk_program(prog)
        tu_facts = _facts_at(facts, "instruction.time_unit")
        assert tu_facts
        assert tu_facts[0].value_kind == "literal"
        assert "Ts" in tu_facts[0].summary

    def test_off_delay_time_unit_captured(self):
        from pyrung.core.program import off_delay

        done = Bool("T2")
        acc = Int("TD2")

        with Program() as prog:
            with Rung(Bool("Enable2")):
                off_delay(done, acc, 100, TimeUnit.Tm)

        facts = walk_program(prog)
        tu_facts = _facts_at(facts, "instruction.time_unit")
        assert tu_facts
        assert tu_facts[0].value_kind == "literal"
        assert "Tm" in tu_facts[0].summary

    def test_search_continuous_captured(self):
        from pyrung.core.program import search

        found = Bool("Found")
        result = Int("Result2")

        with Program() as prog:
            with Rung():
                search("==", 42, DS.select(1, 10), result, found, continuous=True)

        facts = walk_program(prog)
        cont_facts = _facts_at(facts, "instruction.continuous")
        assert cont_facts
        assert cont_facts[0].value_kind == "literal"
        assert cont_facts[0].summary == "True"


# ---------------------------------------------------------------------------
# 11. Oneshot flag captured
# ---------------------------------------------------------------------------


class TestOneshotCaptured:
    """Walker captures oneshot property for OneShotMixin instructions."""

    def test_oneshot_true_captured(self):
        Light = Bool("Light")

        with Program() as prog:
            with Rung():
                out(Light, oneshot=True)

        facts = walk_program(prog)
        os_facts = _facts_at(facts, "instruction.oneshot")
        assert os_facts
        assert os_facts[0].value_kind == "literal"
        assert os_facts[0].summary == "True"

    def test_oneshot_false_captured(self):
        Light = Bool("Light2")

        with Program() as prog:
            with Rung():
                out(Light)

        facts = walk_program(prog)
        os_facts = _facts_at(facts, "instruction.oneshot")
        assert os_facts
        assert os_facts[0].value_kind == "literal"
        assert os_facts[0].summary == "False"


# ---------------------------------------------------------------------------
# 12. Coil tags captured
# ---------------------------------------------------------------------------


class TestCoilsCaptured:
    """Walker captures rung coil tags."""

    def test_coil_tags_emitted(self):
        Light = Bool("Light3")

        with Program() as prog:
            with Rung():
                out(Light)

        facts = walk_program(prog)
        coil_facts = [f for f in facts.operands if f.location.arg_path.startswith("coil[")]
        assert coil_facts
        assert coil_facts[0].value_kind == "tag"
        assert coil_facts[0].metadata["tag_name"] == "Light3"
