
# Deep Analysis: Concrete Elision System for Shared Baseline Batch Proof Optimization

## Executive Summary

The concrete elision system proves that candidate stateful tags can be safely removed from the BFS state space by
exhaustively enumerating (retained × input) combinations and verifying that varying the candidate + hidden
stateful doesn't affect observable outcomes. The current implementation performs **isolated scans for each
combination**, making it highly parallelizable and cacheable. A **shared baseline batch proof** optimization would
pre-compute one baseline scan per (retained × input) combo, then reuse it across all candidates that observe the
same retained/input scope.

---

## 1. Core Components Overview

### 1.1 Class: `_ConcreteStateElider`

**Initialization** (`__init__`, lines 213–308):

```python
class _ConcreteStateElider:
   def __init__(
       self,
       program: Program,
       graph: ProgramGraph,
       stateful_dims: Mapping[str, tuple[Any, ...]],
       nondeterministic_dims: Mapping[str, tuple[Any, ...]],
       *,
       state_basis: frozenset[str] | None = None,
       compiled: CompiledKernel | None = None,
       progress: Callable[[str], None] | None = None,
   ) -> None:
```

**Held State:**

- `self._program`: Program being analyzed
- `self._graph`: ProgramGraph (PDG with rung nodes, readers_of, writers_of dicts)
- `self._stateful_dims`: Dict[str, tuple[Any, ...]] — all stateful tag names to their discrete value domains
- `self._state_basis`: frozenset[str] — subset of `_stateful_dims` eligible for concrete proofs (excludes
PENDING-bearing and continued-source tags)
- `self._nondeterministic_dims`: Dict[str, tuple[Any, ...]] — external input names to their domains
- `self._compiled`: CompiledKernel — the execution oracle (step_fn, referenced_tags, memory state)
- `self._entry_sensitive_cache`: Cache of (tag_name, retained_frozenset) → bool for `_hidden_entry_matters`
results
- `self._coverage`: _ForcedTrueCoverage — discovered written_tags from pilot scans
- `self._exclusive_input_groups`: Tuple[_ExclusiveInputGroup, ...] — Bool encoder families (multi-hot → one-hot
canonical)
- `self._exclusive_input_group_by_member`: Dict[str, int] — maps input name to its group index
- `self._written_tags`: frozenset[str] — union of static graph writers + dynamically discovered writers from pilot
- `self._continued_source_tags`: frozenset[str] — tags read via continued() rungs
- `self._warm_memory`: Dict[str, Any] | None — discovered "warm" kernel memory state (for oneshot/OTE hidden
state)

**Pilot Scan Phase** (lines 259–299):

Sweeps all (nondeterministic input combination) × (alternate seed: default/alternate) to:
1. **Discover memory keys** — identify kernel.memory entries that differ from fresh kernel defaults
2. **Detect warm memory** — capture a memory state where at least one key differs (used in `_scan` re-run)
3. **Identify written tags** — see which referenced tags change after stepping (lines 198–201)

```python
# Lines 277–297: For each ND input combo and seed:
kernel = forced_compiled.create_kernel()
kernel.tags.update(seed)  # or alternate seed
for name, val in zip(nd_names, combo):
   kernel.tags[name] = val
_step_compiled_kernel(forced_compiled, kernel, dt=_DEFAULT_DT)
# Track which tags changed → added to _coverage.written_tags
```

---

## 2. The Elision Loop Structure (`elide()` and `_pass_concrete_batch()`)

### 2.1 `_pass_concrete_batch` (lines 668–714)

**Entry point from elision pipeline:**

```python
def _pass_concrete_batch(ctx: _ElisionContext) -> None:
   concrete_elider = _ConcreteStateElider(...)
   abstract_retained = frozenset(ctx.stateful_dims)  # Tags approved by abstract pass
   retained = set(abstract_retained)

   # Fast-path: elide never-written tags
   never_written = concrete_elider._never_written_elidable(retained)
   retained.difference_update(never_written)

   # Main loop: iteratively try removing each candidate
   changed = True
   while changed:
       changed = False
       snapshot = set(retained)  # Snapshot for this round
       for tag_name in sorted(snapshot):
           if concrete_elider._can_elide(tag_name, frozenset(snapshot - {tag_name})):
               retained.discard(tag_name)
               ctx.elided[tag_name] = "concrete_batch"
               changed = True

   # Update context with elided tags
   removed_names = set(ctx.stateful_dims) - retained
   for tag_name in removed_names:
       del ctx.stateful_dims[tag_name]
```

