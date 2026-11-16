# Static Facts Agent Guidance

This file provides guidance to coding agents working in this subsystem.

## What this subsystem does

`prove/` builds static facts about a program. `_build_prior_facts(program)` returns a `_PriorFacts` or an `Intractable`. The facts say which tags carry state from scan to scan and which are inputs. They give each one the values it can take, and they name what PILOT's world key tracks. `plc.how()` (PILOT, `../pilot/`, with its own AGENTS.md) steers with them. The validators read value domains through `collect_produced_value_domains` (`../value_domains.py`, `core/validation/cmp_conditions.py`).

Nothing here searches states or proves a property. The package keeps the name `prove/`.

PILOT runs the PLC to check every plan it returns. So a domain that is too wide costs a slower plan, and a domain that is too narrow can hide a plan. A domain may hold values no run reaches. It must not leave out a value a run can reach.

`tests/facts/` checks that the facts hold. It runs the idiom corpus and the conformance programs, and it judges every fact on every scan of every run. A fact a run refutes is a bug in the fact. Each known one is a strict xfail there, and the roadmap lists them under 1b.

### Where a tag's values come from

- A tag the program writes holds its power-on value and what its writers store. A copy's or a `math()`'s store conversion applies (`_stored_domain`).
- An `external` tag or a field feedback (`link=`) that the program writes also holds a free input's values, because the outside can write it any value (`_extract_value_domain`).
- The validators' produced domains (`collect_produced_value_domains`) treat a tag with no writer as an input when it is `external`, a physical input or a `link=` feedback.
  - An input holds its declared `choices=` or integer `min=`/`max=`. A `Real` input gets no domain, because it can hold any value between its bounds.
  - Any other tag with no writer holds its default.
  - The runtime writes the system tags, so these domains leave them out.

## Build and test

```
make test-prove    # the fact tests in tests/core/analysis/ (test_prove_*.py, packml_diagnosis), then tests/facts
make test-pilot    # how(), which reads the facts
make lint
```

## Glossary

- **Dimension** — a tag the facts track. It is stateful (its value carries to the next scan) or nondeterministic (an input PILOT may set). A tag written and then read in the same scan is combinational and is not tracked.
- **Elision** — Drops a scan-local tag from the world key. A tag is scan-local when every scan writes it before reading it. The slice fast path trusts def-use chains, which leave out an instruction's read of its own accumulator (`exclusive_reads`). `self_read_tags` sends those to the full check, or a timer's accumulator would leave the key. Risk: calling a cross-scan tag scan-local.
- **Absorption** — Gives a timer's or counter's accumulator fewer values. The world key projects a timer's Done and Acc onto `False`/`PENDING`/`True` (`absorb._done_acc_state`). A progress accumulator becomes the thresholds it has crossed (`_ThresholdVectorSpec`). Risk: an accumulator read as data, which the exclusivity check (`_has_forbidden_data_read`) must catch.
- **Backward propagation** — Traces comparisons back through arithmetic and copies to seed input domains. Risk: an operation it cannot invert, or a chain it stops following too soon.
- **Functional dependency** — A tag that is always a fixed function of another tag leaves the key (`functional_dep_projections`).
- **Init constant** — A tag written only under a first-scan or latch guard with a literal keeps its one value (`../init_constants.py`).
- **Edge memory** — Each edge contact keeps its own memory (`_edge:e{N}`, one key per contact site). Slice elision tries both values of every edge site whose tag is in a candidate's slice.

## Module map

Each module has a docstring with implementation details. This map is for navigation.

- **`__init__.py`** — `_build_prior_facts`.
- **`results.py`** — `Intractable` (with `tags` and `hints`) and `PENDING`, the third value of an absorbed Done.
- **`passes.py`** — The pass pipeline (`_PRIOR_PASSES`) that fills a `_PassContext` and returns `_PriorFacts`.
- **`classify.py`** — Dimension classification and domain inference. See its docstring for the 7-level domain inference stack. It also holds `collect_produced_value_domains` for the validators.
- **`absorb.py`** — Done/Acc pairing, the data-read exclusivity check, and the absorptions of the world key.
- **`expr.py`** — Expression helpers for partial evaluation, tag references and atom indexing. PILOT evaluates conditions with `_eval_expr_from_state`.
- **`elision/`** — Slice-based world-key elision.
  - **`__init__.py`** — Pipeline entry point (delegates to slice.py).
  - **`slice.py`** — Write-before-read check per candidate, over every entry state of the tag's slice. A tag touched only inside one subroutine, and read by nothing outside the program, is checked per call of that subroutine instead (`_local_slice_check`).

## Pipeline

```
Program
  -> _run_prior_pipeline (passes.py)
    -> build graph -> classify -> elide scan-local state -> functional dependencies
       -> init constants -> compile kernel -> world-key Done specs
  -> _PriorFacts | Intractable
```

A `For` loop whose count can pass `PROVE_FOR_ITERATION_CAP` ends the pipeline with an `Intractable`.

## Invariants to preserve

- **No reachable value left out**: every value a run can store must be in the tag's domain. Extra values are safe.
- **World key completeness**: every tag whose cross-scan value changes what a scan does must be in the key. Otherwise one of the rules above must elide, absorb or project it.
- **Same answers for pilot and validation**: a change here must leave what `how()` and the validators see the same, or say why it differs.

## Testing

`tests/facts/` checks the facts against real runs (see above). The unit tests are in `tests/core/analysis/`, split by theme across `test_prove_*.py` files:

- `test_prove_dimension_classification.py` — dimension classification
- `test_prove_value_domain_extraction.py` — value domain extraction
- `test_prove_dont_care_pruning.py` — don't-care pruning
- `test_prove_timer_abstraction.py` — timers and counters in the facts (tag presets, accumulator thresholds, drums)
- `test_prove_intractable_hints.py` — intractable tags and blocker hints
- `test_prove_kernel_domains.py` — domains of the compiled kernel
- `test_prove_coverage_misc.py` — edge conditions and annotated functions
- `test_prove_inputblock_tagmap_input_inference.py` — InputBlock and TagMap input inference
- `test_prove_absorption_gap_regression_tests.py` — absorption regressions
- `test_prove_simultaneous_edge_coverage_tests.py` — simultaneous edges and elision
- `test_prove_matrix.py` — classification over a matrix of instruction shapes
- `test_prove_passes.py` — pass pipeline unit tests
- `test_prove_edge_demotion.py` — edge-source tags leaving the key while their contacts' memory stays
- `test_prove_system_points.py` — system tags in the facts
- `test_prove_slice_elision.py` — slice elision
- `test_packml_diagnosis.py` — PackML-specific regression tests
