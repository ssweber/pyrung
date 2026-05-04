# Refactor elision into a pass pipeline

## Context

`passes.py` defines a clean pre-BFS pipeline: named `_PreBFSPass` objects with
`(name, description, fn)`, operating on a shared `_PassContext` dataclass,
run sequentially by `_run_pre_bfs_pipeline`. Each pass is independently
toggleable, testable, and self-describing.

The elision system (`elision.py`) is currently one monolithic entry point
(`_elide_scan_local_stateful_dims`) that internally runs the abstract phase
and concrete phase as one blob. We want to decompose it into the same
pass-pipeline pattern so we can add new elision rules incrementally without
touching existing logic.

## Goal

Refactor the elision system into an `_ElisionPass` sub-pipeline that mirrors
the `_PreBFSPass` pattern in `passes.py`. No new elision rules yet — just
reorganize what exists into named passes. The external interface
(`_elide_scan_local_stateful_dims` or its replacement) should produce the
same `stateful_dims` result as before.

## Design

### Two levels: passes and rules

The absorb system in `passes.py` has sequential passes with data dependencies
between them (done_acc_pairs → redundant_absorptions → threshold_absorptions).
Genuinely separate stages. Elision is different. The new abstract rules all
consume the same analysis output (provenance lattice, write/read sets, guard
conditions). Running them as separate passes would re-run the analysis each
time. Instead:

- **Passes** — coarse pipeline stages (abstract → concrete). Two of them.
  Each pass runs once. Same shape as `_PreBFSPass`.
- **Rules** — fine-grained checks registered within the abstract pass.
  All rules fire on the same shared analysis output in one shot.
  Each rule is named, described, toggleable, and records which tags it elided.

### `_ElisionContext` — shared accumulator

Similar to `_PassContext`. Holds:

- `program` — the Program
- `graph` — the ProgramGraph
- `stateful_dims` — current retained set (shrinks as passes/rules run)
- `nondeterministic_dims` — ND input domains
- `compiled` — the CompiledKernel (needed by concrete phase)
- `elided` — dict mapping elided tag names to which rule/pass removed them
  (for diagnostics: "why was this tag elided?" → rule name)
- `progress` — optional progress callback

### `_ElisionPass` — same shape as `_PreBFSPass`

```python
@dataclass
class _ElisionPass:
    name: str
    description: str
    fn: Callable[[_ElisionContext], None]
    enabled: bool = True

    def run(self, ctx: _ElisionContext) -> None:
        self.fn(ctx)
```

### `_AbstractRule` — registered checks within the abstract pass

```python
@dataclass
class _AbstractRule:
    name: str
    description: str
    fn: Callable[[_AbstractAnalysis, _ElisionContext], None]
    enabled: bool = True
```

Each rule receives the shared `_AbstractAnalysis` (provenance results, write
sets, guard conditions — computed once) and the `_ElisionContext`. It marks
candidates as elidable by removing them from `ctx.stateful_dims` and recording
`ctx.elided[tag_name] = self.name`.

### Decomposition of existing code

The current `_elide_scan_local_stateful_dims` does roughly:

1. Build `_TagElisionCheck` for each candidate, run abstract interpretation
2. Compute `_compute_nonretained_summaries` (fixed-point projection loop)
3. Run `_prove_tag_from_canonical_entry` for remaining candidates
4. Run concrete `_can_elide` for remaining candidates (with batch removal)

Steps 1-3 become the abstract pass with one registered rule (provenance).
Step 4 becomes the concrete pass.

```python
_DEFAULT_ABSTRACT_RULES: tuple[_AbstractRule, ...] = (
    _AbstractRule(
        "provenance",
        "Per-tag dependency lattice — WBR, unconditional out(), deterministic projections, canonical entry convergence",
        _rule_provenance,
    ),
)

_DEFAULT_ELISION_PASSES: tuple[_ElisionPass, ...] = (
    _ElisionPass(
        "abstract",
        "Run abstract analysis once, apply all registered rules",
        _pass_abstract,
    ),
    _ElisionPass(
        "concrete_batch",
        "Exhaustive kernel proofs — shared baseline, per-candidate perturbation",
        _pass_concrete_batch,
    ),
)
```

The abstract pass:
1. Runs the shared analysis (provenance lattice, write sets, guard extraction)
2. Iterates `_DEFAULT_ABSTRACT_RULES`
3. Each enabled rule fires on the analysis output and shrinks `stateful_dims`

The concrete pass runs on whatever candidates survive all abstract rules.

### Adding new rules later

Adding a rule is: write one function, append one `_AbstractRule` to the tuple.
No changes to existing rules or to the pass structure.

```python
_DEFAULT_ABSTRACT_RULES: tuple[_AbstractRule, ...] = (
    _AbstractRule("provenance", ...),
    # New rules — each independent, each toggleable
    _AbstractRule("write_coverage", "Cross-rung exhaustive guard analysis", _rule_write_coverage),
    _AbstractRule("drum_projection", "Drum outputs from pattern table", _rule_drum_projection),
    _AbstractRule("init_guard_constants", "Write-once init blocks", _rule_init_guard_constants),
    _AbstractRule("oneshot_output", "Oneshot edge semantics", _rule_oneshot_output),
)
```

All rules consume the same analysis output. No re-runs. The rule list is
explicit and readable — you can see exactly what the abstract pass checks
by looking at the tuple.

## What NOT to change

- Don't add any new elision rules. Just reorganize existing code.
- Don't change the `_TagElisionCheck` or `_AbsValue` internals.
- Don't change the concrete `_can_elide` logic.
- The external result (which tags survive in `stateful_dims`) must be
  identical before and after this refactor.

## Validation

Run the PackML benchmark before and after. The retained key must be the same
4 tags. Run the existing elision test suite. No behavioral changes — this is
purely structural.

## Reference

Look at `passes.py` for the pattern: `_PreBFSPass`, `_PassContext`,
`_run_pre_bfs_pipeline`, and how `_pass_elide_scan_local_state` currently
calls into `_elide_scan_local_stateful_dims`. The elision sub-pipeline
replaces the internals of that call.