**Key observations:**

- **`snapshot`** (line 695): immutable view of current retained set before this round's proofs
- **`changed` flag** (line 694): if any tag removed, re-loop (order matters because retained scope shrinks)
- **Batch removal**: `_ELISION_BATCH_REMOVE = True` (line 35) collects all removable tags in a round, then applies
all at once
- **Round iteration**: Each round tests candidates against the same `snapshot` scope; if a removal happens,
restart with a new snapshot

---

## 3. The Core Proof: `_can_elide()` Inner Loop (lines 454–507)

### 3.1 High-Level Flow

```python
def _can_elide(self, candidate: str, retained: frozenset[str]) -> bool:
   # Step 1: Find reachable stateful frontier
   observed, fallback_hidden = self._reachable_stateful_frontier(candidate, retained)
   if not observed:
       sticky_hidden = tuple(name for name in fallback_hidden
                              if self._hidden_entry_matters(name, retained))
       if not sticky_hidden:
           return True
       observed = sticky_hidden

   # Step 2: Compute scoped dependencies
   retained_names, input_names, hidden_stateful = self._scoped_dependencies(
       candidate, observed, retained
   )

   # Step 3: Enumerate proof space
   retained_domains = tuple(self._stateful_dims[name] for name in retained_names)
   input_assignment_dimensions = self._input_assignment_dimensions(input_names)

   # Compute input combos (exclusive groups collapsed into one-hot options)
   input_combo_count = 1
   for dimension in input_assignment_dimensions:
       input_combo_count *= len(dimension)

   # (retained × input) forms the baseline space
   group_product = _product_size(retained_domains) * input_combo_count

   # (hidden_stateful + candidate) forms the vary space
   vary_names = hidden_stateful + (candidate,)
   vary_domains = tuple(self._stateful_dims[name] for name in hidden_stateful) + (
       self._stateful_dims[candidate],
   )

   # Check against budget
   proof_limit = min(_ELISION_ENUM_LIMIT, _ELISION_PROOF_BUDGET)  # 200_000
   if group_product * _product_size(vary_domains) > proof_limit:
       return False  # Over budget → treat as non-elidable (conservative)

   # Step 4: Iterate (retained × input × vary) space
   retained_iter = product(*retained_domains) if retained_domains else [()]
   for retained_values in retained_iter:
       retained_entry = dict(zip(retained_names, retained_values, strict=True))
       input_iter = product(*input_assignment_dimensions) if input_assignment_dimensions else [()]
       for input_assignments in input_iter:
           entry_values = dict(retained_entry)
           for partial_assignment in input_assignments:
               entry_values.update(partial_assignment)

           # Baseline scan (only first vary iteration)
           expected: tuple[Any, ...] | None = None
           vary_iter_values = product(*vary_domains) if vary_domains else [()]
           for vary_values in vary_iter_values:
               full_entry = dict(entry_values)
               full_entry.update(dict(zip(vary_names, vary_values, strict=True)))
               outcome = self._scan(full_entry, observed)
               if outcome is None:
                   return False  # Warm memory test failed
               if expected is None:
                   expected = outcome  # Capture first outcome as baseline
                   continue
               if outcome != expected:
                   return False  # Candidate affects observable → cannot elide

   return True  # All scans matched baseline for their (retained × input) → elidable
```

### 3.2 Enumeration Structure

The three-level nesting enumerates:

1. **Retained × Input Baseline** (lines 484–493):
  - For each (retained_values, input_assignments) pair, produce one `entry_values` dict
  - This baseline should be computed **once per candidate** and **reused across candidates** in the same round

2. **Vary (Hidden + Candidate)** (lines 495–506):
  - For each variation of hidden_stateful + candidate, scan and check outcome
  - **First outcome** becomes `expected`
  - **All subsequent outcomes** must equal `expected`
  - If any outcome differs, candidate affects the state → **not elidable**

### 3.3 Key Variables

