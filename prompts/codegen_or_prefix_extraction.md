# Codegen: extract common AND prefix from OR branches

## Problem

When the DFS graph walk finds multiple paths to the same AF cell, it groups them
as OR branches. But it does not factor out conditions that are common to ALL
branches. This means `A, any_of(B, C)` round-trips incorrectly:

```
Original:   with Rung(A, any_of(B, C)): out(Y)
Exported:   R, X001, T:X002, T, -, ..., out(Y001)
             , X001, C1,     , , ...
Codegen:    with Rung(any_of(X001, X002, X001, C1)): out(Y001)   # WRONG
Expected:   with Rung(X001, any_of(X002, C1)): out(Y001)
```

The DFS finds two paths to `out(Y001)`:
- Path 1: `[X001, X002]`
- Path 2: `[X001, C1]`

These get flattened into a single `any_of()` with all conditions concatenated. The
fix is to extract the longest common prefix from all OR branch condition lists and
emit it as shared AND conditions before the `any_of()`.

## Pre-existing

This bug exists in the old T/T/- format too — the old grid for `A, any_of(B, C)`
produces the same two DFS paths with the same duplicate `X001`. It was never caught
because no round-trip test covered mid-rung OR until now.

## Where to fix

`src/pyrung/click/codegen.py` — the path grouping / OR detection logic. Look at
how `_OrGroup` instances are built from `_PathResult` lists. After grouping paths
that share the same AF token, extract the longest common prefix from their condition
lists and move those into `shared_conditions`.

### Algorithm sketch

```python
def _extract_common_prefix(groups: list[_OrGroup]) -> list[str]:
    """Remove and return the longest common prefix across all OR branches."""
    if not groups:
        return []
    prefix = []
    for i, token in enumerate(groups[0].conditions):
        if all(len(g.conditions) > i and g.conditions[i] == token for g in groups):
            prefix.append(token)
        else:
            break
    for g in groups:
        g.conditions = g.conditions[len(prefix):]
    return prefix
```

After extracting the prefix, prepend it to `shared_conditions`. If any OR branch
ends up with an empty condition list after prefix extraction, it means that branch
was unconditional relative to the shared prefix — handle appropriately.

## Tests

Two round-trip tests are marked `xfail` waiting for this fix:
- `TestRoundTrip::test_mid_rung_or` — `A, any_of(B, C)`
- `TestRoundTrip::test_two_series_ors` — `any_of(A, B), any_of(C, D)`

Remove the `xfail` markers once the fix is in and they pass.

## Two series ORs

`any_of(A, B), any_of(C, D)` is a harder variant. The DFS finds paths like:
- `[X001, C1]` (A path, C branch)
- `[X001, C2]` (A path, D branch)
- `[X002]` (B path — only reaches AF through the first OR, second OR is on
  `accepts_terms=True` row only)

The codegen needs to recognise that `[X001, C1]` and `[X001, C2]` share prefix
`X001` AND that `C1`/`C2` form a nested OR, while `X002` is a separate top-level
OR branch. This may require recursive prefix extraction or a tree-based grouping
approach rather than flat prefix stripping.
