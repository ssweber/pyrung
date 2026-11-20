# Parse operands once: the shape Sam agreed (2026-11-20, through the planner)

This is the direction, not a brief. The planner read only the top of `core/instruction/operands.py`. Check the fit against `execute()` and the kernel before building. If the shape does not hold, go to the planner at a stop.

1. The operand declarations parse. `Operand.fits(value) -> bool` becomes `parse(call, arg, value) -> T`. It returns a small frozen object, or raises the refusal that it already words. The `_operands` table and its messages stay.
2. The results are a closed set of frozen dataclasses, one for each thing an operand can be: a tag, a pointer, a constant, a range. `immediate` is a field of the result, not a wrapper around it. Unions such as `Source` and `Dest` name what a field takes.
3. An instruction holds only parsed objects. The builder is the one place where raw arguments come in. The type checker holds that line, so `execute()`, the kernel and the CLICK checks do not check again.
4. Each consumer matches on the union and ends with `assert_never`. A new kind of operand then fails the type check wherever it is not handled.

The parse holds what all three consumers need and what is fixed when the program is built: the kind, the tag type, a static count, and the result of a rule across two fields. A value read at scan time stays with the scan. Every CLICK rule stays in laddercodec.

Leave out:
- a schema library;
- a `validated` flag, or a `NewType` over the raw value;
- guards in `execute()` that repeat the parse.

Landing: "It's always easier to lock things down once we know the shape" (Sam). Settle the result types on one family first. Copy is the one, because it has all three consumers. Then sweep the other families from it. Afterwards, one test holds the line: no `isinstance` on `IndirectRef`, `BlockRange` or `ImmediateRef` outside `operands.py`.

The roadmap item "Parse operands once, in the builder" points here.