- **`retained_names`**: tuple of tags from `observed` ∩ `retained` (the "reachable retained frontier")
- **`input_names`**: tuple of nondeterministic inputs that affect `candidate` through `observed`
- **`hidden_stateful`**: tuple of non-retained stateful tags reachable through `observed`, excluding candidate
- **`vary_names`**: hidden_stateful + (candidate,) — the tags whose variations we test
- **`observed`**: tuple of tags that form the "interface" between candidate and the rest of the state

---

## 4. `_scan` Method (lines 638–660)

### 4.1 Core Execution

```python
def _scan(
   self,
   entry_values: Mapping[str, Any],
   observed: tuple[str, ...],
) -> tuple[Any, ...] | None:
   # Create fresh kernel, initialize with entry values
   kernel = self._compiled.create_kernel()
   kernel.tags.update(entry_values)

   # Step once
   _step_compiled_kernel(self._compiled, kernel, dt=_DEFAULT_DT)

   # Extract observed outputs
   result = tuple(kernel.tags.get(name) for name in observed)

   # Optional: re-run with warm memory
   if self._warm_memory is not None:
       warm_kernel = self._compiled.create_kernel()
       warm_kernel.tags.update(entry_values)
       warm_kernel.memory.update(self._warm_memory)
       _step_compiled_kernel(self._compiled, warm_kernel, dt=_DEFAULT_DT)
       warm_result = tuple(warm_kernel.tags.get(name) for name in observed)
       if warm_result != result:
           return None  # Memory-dependent behavior detected → untestable

   return result
```

### 4.2 Warm Memory Re-scan Purpose

- **Initial scan**: Uses fresh kernel.memory (all defaults)
- **Warm memory scan**: Uses `self._warm_memory` (discovered state from pilot scans)
- **Purpose**: Detects hidden memory-dependent behavior (e.g., OTE oneshot instructions that store state in
kernel.memory)
- **Return None on mismatch**: If warm_memory changes the outcome, the tag is **not elidable** (cannot be safely
eliminated with abstract proof alone)

---

## 5. `_scoped_dependencies` (lines 596–636)

### 5.1 Purpose

Compute the **minimal set of retained and input tags** that affect the observed frontier for a specific candidate.
This is **per-candidate** because different candidates may have different upstream cones.

### 5.2 Algorithm (BFS Backward through Writers)

```python
def _scoped_dependencies(
   self,
   candidate: str,
   observed: tuple[str, ...],
   retained: frozenset[str],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
   retained_set = set(retained)
   observed_set = set(observed)

   # Initialize with observed tags
   retained_names = set(observed_set & retained_set)
   input_names: set[str] = set()
   hidden_stateful = set(observed_set - retained_set)

   # BFS backward: for each tag, find its writers
   queue: deque[str] = deque([candidate, *observed])
   visited: set[str] = set(queue)

   while queue:
       current = queue.popleft()
       if _is_fault_tag(current):
           continue

       # Find all rungs that WRITE to current
       for rung_idx in self._graph.writers_of.get(current, frozenset()):
           node = self._graph.rung_nodes[rung_idx]

           # For each tag READ by that rung (condition or data)
           for src in node.condition_reads | node.data_reads:
               if _is_fault_tag(src):
                   continue

               # Classify the source
               if src in self._nondeterministic_dims:
                   input_names.add(src)
                   continue

               if src in retained_set and self._is_retained_anchor(src):
                   retained_names.add(src)
                   continue

               # Hidden stateful: not retained, not ND input, not candidate
               if src in self._state_basis and src not in retained_set and src != candidate:
                   hidden_stateful.add(src)

               # Enqueue to continue BFS
               if src in visited:
                   continue
               visited.add(src)
               queue.append(src)

   return (
       tuple(sorted(retained_names)),
       tuple(sorted(input_names)),
       tuple(sorted(hidden_stateful)),
   )
```

### 5.3 Key Insight: Per-Candidate Variation

Each candidate may have a **different scoped dependency set**:

- **Candidate A**: May affect retained tags `{R1, R2}`, inputs `{I1}`, hidden `{H1}`
- **Candidate B**: May affect retained tags `{R1, R3}`, inputs `{I2}`, hidden `{H2}`

