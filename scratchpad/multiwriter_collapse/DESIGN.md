# Multi-writer route collapse — design (scratchpad, unbuilt)

Follow-on to commit `57a1b60` (OR-arm collapse). Other agents are in the tree;
this is design + validated prototype only, **no source edits yet**.

## The boundary

`enumerate_trace_choices` consults the OR-arm collapse (`_or_ambiguity_over_inputs`)
**only when `not multi_writer`**. A Bool tag with ≥2 viable *writers* always
surfaces a choice — even when one writer is gated by directly-steerable inputs.

## Probe results (`probe_multiwriter.py`, `ground_truth.py`)

| Program | writers | today | correct behavior |
|---|---|---|---|
| **P1 multi-latch** (`latch(Cmd)` on `Manual`; `latch(Cmd)` on `State==5`) | 1 steerable + 1 internal, both retentive | ambiguous | **collapse → Manual latch** |
| **P2 multi-out** (`out(Cmd)` on `Manual`; `out(Cmd)` on `Auto∧State==5`) | both OTE | ambiguous | **stay surfaced** (see clobber) |
| **P3 Burner** (`latch(Cmd)` on `ProdMode`; on `MaintMode`) | both internal coils | ambiguous | **stay surfaced** (contract) |

## The clobber is a non-retentive duplicate-coil phenomenon (not a trace bug)

Ground truth, hold `Manual` only:
- P1 latch → `Cmd=True` (sticks). validator: **no conflict**.
- P2 out → `Cmd=False` (the later `out(Cmd)` re-drives it). validator: **flags `Cmd`** (`CORE_CONFLICTING_OUTPUT`, last-writer-wins stomping).

So non-retentive multi-writer is, by definition, a *conflict the `duplicate_out`
validator rejects* — not a route PILOT should plan. The legitimate multi-writer
case is **retentive** (latch / copy-into-held), which has no clobber.

End-to-end PILOT is already honest on the dead route: `pilot_how(choice=1)` on P2
reports `reachable=False` (verify/replay catches the clobber). The only latent
gap is internal: the static trace tree through a non-last `out` claims
`Manual→Cmd`. Optional hardening — make viability shadow-aware (drop non-last OTE
writers for the end-of-scan target) so trace never offers a dead route — but it
polishes a program the validator already rejects. **Not required for collapse.**

## Design: two gates, mirroring the OR-arm fix

In `enumerate_trace_choices` (or the `choice is None` branch of
`_prepare_trace_choice`), when `multi_writer`, auto-resolve onto a writer that is
both:

1. **Retentive** — `tag not in pdg.rung_nodes[ri].ote_writes` (latch/SET or
   copy/calc into a held register). Establishing it is not clobbered. This is the
   exact distinction trace's preserve logic already uses.
2. **Input-gated** — `_arm_fully_steerable(writer_condition, tag, steerable)`,
   the *same* recursive predicate the OR-arm fix uses. NOT merely transitively
   reachable (that is what would wrongly auto-resolve Burner — ProdMode is
   reachable via ProdCmd but is an internal coil).

If ≥1 writer passes both → lock the cheapest (`_trace_score`) and proceed
(the existing `len(choices)==1` "take it" path). If none → surface (Burner /
duplicate-coil stay choices, available via `choice=`).

Validated: with these gates, P1 collapses (`choice` auto = Manual latch,
reachable, replays `{'Manual': True}`); P2 and P3 return `None` → surfaced.

## RESOLVED — `choice=` is retired; see `CHOICE_REDESIGN.md`

The retentive+input-gated test is a *structural proxy* for "this route is a
trivial default, not a material commitment." It works, but the proxy was leaky
(the Burner stays surfaced for the wrong reason). The conversation reframed the
whole surface decision: **`how()` never asks — it picks a deterministic default,
reports the route it took, and the engineer redirects with `avoid=`/`via=`.**
The default-selection logic above is *reused* (it picks WHICH route is default),
but "collapse vs surface" becomes "default vs pivot." Full design + the new
`Path.route` shape: **`CHOICE_REDESIGN.md`**.
