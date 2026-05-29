# Fix Real tag domain inference in prover

## Context

The prover's domain inference uses `range(int(min), int(max)+1)` to enumerate values for bounded tags. This only produces integers. For Real (float) tags with whole-number bounds (`min=0.0, max=100.0`) this accidentally works — all integers are valid floats that straddle comparison boundaries. But for fractional bounds (`min=0.5, max=99.5`) it silently truncates, and for large ranges (>1000) it returns `None`, leaving the tag stuck at 0.0 and blocking exploration.

## Fix

Guard `range()` with `int(min) == min and int(max) == max`. When bounds are whole numbers, keep the existing integer enumeration. When bounds are fractional, fall through to the partition path which seeds `{min, max}` and uses ±1 expansion.

Three sites to fix, plus a partition-seed addition for the fractional fallback in `_extract_value_domain`.

## Changes

### classify.py

**1. `_declared_domain()` (line 414-418)** — add guard before `range()`:

```python
# Before range(), reject non-integer bounds:
if int(tag.min) != tag.min or int(tag.max) != tag.max:
    return None
```

**2. `_extract_value_domain()` (lines 1288-1296)** — restructure the min/max block. When bounds are fractional and there are no comparison literals, seed `literals` with `{min, max}` so the partition expansion at lines 1304-1316 produces a working domain instead of returning `range()` or `None`:

```python
if tag.min is not None and tag.max is not None:
    domain_size = tag.max - tag.min + 1
    if literals:
        literals.add(tag.min)
        literals.add(tag.max)
    elif int(tag.min) != tag.min or int(tag.max) != tag.max:
        literals.add(tag.min)
        literals.add(tag.max)
    elif domain_size > 1000:
        return None
    else:
        return tuple(range(int(tag.min), int(tag.max) + 1))
```

**3. Docstring (line 19)** — update domain inference stack description:

```
3. ``min=`` / ``max=`` metadata → integer range (Int/Dint/Word; capped at 1000)
   or partition seed (Real with non-integer bounds)
```

### passes.py

**4. `_pass_pilot_sweep()` (lines 794-797)** — same guard:

```python
elif tag.min is not None and tag.max is not None:
    if int(tag.min) != tag.min or int(tag.max) != tag.max:
        domain = (tag.min, tag.max)
    else:
        range_size = int(tag.max - tag.min + 1)
        if range_size <= 1000:
            domain = tuple(range(int(tag.min), int(tag.max) + 1))
```

### test_prove_value_domain_extraction.py

Add `Real` to imports and 4 new test methods in `TestValueDomainExtraction`:

1. **Real with whole-number bounds** — still gets integer range (no regression)
2. **Real with fractional bounds + comparison** — gets partition domain containing min, max, and comparison boundary; all values within bounds
3. **Real with fractional bounds, no comparison** — seeds min/max, gets a domain (not Intractable)
4. **Real with large fractional range + comparison** — partition not None, small domain

## Verification

```
make test-prove
make lint
```
