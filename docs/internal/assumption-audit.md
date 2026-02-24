# Assumption Audit Checklist

This checklist is for finding tests that accidentally lock in implementation quirks
instead of intended behavior.

## Goal

For each test:

- Keep tests that encode user-visible or spec-visible behavior.
- Rename tests that are valid but vague/implementation-coupled.
- Replace tests that only assert incidental internals with invariant-based tests.

## Fast Name Audit

Run:

```bash
rg -n "^\s*def test_[^(]+" tests/core tests/examples
```

Flag names containing terms that often indicate hidden assumptions:

- `missing`, `default`, `none`
- `before`, `after`, `order`, `first`, `second`
- `without`, `only`, `still`
- `noop`, `silent`

These are not always wrong; they are review prompts.

## Triage Questions

For each flagged test:

1. Is this behavior documented in `docs/` or part of public DSL semantics?
2. Would changing internals while preserving behavior still make this test pass?
3. Is the assertion expressed as an invariant (what must hold), not a mechanism (how it happens)?
4. Does the name communicate observable behavior and trigger conditions?

If answers are weak, rewrite or replace the test.

## Naming Guidance

Prefer:

- `test_<trigger>_<expected_observable_outcome>`

Avoid:

- names that encode accidental mechanics (`source_order_before_call`, `is_noop`, `returns_none_without...`)

## Add Guards for Repeating Bug Classes

When a bug is fixed:

1. Add a focused regression test.
2. Add a semantic lint/static check if the bug has a detectable code pattern.
3. Add one invariant test that checks complementary behavior (e.g., `==` vs `!=` with missing/default values).