**Different candidates in the same round typically have different scoped dependencies** because:
1. Their upstream cones (via writers_of graph) differ
2. Their downstream uses (via readers_of) are distinct
3. Hidden stateful frontiers depend on what they feed into

This means **baseline scans cannot be trivially shared across all candidates** — but they *can* be shared among
candidates with **identical (retained_names, input_names, hidden_stateful)** tuples.

---

## 6. `_reachable_stateful_frontier` (lines 509–540)

### 6.1 Forward Reachability BFS

```python
def _reachable_stateful_frontier(
   self,
   candidate: str,
   retained: frozenset[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
   """Return (observed_retained, fallback_hidden) reachable from candidate."""
   reachable_retained: set[str] = set()
   reachable_hidden: set[str] = set()
   retained_set = set(retained)
   stateful_names = set(self._stateful_dims)
   visited: set[str] = {candidate}
   queue: deque[str] = deque([candidate])

   while queue:
       current = queue.popleft()
       if _is_fault_tag(current):
           continue

       # Find all rungs that READ from current
       for rung_idx in self._graph.readers_of.get(current, frozenset()):
           node = self._graph.rung_nodes[rung_idx]

           # For each tag WRITTEN by that rung
           for written_tag in node.writes:
               if _is_fault_tag(written_tag):
                   continue

               if written_tag in retained_set:
                   reachable_retained.add(written_tag)
                   continue

               if written_tag in stateful_names and written_tag != candidate:
                   reachable_hidden.add(written_tag)

               if written_tag in visited:
                   continue
               visited.add(written_tag)
               queue.append(written_tag)

   return tuple(sorted(reachable_retained)), tuple(sorted(reachable_hidden))
```

### 6.2 Purpose: Compute Observable Interface

- **Input**: candidate tag name, retained tag set
- **Output**: (observed_retained, fallback_hidden)
 - **observed_retained**: retained tags that candidate transitively reaches via readers_of
 - **fallback_hidden**: hidden stateful tags that candidate reaches (used if no retained observers)

### 6.3 Two-Level Observation Logic (lines 455–462)

```python
observed, fallback_hidden = self._reachable_stateful_frontier(candidate, retained)

if not observed:  # No retained tags observe the candidate
   # Check if hidden tags need to be observed
   sticky_hidden = tuple(
       name for name in fallback_hidden
       if self._hidden_entry_matters(name, retained)
   )
   if not sticky_hidden:
       return True  # Candidate affects nothing observable → ELIDABLE
   observed = sticky_hidden  # Use hidden tags as fallback observers
```

**Logic:**
1. If candidate reaches retained tags → those become the observed interface
2. If candidate **only** reaches hidden stateful tags:
  - Check if any hidden tag's entry value matters (via `_hidden_entry_matters`)
  - If yes, use those as observers
  - If no, candidate is **always elidable** (affects nothing observable)

---

## 7. `_hidden_entry_matters` (lines 542–586)

### 7.1 Purpose

Cache-backed check: **does the entry value of a hidden stateful tag affect its observable outputs?**

### 7.2 Algorithm

```python
def _hidden_entry_matters(self, tag_name: str, retained: frozenset[str]) -> bool:
   cache_key = (tag_name, retained)
   cached = self._entry_sensitive_cache.get(cache_key)
   if cached is not None:
       return cached

   # Compute upstream cone and retained intersections
   upstream = set(self._graph.upstream_slice(tag_name))
   cone = upstream | {tag_name}
   retained_names = tuple(sorted(set(retained) & upstream))
   input_names = tuple(sorted(upstream & set(self._nondeterministic_dims)))
   hidden_names = tuple(
       sorted((cone & set(self._stateful_dims)) - set(retained_names) - {tag_name})
   )

   # Check if enumerate within budget
   fixed_domains = (
       tuple(self._stateful_dims[name] for name in retained_names)
       + tuple(self._nondeterministic_dims[name] for name in input_names)
       + tuple(self._stateful_dims[name] for name in hidden_names)
   )
   tag_domain = self._stateful_dims[tag_name]
   if _product_size(fixed_domains + (tag_domain,)) > _ELISION_ENUM_LIMIT:
       # Over budget → conservatively assume entry matters
       self._entry_sensitive_cache[cache_key] = True
       return True

   # Enumerate: fix all others, vary tag_name, check if output changes
   fixed_names = retained_names + input_names + hidden_names
   fixed_iter = product(*fixed_domains) if fixed_domains else [()]
   for fixed_values in fixed_iter:
       base_entry = dict(zip(fixed_names, fixed_values, strict=True))
       expected: tuple[Any, ...] | None = None
       for tag_value in tag_domain:
           full_entry = dict(base_entry)
           full_entry[tag_name] = tag_value
           outcome = self._scan(full_entry, (tag_name,))  # Observe only the tag itself
           if outcome is None:
               self._entry_sensitive_cache[cache_key] = True
               return True
           if expected is None:
               expected = outcome
               continue
           if outcome != expected:
               # Varying tag_name changed its output → entry value matters
               self._entry_sensitive_cache[cache_key] = True
               return True

   self._entry_sensitive_cache[cache_key] = False
   return False
```

