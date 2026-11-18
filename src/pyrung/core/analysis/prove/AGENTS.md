# Static Facts Agent Guidance

This file provides guidance to coding agents working in this subsystem.

## What this subsystem does

`prove/` builds static facts about a program. `_build_prior_facts(program)` returns a `_PriorFacts` or an `Intractable`. The facts say which tags carry state from scan to scan and which are inputs. They give each one the values it can take, and they name what PILOT's world key tracks. `plc.how()` (PILOT, `../pilot/`, with its own AGENTS.md) steers with them. The validators read value domains through `collect_produced_value_domains` (`../value_domains.py`, `core/validation/cmp_conditions.py`).

Nothing here searches states or proves a property. The package keeps the name `prove/`.

PILOT runs the PLC to check every plan it returns. So a domain that is too wide costs a slower plan, and a domain that is too narrow can hide a plan. A domain may hold values no run reaches. It must not leave out a value a run can reach.

`tests/facts/` checks that the facts hold. It runs the idiom corpus and the conformance programs, and it judges every fact on every scan of every run. A fact a run refutes is a bug in the fact. Each known one is a strict xfail there, and the roadmap lists them under 1b.

### Where a tag's values come from

- A tag the program writes holds its power-on value and what its writers store. A copy's or a `math()`'s store conversion applies (`_stored_domain`).
- A timer's or counter's Acc starts from 0, from its default and from every value another write stores, such as a copy or a `math()` (`_acc_start_values`). Its instruction times or counts on from each of them.
  - An up counter with a down input can cross its preset either way. So its domain also holds the preset and the values one and two below it and one above it.
  - An Acc gets no domain when the outside writes it, when a write stores values no list holds, or when two instructions time or count it.
- Tags the rungs compare with each other share the values of their domains (`_close_tag_comparisons`). Each gap between two of those values, and each end, also gets one value for each tag in the group. A group with a `Real` gets them at even fractions of the gap. A group with an integer tag also gets whole numbers there, so an `Int` and a `Real` can be equal. A value added below the lowest end goes no lower than the tag can hold, such as 0 for an Acc that no write sets negative. So when a rung compares two tags directly, as in `A == B` or `A < B`, some pair of domain values answers as the run does. A chain such as `A < B, B < C` works the same way.
- A comparison through a pointer or an expression gets no such pair, and neither does a comparison with a tag that has no domain (`_tag_comparisons`). A tag compared that way gets no domain, unless a domain can list every value it holds (`_drop_unlisted_comparisons`). Such a domain is one of these:
  - the `choices=` of a tag no rung writes. A rung may store a value past a tag's choices.
  - the declared range of an integer tag no rung writes. A `Real`'s range has too many values to list.
  - the values its writers store (`collect_produced_value_domains`), when the outside does not write it and no instruction times or counts it

  Each tag those writers read that has a domain then needs every value too, since a swap of it changes what they store.

  A tag preset counts as a comparison of the Acc with the preset tag.
- An `external` tag or a field feedback (`link=`) that the program writes also holds a free input's values, because the outside can write it any value (`_extract_value_domain`).
- A system tag the runtime writes, such as a clock or an RTC field, holds the values `RUNTIME_VALUES` lists (`core/system_points.py`). The scan counter and the other tags with too many values to list get no domain. So a copy of one of them gets none either.
- A `Real` the program writes gets no domain when it may hold NaN or Inf (`non_finite.py`). A NaN is not equal to itself, so no list of values can stand for it. A `Real` may hold one when:
  - a `math()` can pass the Real range, divides by a `Real`, or raises to a power other than a whole number from 0 to 64
  - a pack fills it
  - the outside writes it too
  - it copies a `Real` that may hold one, such as an input or a receive payload
