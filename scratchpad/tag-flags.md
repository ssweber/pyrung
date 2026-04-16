# Tag flags: design note

Pyrung tags carry metadata flags that fall into two tiers with distinct
purposes. Validators enforce semantic flags; a future HMI/DataView
surface reads the presentation flag. No flag serves both roles.

## Semantic flags (validator-enforced)

These flags change what a static validator concludes about the program.
Each flag corresponds to an enforceable claim about how the tag is
written.

### readonly  *(implemented on Tag, needs validator)*

Zero writers after startup. The tag is initialized from its declared
default at PLC power-on and never written again — not by the ladder,
not by any HMI surface, not by any external source.

Already implemented:
- `Tag(..., readonly=True)` field on the frozen dataclass.
- Click comment parser: `[readonly]` bracket token.

Needs implementation:
- Validator: flag any write site targeting a `readonly=True` tag as
  `CORE_READONLY_WRITE`.
- Stuck-bits validator: skip `readonly` tags — they're frozen
  constants by design, not stuck-bit candidates.

Note: this is weaker than IEC `CONSTANT` (which is truly immutable,
compile-time). Our `readonly` permits the power-on initialization
write, then freezes.

### choices=  *(implemented on Tag, needs validator)*

Value constraint. The tag's `choices` field is a `ChoiceMap`
(`dict[int | float | str, str]`) — a value→label mapping. The key set
defines the allowed values.

Already implemented:
- `Tag(..., choices={0: "Off", 1: "On"})` or via `IntEnum` subclass.
- Click comment parser: `[choices=Off:0|On:1]` (pipe-separated
  `LABEL:VALUE` pairs).

Needs implementation:
- Static validator: check every literal-value write site against the
  key set. Flag as `CORE_CHOICES_VIOLATION`.
- Runtime: bounds check on non-literal writes if not caught statically.

### external  *(not yet implemented)*

The ladder is not responsible for writing this tag. Something outside
the ladder — HMI, SCADA, comms link, Python compute block — is the
writer.

Needs implementation:
- `Tag(..., external=True)` field.
- Click comment parser: `[external]` bracket token.
- Stuck-bits validator: treat the "reset side" (or "latch side",
  symmetrically) as satisfied. An `external` tag with a latch but no
  reset in the ladder is not `CORE_STUCK_HIGH` — the reset happens
  externally by design.
- `plc.recovers()`: return a distinct `'external'` chain mode rather
  than false — "not recoverable by ladder alone, but declared
  externally driven."
- Duplicate-out validator: if the ladder also writes an `external` tag,
  emit a distinct finding code (not `CORE_CONFLICTING_OUTPUT`) — this
  is usually ladder-initializes-then-HMI-owns, which is legitimate but
  worth surfacing.

Naming caveat: IEC 61131-3 uses `VAR_EXTERNAL` to mean "declared in
another scope" — a linker concept, not a write-responsibility concept.
Our `external` is unrelated. Document prominently to avoid confusion
for IEC-first engineers.

### final  *(not yet implemented)*

Exactly one writer in the ladder. Intended for int/analog tags used as
filtered values, averages, or any accumulator whose correctness
depends on being the sole authoritative source.

Needs implementation:
- `Tag(..., final=True)` field.
- Click comment parser: `[final]` bracket token.
- Validator: count write sites targeting a `final` tag. Flag as
  `CORE_FINAL_MULTIPLE_WRITERS` if > 1, regardless of mutual
  exclusivity between sites. This is stricter than `duplicate_out`,
  which allows mutually-exclusive multi-writers.
- Typical use: `with Rung(AlwaysOn): calc(Val + 1, Val)` — declaring
  `Val` as `final` asserts that no other rung should ever write `Val`.

v1 picks the strict interpretation: no exemption for writing the tag's
declared default value. Loosens only if real code demands it.

No IEC equivalent — single-writer discipline isn't an IEC notion.

## Presentation flag

### public  *(implemented)*