**Returns:**
- **True**: Tag's entry value affects its post-scan value (entry-sensitive) → must be observed
- **False**: Tag always produces the same output regardless of entry (entry-insensitive) → can be omitted from
observed

---

## 8. `_input_assignment_dimensions` (lines 419–452)

### 8.1 Purpose

Transform nondeterministic input names into enumeration dimensions, respecting **exclusive input groups** (encoder
families).

### 8.2 Exclusive Input Groups Concept

From `inputs.py`:

```python
@dataclass(frozen=True, slots=True)
class _ExclusiveInputGroup:
   target_name: str  # Tag that encodes the group (e.g., "Mode")
   members: tuple[str, ...]  # Input names (e.g., ("Auto", "Manual", "Config"))
   canonical_assignments: tuple[tuple[tuple[str, bool], ...], ...]
```

**Canonical assignments** for 3 members: `("Auto", "Manual", "Config")`

```python
(
   (("Auto", False), ("Manual", False), ("Config", False)),  # All off (default)
   (("Auto", True), ("Manual", False), ("Config", False)),   # Auto on
   (("Manual", True), ("Auto", False), ("Config", False)),   # Manual on
   (("Config", True), ("Auto", False), ("Manual", False)),   # Config on
)
```

**One-hot constraint**: Only one member can be True at a time → reduces combos from 2³=8 to 4.

### 8.3 Algorithm

```python
def _input_assignment_dimensions(
   self,
   input_names: tuple[str, ...],
) -> tuple[tuple[tuple[tuple[str, Any], ...], ...], ...]:
   """
   Returns a tuple of dimension tuples:
   dimension[i] = tuple of possible assignments for input i
   Each assignment is a tuple of (name, value) pairs

   Example for exclusive group {A, B}:
   (
       (("A", False), ("B", False)),  # Neither
       (("A", True), ("B", False)),   # A only
       (("B", True), ("A", False)),   # B only
   )

   Example for free input X with domain [0, 1]:
   (
       ((X, 0),),
       ((X, 1),),
   )
   """
   dimensions: list[tuple[tuple[tuple[str, Any], ...], ...]] = []
   live_inputs = set(input_names)
   seen_groups: set[int] = set()

   for name in sorted(input_names):
       group_index = self._exclusive_input_group_by_member.get(name)
       if group_index is not None:
           if group_index in seen_groups:
               continue  # Skip subsequent members of same group
           seen_groups.add(group_index)
           group = self._exclusive_input_groups[group_index]

           # Collect canonical options, filtered to live inputs
           options: list[tuple[tuple[str, Any], ...]] = []
           seen_options: set[tuple[tuple[str, Any], ...]] = set()
           for canonical in group.canonical_assignments:
               filtered = tuple(
                   (member, value) for member, value in canonical
                   if member in live_inputs
               )
               if filtered in seen_options:
                   continue
               seen_options.add(filtered)
               options.append(filtered)

           if options:
               dimensions.append(tuple(options))
           continue

       # Free input: enumerate its domain
       dimensions.append(
           tuple(((name, value),) for value in self._nondeterministic_dims[name])
       )

   return tuple(dimensions)
```

### 8.4 Example Combo Count

**Input scope**: `{Auto, Manual, Config, Threshold}`

1. **Exclusive group** {Auto, Manual, Config} with 3 canonical options
  - Contribution to combo count: 3