- An input `Real` with no ladder writer and a `receive()` payload keep an input domain, which has no NaN in it. A copy of an input `Real` with no ladder writer into another type takes that domain plus what a NaN or an Inf stores. So does a `math()` of the bare tag. A copy clamps an Inf (a Word stores 65535 or 32768). It stores 0 for a NaN, except into a `Dint`, which stores -2147483648. A `math()` stores 0 for a NaN and for either Inf.
- A write through a pointer with no declared bounds, into a block of more than 1000 elements (`_INDIRECT_BLOCK_CAP` in pdg.py), lists no targets. `ProgramGraph.unlisted_writers_of` names the rungs that make such writes, for every element the pointer can reach. The facts give such an element no domain, whatever its type. A window or an index expression into such a block counts too, even with declared bounds.
  - The pointer reaches its power-on value and what its writers store (`_UnlistedReach` in pdg.py, over `value_sets`).
  - A tag the scan writes before every read of it (`unconditional_write_before_read`) never shows its power-on value.
  - A writer stores under its rung's comparisons of a tag with a constant (`value_sets.rung_guards`). So `math(Cmd * 10 + 2999, At)` under `Cmd >= 1, Cmd <= 100` reaches only 3009..3999.
  - An input with declared `choices` or `min`/`max` holds those, unless the ladder writes it too. A CLICK stores a ladder write past a declared bound, and pyrung only reports it.
  - A write through a pointer the graph lists by its declared bounds (`_indirect_ref_tags`) reaches only those elements here too (`_declared_reach`). Roadmap 1b holds that question.
  - A pointer the outside or a `receive()` sets reaches the whole block.
  - An address outside the block is no write, as the runtime drops it.
- A tag with no ladder writer holds its default only when two checks are False:
  - `tag_written_from_outside` (`core/tag.py`). That covers `external`, physical inputs, `link=` feedbacks and the system tags the runtime writes.
  - `ProgramGraph.unlisted_writers_of`, the writes through a pointer that list no targets. `ProgramGraph.may_be_written` asks it and `writers_of` together.

  The facts, the pilot and the lints ask both before they read "no writer" as "constant" or "input".
- The validators' produced domains (`collect_produced_value_domains`) treat a tag with no writer as an input when it is `external`, a physical input or a `link=` feedback.
  - An input holds its declared `choices=` or integer `min=`/`max=`. A `Real` input gets no domain, because it can hold any value between its bounds.
  - Any other tag with no writer holds its default.
  - The runtime writes the system tags, so these domains leave them out.
  - A tag a write through a pointer that lists no targets may reach gets none.

## Build and test

```
make test-prove    # the fact tests in tests/core/analysis/ (test_prove_*.py, packml_diagnosis), then tests/facts
make test-pilot    # how(), which reads the facts
make lint
```

## Glossary

- **Dimension** — a tag the facts track. It is stateful (its value carries to the next scan) or nondeterministic (an input PILOT may set). A tag written and then read in the same scan is combinational and is not tracked.
- **Elision** — Drops a scan-local tag from the world key. A tag is scan-local when every scan writes it before reading it. The slice fast path trusts def-use chains, which leave out an instruction's read of its own accumulator (`exclusive_reads`). `self_read_tags` sends those to the full check, or a timer's accumulator would leave the key. A continuous search's result goes there too, since the search reads it to resume. The full check runs one scan from each entry state with every instruction's memory at its default. Some instructions read a field only on some scans, as their own memory decides. A time drum reads its step's preset only while the step times. So in the check's scans each such instruction counts as reading every tag those fields name (`_STATE_DECIDED_READS` and `_EntryRead` in slice.py). A tag the scan wrote before the instruction ran is no such read. The fast path counts each write an unconditional rung declares as sure, so it holds only for writes that always fire. Risk: calling a cross-scan tag scan-local.
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
- **`non_finite.py`** — The `Real` tags that may hold NaN or Inf, found from the bounds of each writer's values.
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
  The key leaves out a stateful tag with no domain. Some Accs have none, and so does a tag compared with values no list holds (see above). PILOT then cannot tell apart two states that differ only in that tag.
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
- `test_prove_non_finite.py` — `Real` tags that may hold NaN or Inf
- `test_prove_system_points.py` — system tags in the facts
- `test_prove_slice_elision.py` — slice elision
- `test_packml_diagnosis.py` — PackML-specific regression tests
