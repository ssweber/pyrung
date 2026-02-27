# Plan: Math Expression Support for pyrung Engine (TDD)

## Goal
Enable native Python expressions with Tags in conditions and instruction arguments:
```python
with Rung((DS[1] + DS[2]) > 100):         # Expression in condition
with Rung((Temperature * 1.8 + 32) > 212): # Complex expression
copy(DS[1] * 2 + Offset, Result)           # Expression in copy source
with Rung(DS[idx + 1] > 100):              # Pointer arithmetic
```

## Click Math Operations (Complete Reference)

### Decimal Mode
| Operation | Click Syntax | Python Equivalent |
|-----------|--------------|-------------------|
| Add | `+` | `+` |
| Subtract | `-` | `-` |
| Multiply | `*` | `*` |
| Divide | `/` | `/` |
| Modulo | `MOD` | `%` |
| Power | `^` | `**` |
| Sine | `SIN(x)` | `sin(x)` |
| Cosine | `COS(x)` | `cos(x)` |
| Tangent | `TAN(x)` | `tan(x)` |
| Arc Sine | `ASIN(x)` | `asin(x)` |
| Arc Cosine | `ACOS(x)` | `acos(x)` |
| Arc Tangent | `ATAN(x)` | `atan(x)` |
| Log base 10 | `LOG(x)` | `log10(x)` |
| Natural Log | `LN(x)` | `log(x)` |
| Square Root | `SQRT(x)` | `sqrt(x)` |
| Deg to Rad | `RAD(x)` | `radians(x)` |
| Rad to Deg | `DEG(x)` | `degrees(x)` |
| Pi | `PI` | `PI` (constant) |
| Summation | `SUM(DF1:DF10)` | `sum_range(bank, start, end)` |

### Hex Mode (Bitwise)
| Operation | Click Syntax | Python Equivalent |
|-----------|--------------|-------------------|
| Add | `+` | `+` |
| Subtract | `-` | `-` |
| Multiply | `*` | `*` |
| Divide | `/` | `//` (floor div for hex) |
| Modulo | `MOD` | `%` |
| Bitwise AND | `AND` | `&` |
| Bitwise OR | `OR` | `\|` |
| Bitwise XOR | `XOR` | `^` |
| Shift Left | `LSH(x, n)` | `x << n` or `lsh(x, n)` |
| Shift Right | `RSH(x, n)` | `x >> n` or `rsh(x, n)` |
| Rotate Left | `LRO(x, n)` | `lro(x, n)` |
| Rotate Right | `RRO(x, n)` | `rro(x, n)` |

---

## TDD Implementation Order

### Phase 1: Write Tests First (RED)

Create `tests/engine/test_expression.py` with failing tests:

```python
# Test basic arithmetic - these drive the API design
class TestBasicArithmetic:
    def test_tag_plus_literal(self):
        """DS[1] + 5 creates an AddExpr that evaluates correctly."""
        DS = MemoryBank("DS", TagType.INT, range(1, 10))
        expr = DS[1] + 5
        state = SystemState().with_tags({"DS1": 10})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 15

    def test_literal_plus_tag(self):
        """5 + DS[1] uses __radd__."""
        ...

    def test_tag_minus_tag(self):
        """DS[1] - DS[2]."""
        ...

    def test_multiplication(self):
        """DS[1] * 2."""
        ...

    def test_division(self):
        """DS[1] / 3 returns float."""
        ...

    def test_floor_division(self):
        """DS[1] // 3 returns int."""
        ...

    def test_modulo(self):
        """Count % 10."""
        ...

    def test_power(self):
        """DS[1] ** 2."""
        ...

    def test_negation(self):
        """-DS[1]."""
        ...

    def test_complex_expression(self):
        """(DS[1] * 2) + (DS[2] / 3)."""
        ...

    def test_nested_parentheses(self):
        """((DS[1] + DS[2]) * (DS[3] - DS[4])) / DS[5]."""
        ...


class TestExpressionComparisons:
    def test_expression_gt_literal(self):
        """(DS[1] + DS[2]) > 100 returns a Condition."""
        expr = DS[1] + DS[2]
        cond = expr > 100
        assert isinstance(cond, Condition)

    def test_expression_eq_zero(self):
        """(Count % 10) == 0."""
        ...

    def test_expression_le_expression(self):
        """DS[1] <= (High + Band)."""
        ...


class TestExpressionInRung:
    def test_rung_with_expression_condition(self):
        """with Rung((DS[1] + DS[2]) > 100): out(Alarm)."""
        ...

    def test_fahrenheit_conversion(self):
        """with Rung((Temperature * 1.8 + 32) > 212):"""
        ...


class TestExpressionInCopy:
    def test_copy_expression_to_tag(self):
        """copy(DS[1] * 2 + Offset, Result)."""
        ...


class TestPointerArithmetic:
    def test_indirect_with_expression_index(self):
        """DS[idx + 1] where idx is a Tag."""
        ...


class TestBitwiseOperations:
    def test_bitwise_and(self):
        """DH[1] & DH[2]."""
        ...

    def test_bitwise_or(self):
        """DH[1] | DH[2]."""
        ...

    def test_bitwise_xor(self):
        """DH[1] ^ DH[2] (note: conflicts with power in decimal)."""
        ...

    def test_left_shift(self):
        """DH[1] << 2."""
        ...

    def test_right_shift(self):
        """DH[1] >> 2."""
        ...

    def test_bitwise_invert(self):
        """~DH[1]."""
        ...


class TestMathFunctions:
    def test_sqrt(self):
        """sqrt(DS[1])."""
        ...

    def test_sin_cos_tan(self):
        """Trig functions with radians."""
        ...

    def test_radians_degrees_conversion(self):
        """radians(DS[1]), degrees(DS[1])."""
        ...

    def test_log_functions(self):
        """log10(x), log(x)."""
        ...

    def test_pi_constant(self):
        """PI value."""
        ...
```

### Phase 2: Implement Core Expression Classes (GREEN)

Create `src/pyrung/engine/expression.py`:

```python
from abc import ABC, abstractmethod

Numeric = int | float

class Expression(ABC):
    @abstractmethod
    def evaluate(self, ctx: ScanContext) -> Numeric: ...

    # Arithmetic -> Expression
    def __add__(self, other): ...
    def __radd__(self, other): ...
    # ... etc

    # Comparison -> Condition
    def __gt__(self, other): ...
    # ... etc

class TagExpr(Expression): ...
class LiteralExpr(Expression): ...
class AddExpr(Expression): ...
class SubExpr(Expression): ...
class MulExpr(Expression): ...
class DivExpr(Expression): ...
class FloorDivExpr(Expression): ...
class ModExpr(Expression): ...
class PowExpr(Expression): ...
class NegExpr(Expression): ...
```

### Phase 3: Add Tag Operators (GREEN)

Update `tag.py` to add arithmetic operators that return Expression.

### Phase 4: Add Expression Conditions (GREEN)

Update `condition.py` with ExprCompare* classes.

### Phase 5: Integrate with copy() (GREEN)

Update `instruction.py` `resolve_tag_or_value_ctx()`.

### Phase 6: Add Bitwise (if time permits)

Bitwise operators and shift/rotate functions.

### Phase 7: Add Math Functions (if time permits)

sqrt, sin, cos, tan, etc.

---

## Files to Modify/Create

| File | Action | Phase |
|------|--------|-------|
| `tests/engine/test_expression.py` | **CREATE** | 1 (RED) |
| `src/pyrung/engine/expression.py` | **CREATE** | 2 (GREEN) |
| `src/pyrung/engine/tag.py` | Add arithmetic ops | 3 (GREEN) |
| `src/pyrung/engine/condition.py` | Add ExprCompare* | 4 (GREEN) |
| `src/pyrung/engine/instruction.py` | Update resolver | 5 (GREEN) |
| `src/pyrung/engine/memory_block.py` | Add ops to IndirectTag | 5 (GREEN) |
| `src/pyrung/engine/__init__.py` | Export new symbols | 5 (GREEN) |

## Verification

1. `make test` - new tests should fail initially (RED)
2. Implement until tests pass (GREEN)
3. Refactor if needed
4. All existing tests must continue to pass