2. **Free input** Threshold with domain [0, 1, 2]
  - Contribution to combo count: 3

**Total input combos**: 3 × 3 = 9

---

## 9. `_ForcedTrueCoverage` and `_collect_forced_true_coverage` (lines 45–210)

### 9.1 Data Structure

```python
@dataclass(frozen=True, slots=True)
class _ForcedTrueCoverage:
   written_tags: frozenset[str]  # Tags modified by any kernel scan
   varied_tags: tuple[str, ...]  # Tags with enumerated domains
   truncated: bool = False  # True if enum space exceeded combo_limit
```

### 9.2 Purpose

**Discover which tags are actually written** by the compiled kernel, accounting for runtime behavior that's
invisible to static analysis.

- **Static writers**: tags with write instructions in the program graph
- **Dynamic writers**: tags modified by conditional logic, timers, counters, etc. discovered via pilot scans

### 9.3 Algorithm

```python
def _collect_forced_true_coverage(
   program: Program,
   graph: ProgramGraph,
   stateful_dims: Mapping[str, tuple[Any, ...]],
   nondeterministic_dims: Mapping[str, tuple[Any, ...]],
   *,
   compiled: CompiledKernel | None = None,
   combo_limit: int = _FORCED_TRUE_COMBO_LIMIT,  # 4096
) -> _ForcedTrueCoverage:
   forced_compiled = compiled or compile_kernel(program, force_rung_enable=True, blockless=True)

   # Determine which stateful/ND dimensions to vary
   domain_items = _coverage_domain_items(
       graph, stateful_dims, nondeterministic_dims, combo_limit=combo_limit
   )
   varied_tags = tuple(name for name, _domain in domain_items)
   combo_space = _product_size(tuple(domain for _name, domain in domain_items))
   truncated = (combo_space * 2) > combo_limit

   # Enumerate combinations with two seed profiles (default + alternate)
   written_tags: set[str] = set()
   domain_values = [domain for _name, domain in domain_items]
   remaining_budget = combo_limit

   for alternate in (False, True):
       if remaining_budget <= 0:
           break

       seed = _seed_profile(forced_compiled, alternate=alternate)
       combo_iter = product(*domain_values) if domain_values else [()]

       for combo in combo_iter:
           if remaining_budget <= 0:
               break

           kernel = forced_compiled.create_kernel()
           kernel.tags.update(seed)
           entry_values = dict(zip(varied_tags, combo, strict=True))
           kernel.tags.update(entry_values)

           # Snapshot before/after step
           before = {name: kernel.tags.get(name) for name in forced_compiled.referenced_tags}
           _step_compiled_kernel(forced_compiled, kernel, dt=_DEFAULT_DT)

           # Collect changed tags
           for name in forced_compiled.referenced_tags:
               if kernel.tags.get(name) != before.get(name):
                   written_tags.add(name)

           remaining_budget -= 1

   return _ForcedTrueCoverage(
       written_tags=frozenset(written_tags),
       varied_tags=varied_tags,
       truncated=truncated,
   )
```

### 9.4 Used in `__init__`

```python
self._coverage = _collect_forced_true_coverage(...)
static_writers = set(graph.writers_of) & set(self._stateful_dims)
dynamic_writers = set(self._coverage.written_tags) & set(self._stateful_dims)
self._written_tags = frozenset(static_writers | dynamic_writers)
```

---

## 10. Data Flow Summary

```
_can_elide(candidate, retained)
├─> _reachable_stateful_frontier(candidate, retained)
│   └─> BFS from candidate via readers_of graph
│       → (observed_retained, fallback_hidden)
│
├─> (if no observed, check _hidden_entry_matters)
│   └─> For each hidden tag: enumerate (retained × input × hidden_values)
│       → Does tag's entry value affect its post-scan value?
│       → Cached to avoid re-computation
│
├─> _scoped_dependencies(candidate, observed, retained)
│   └─> BFS backward from (candidate + observed) via writers_of graph
│       → (retained_names, input_names, hidden_stateful)
│
└─> Enumerate: product(retained_domains × input_assignments × vary_domains)
   ├─> For each (retained_values, input_assignments) pair:
   │   └─> retained_entry = dict(zip(retained_names, retained_values))
   │       entry_values = retained_entry + input_assignments
   │       expected = None
   │
   │   ├─> For each (vary_values) in product(vary_domains):
   │   │   └─> full_entry = entry_values + vary_values
   │   │       outcome = _scan(full_entry, observed)
   │   │       if outcome is None:
   │   │           return False  # Warm memory test failed
   │   │       if expected is None:
   │   │           expected = outcome
   │   │       else if outcome != expected:
   │   │           return False  # Varied entry changed outcome
   │
   └─> return True if all scans passed
```