Part of the intended API surface. Setpoints, mode commands, alarms,
key status bits — the tags engineers and operators are supposed to
interact with.

- Tag field: `Tag(..., public=True)` on the frozen dataclass.
- Click comment parser: `[public]` bracket token.
- DAP DataView: "P" badge next to tag name; "☐ Public" filter
  checkbox hides all non-public tags when checked.
- No validator consequence.

The absence of `public` means "plumbing" — the tag is ladder
implementation detail. Plumbing tags are not hidden, not forbidden to
interact with, not validated any differently. They're just not the
featured interface. Same distinction as Python's `foo` vs `_foo`
convention.

There is **no** `private` or `internal` flag. Privacy is the
default; publicness is the opt-in. One bit of information, one place
it lives.

## Mutual exclusivity  *(enforce in Tag.__post_init__)*

- `readonly` and `final` are mutually exclusive: `readonly` means zero
  writers, `final` means exactly one. `Tag(readonly=True, final=True)`
  should raise at construction time.
- `readonly` and `external` are mutually exclusive: `readonly` means
  nothing writes it, `external` means something outside the ladder
  writes it.
- `external` and `final` may combine: "exactly one writer in the
  ladder AND also written externally" is a coherent (if unusual)
  declaration — the ladder contributes one write site, the external
  source contributes others, and `duplicate_out` should stay quiet
  about the combination.
- `public` combines freely with any semantic flag.

## Click comment convention

All flags round-trip through the Click ladder comment parser
(`parse_tag_meta` / `format_tag_meta` in `click/tag_map/_parsers.py`).
Engineers write flags in the tag's comment field using bracket syntax;
the parser lifts them into a `TagMeta` dataclass that feeds `Tag(...)`
construction:

    [readonly]                -> TagMeta(readonly=True)
    [choices=Off:0|On:1]      -> TagMeta(choices={0: "Off", 1: "On"})
    [readonly, choices=A:1|B:2] -> TagMeta(readonly=True, choices={1: "A", 2: "B"})

Already implemented: `[readonly]` and `[choices=LABEL:VALUE|...]`.
Add `[external]` and `[final]` to the parser in the same pass.
`[public]` waits until a surface uses it.

## The bar for future flags

A new semantic flag earns its slot only if it changes what a validator
concludes about a program. "Claim without enforcement" is not a flag —
it belongs in the tag description string or a generic metadata dict.

A new presentation flag earns its slot only if some surface (DataView,
HMI codegen, graph view) has a distinct rendering mode that isn't
covered by the existing set.

Four semantic flags and one presentation flag is the current ceiling.
Hold the line.

Reject, in particular:

- `internal`, `private`, `hide` — redundant with absence of `public`.
- `computed`, `calc` — redundant with `external` (both mean "ladder
  isn't the writer"; the source of the write doesn't matter to
  validators).
- A stricter `final` variant meaning "no conflicting writers" —
  redundant with `duplicate_out`.

## Implementation order

1. Add `external` and `final` fields to `Tag` dataclass + typed
   constructors (`Bool`, `Int`, etc.). Enforce mutual exclusivity in
   `__post_init__`.
2. Extend `TagMeta` and Click comment parser for `[external]` and
   `[final]` tokens.
3. `CORE_READONLY_WRITE` validator — flag write sites targeting
   `readonly=True` tags.
4. Stuck-bits validator — skip `readonly` tags.
5. `CORE_CHOICES_VIOLATION` validator — static check of literal writes
   against `choices` key set.
6. `external` support in stuck-bits validator + `recovers()`.
7. `CORE_FINAL_MULTIPLE_WRITERS` validator.

## Out of scope for this note

- Cross-system validation (HMI config + ladder together) that would
  give a privacy flag real enforcement teeth — deferred until pyrung
  grows that surface.
- CODESYS attribute-pragma codegen backend — deferred; our flag names
  optimize for Click-plus-Python-developer intuition today, and the
  pragma mapping is a future translation concern.