# Code Reduction Opportunities

Found after the Expression refactor (`BinaryExpr`/`UnaryExpr`/`ExprCompare`, -438 lines).

## Source Code

### 1. findings.py: 19-arm `if code ==` chain (~100 lines)

`click/validation/findings.py:79-225` — `_build_suggestion()` is a 19-arm
`if code == CONSTANT:` chain where every arm returns a string. Most are 2-4
lines of string interpolation with no real branching. A dict mapping
`code -> callable(meta, tag_map) -> str` collapses this to ~40 lines.

### 2. portability.py: repeated ClickFinding constructor (~50 lines)

`click/validation/portability.py` — `ClickFinding(code=..., severity=_route_severity(...), message=..., location=..., suggestion=...)` is called 10+ times with the same 5-field shape. A helper like `_finding(code, message, location, mode)` that packages `_route_severity` internally cuts each 5-line block to 1-2 lines. Same pattern repeats in `_evaluate_immediate_coil_target` and `_evaluate_immediate_usage`.

### 3. send_receive.py: duplicated __post_init__ validation (~35 lines)

`core/instruction/send_receive.py:152-228` — `ModbusTcpTarget.__post_init__`
and `ModbusRtuTarget.__post_init__` validate their fields with the same
`isinstance`/`raise TypeError` + range check pattern. Both validate `name`,
`device_id`, and `timeout_ms` with identical logic. A shared helper or
table-driven `_validate_fields(self, schema)` halves the line count.

### 4. emitter.py + constants.py: duplicated instruction list (~15 lines)

`click/codegen/emitter.py:194-217` defines a local `instruction_map` dict (22
entries) that maps CSV token names to Python import names. `constants.py:59-87`
defines `_INSTRUCTION_NAMES` as a set of the same tokens. They diverge only for
`"math" -> "calc"` and `"return" -> "return_early"`. Merging into one source of
truth in constants.py eliminates duplication.

## Tests

### 5. test_validation.py: warn/strict twin tests (~120 lines)

`tests/click/test_validation.py` — across 7+ test classes, every class has an
identical warn-mode/strict-mode pair with the same setup. A
`@pytest.mark.parametrize("mode,bucket", [("warn", "hints"), ("strict", "errors")])`
with a shared fixture factory reduces each pair to a single parametrized test.

### 6. test_condition.py: 6 comparison test classes (~110 lines)

`tests/core/test_condition.py:11-165` — `TestCompareEq`, `TestCompareNe`,
`TestCompareLt`, `TestCompareGt`, `TestCompareLe`, `TestCompareGe` each have
the same 3-method structure. A single `@pytest.mark.parametrize` over
`(operator, true_value, false_value)` tuples collapses to ~25 lines.

### 7. test_raw_modbus.py: repeated scaffold (~90 lines)

`tests/core/test_raw_modbus.py:529-793` — `TestRawExecuteStateMachine` has ~8
test methods that each independently construct the same 15-line program/runner
setup. A `_make_send_runner` helper collapses setup to one line per test.

### 8. test_expression.py: 14 identical eval tests (~65 lines)

`tests/core/test_expression.py:19-197` — `TestBasicArithmetic` has 14 methods
that each follow: create block, build `tag OP literal`, create state, evaluate,
assert. All are 7 lines. One `@pytest.mark.parametrize` table.

### 9. test_instruction.py: oneshot/immutability boilerplate (~40 lines)

`tests/core/test_instruction.py` — multiple test classes each have
`test_X_oneshot` and `test_X_does_not_mutate_input` methods following an
identical 8-line template. Parametrize-able.

### 10. test_condition.py: Rising/FallingEdge twins (~35 lines)

`tests/core/test_condition.py:230-300` — structurally identical 3-method
classes for rising and falling edge. Parametrize over condition type and
transition direction.