---

## 11. Opportunity for Shared Baseline Batch Proof Optimization

### 11.1 Current Redundancy

For each candidate in a round:

1. **Compute scoped deps** → (retained_names_i, input_names_i, hidden_stateful_i)
2. **Enumerate (retained × input) combos**:
  - `N_retained_i = product_size(retained_domains_i)`
  - `N_input_i = combo_count_i`
  - **Total combos**: `N_retained_i × N_input_i`
3. **For each combo**: Create kernel, update tags, step once → **this is the baseline scan**
4. **For each combo**: Vary (hidden + candidate), scan and compare

**Problem**: When two candidates have the **same (retained_names, input_names, hidden_stateful)** tuple, their
baseline scans are identical but executed separately.

### 11.2 Optimization Strategy

**Pre-compute baseline registry**:

1. **Before processing candidates**: For each unique scoped-dependency tuple, pre-compute all baseline scans
  ```python
  baseline_registry = {}
  # Key: (tuple(retained_names), tuple(input_names), tuple(hidden_stateful))
  # Value: Dict[scoped_entry] → tuple[observed_values]
  ```

2. **For each candidate**:
  - Compute its scoped deps
  - Look up pre-computed baselines
  - Use cached results for all (retained × input) combos
  - Only vary the candidate + hidden, comparing against cached baselines

### 11.3 Benefits

- **Reduced kernel steps**: Baseline scans run once, not per-candidate
- **Cacheable across rounds**: A baseline for (R1, I2) in round N can serve round N+1 if R1 and I2 are still in
scope
- **Parallelizable**: Baseline computation can be parallelized independently of candidate proofs
- **Measurable**: Track baseline cache hits/misses for optimization metrics

### 11.4 Implementation Sketch

```python
class _BaselineScanCache:
   """Shared baseline scan results across candidates."""

   def __init__(self):
       # Key: (tuple[retained_names], tuple[input_names])
       # Value: Dict[entry_key] → tuple[observed_values]
       self.registry: dict[tuple, dict] = {}

   def lookup_or_compute(
       self,
       retained_names: tuple[str, ...],
       input_names: tuple[str, ...],
       input_assignment_dimensions,
       stateful_dims,
       nondeterministic_dims,
       elider,  # _ConcreteStateElider instance
   ) -> dict:
       """Return cached baselines or compute + cache them."""
       cache_key = (retained_names, input_names)
       if cache_key in self.registry:
           return self.registry[cache_key]

       baselines = {}
       retained_domains = tuple(stateful_dims[name] for name in retained_names)
       retained_iter = product(*retained_domains) if retained_domains else [()]

       for retained_values in retained_iter:
           retained_entry = dict(zip(retained_names, retained_values, strict=True))
           input_iter = product(*input_assignment_dimensions) if input_assignment_dimensions else [()]

           for input_assignments in input_iter:
               entry_values = dict(retained_entry)
               for partial_assignment in input_assignments:
                   entry_values.update(partial_assignment)

               entry_key = tuple(sorted(entry_values.items()))

               # Scan once with observed=(hidden + candidate) set
               # But we don't know observed yet...
               # ISSUE: observed depends on candidate's frontier

       self.registry[cache_key] = baselines
       return baselines
```

### 11.5 Challenge: Observed Set Dependency

The **baseline outcome tuple** depends on what's being observed, which is candidate-specific:

- **Candidate A** observes {R1, R2, H1} → outcome = (r1_val, r2_val, h1_val)
- **Candidate B** observes {R1, H2} → outcome = (r1_val, h2_val)

**Different observed sets → different outcome tuples → cannot trivially share.**

**Solution**: Pre-compute baselines with a **union of all observed sets** for a given (retained, input) scope:

```python
# Collect all observed sets for this (retained_names, input_names) scope
all_observed = set()
for candidate in candidates:
   _, _, hidden = _scoped_dependencies(candidate, ?, retained)
   observed, _ = _reachable_stateful_frontier(candidate, retained)
   all_observed.update(observed | hidden)

# Pre-compute baseline with max observed set
baseline_outcome = _scan(entry_values, tuple(sorted(all_observed)))

# For each candidate, extract subset of outcome tuple
candidate_outcome = tuple(baseline_outcome[i] for i in indices_of_candidate_observed_in_max)
```

---

## 12. Relationship Between Components: Call Graph

```
_pass_concrete_batch
├─> _ConcreteStateElider.__init__
│   ├─> _collect_forced_true_coverage
│   ├─> _detect_exclusive_input_groups
│   └─> _find_continued_source_tags
│
└─> LOOP: while changed
   └─> for each candidate
       └─> _can_elide(candidate, retained)
           ├─> _reachable_stateful_frontier(candidate, retained)
           │   └─> BFS via readers_of
           │
           ├─> (if no observed)
           │   └─> _hidden_entry_matters(hidden_tag, retained)
           │       └─> _scan(full_entry, (tag_name,))
           │
           ├─> _scoped_dependencies(candidate, observed, retained)
           │   └─> BFS via writers_of
           │
           ├─> _input_assignment_dimensions(input_names)
           │   └─> Transform via exclusive_input_groups
           │
           └─> ENUMERATE: (retained × input × vary)
               └─> _scan(full_entry, observed)
                   └─> kernel.step + optional warm_memory rescan
```

---

## 13. Key Invariants

1. **Soundness**: If `_can_elide(candidate, retained)` returns True, candidate's cross-scan value can be safely
ignored in the BFS state key.

2. **Conservative budgeting**: If proof space exceeds `_ELISION_ENUM_LIMIT`, return False (don't attempt, assume
non-elidable).

3. **Warm memory sensitivity**: If kernel memory affects outcomes, return None from `_scan` → candidate is not
elidable.

4. **Round iteration necessity**: Retained set shrinks each round, so scoped dependencies change → must re-compute
per round.

5. **Caching of `_hidden_entry_matters`**: Results keyed by (tag_name, retained_frozenset) are stable within a
round.

---

## 14. Summary Table

| Method | Lines | Input | Output | Purpose |
|--------|-------|-------|--------|---------|
| `__init__` | 213–308 | Program, dims | Initialized elider | Setup: pilot scans, coverage, input groups, memory
state |
| `_can_elide` | 454–507 | candidate, retained | bool | Main proof: enumerate (R×I×V) and check consistency |
| `_reachable_stateful_frontier` | 509–540 | candidate, retained | (observed_ret, fallback_hidden) | BFS forward
via readers_of to find observable interface |
| `_scoped_dependencies` | 596–636 | candidate, observed, retained | (retained_names, input_names, hidden_stat) |
BFS backward via writers_of to compute minimal scope |
| `_scan` | 638–660 | entry_values, observed | tuple or None | Execute one kernel step, extract observed outputs,
optionally rescan with warm memory |
| `_hidden_entry_matters` | 542–586 | tag_name, retained | bool | Cached: does tag's entry value affect its
output? |
| `_input_assignment_dimensions` | 419–452 | input_names | tuple of dimension tuples | Transform inputs into
exclusive-group-aware enumeration structure |
| `_pass_concrete_batch` | 668–714 | _ElisionContext | None (modifies ctx.elided, ctx.stateful_dims) | Pipeline
entry: orchestrate round-based elision with batch removal |

---

## 15. Recommended Next Steps for Shared Baseline Implementation

1. **Define `_BaselineScanKey`**: Tuple of (retained_names, input_names, union_of_observed)
2. **Create `_BaselineScanRegistry`**: Dict[_BaselineScanKey] → Dict[entry_dict → outcome_tuple]
3. **Pre-compute phase**: Before candidate loop, enumerate all (R × I) and run baseline scans once
4. **Candidate phase**: Look up pre-computed baselines, extract observed subsets, vary + compare
5. **Metrics**: Track cache statistics (lookups, hits, miss rate)
6. **Testing**: Verify round-by-round behavior matches original (deterministic equivalence)