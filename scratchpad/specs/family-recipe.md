# Recipe: move an instruction family's CLICK checks to laddercodec

laddercodec holds the rules of CLICK's instruction dialogs. pyrung builds the laddercodec
instruction that its export would write, and reports each value that laddercodec refuses.
This recipe moves one family of instructions onto that path. A family is the set of pyrung
calls that laddercodec checks with the same classes, such as `forloop()` and laddercodec's
`ForLoop`.

Three families are done:

- Copy: `copy()`, `blockcopy()`, `fill()` and the pack and unpack calls. The move is in
  #102 (122d7c0) and the caret in #103 (46f662f, c67159c, 1aa90ae). Read
  `families/copy.py` for a large family.
- For: `forloop()`, in commit 2e0e941 on branch `bob/family-scaffold`.
- Search: `search()`, in commit c3cfa4c on the same branch. Its call takes keyword
  operands, and one argument holds two attributes. Read it for a family that is not a
  plain list of positional operands.

The examples below come from the For and Search commits unless they say otherwise. File names
without a directory are in `src/pyrung/click/validation/`.

## What you edit

- `families/<family>.py`, which is new and declares the family.
- `legacy/<family>.py`, which you delete. It holds pyrung's own checks of the family, its
  codes and its rows of the shared tables.
- `tests/click/test_<family>_validation.py`, which is new.
- The pyrung instruction classes of the family, in `src/pyrung/core/instruction/`.
- The tests and docs that name the family's old codes.
- `CHANGELOG.md`.

Validation imports every module in `families/` and in `legacy/`. So your branch edits no
list, and two family branches conflict only in `CHANGELOG.md`. In a trial, branches for
the timers, shift, drums and `send()` each added a `families/` module and deleted a
`legacy/` module. Every merge of three or four of them conflicted only in `CHANGELOG.md`,
and the merged tree passed the shared tests.

Do not edit these shared files. If the family needs a change to one of them, stop and tell
the orchestrator.

- `fields.py`, `checks.py`, `families/__init__.py` and `legacy/__init__.py`.
- `hardware.py`, `portability.py`, `findings.py` and `__init__.py` in
  `click/validation/`.
- `core/validation/render.py`.
- `tests/core/test_instruction_tables.py` and `tests/click/test_families.py`.
- The bank tables in `click/capabilities.py`.

## Who owns the shared codes

Some codes do not belong to one family. No family branch deletes them.

- `CLK_BANK_UNRESOLVED` stays in pyrung. `evaluate_family` reports it for a tag or range
  that has no CLICK address, because laddercodec has nothing to check then.
- `CLK_COPY_RANGE_NOT_CONSECUTIVE` stays in pyrung. `evaluate_family` reports it for a
  family's `ranges`.
- `CLK_PTR_CONTEXT_ONLY_COPY` and `CLK_PTR_POINTER_MUST_BE_DS` stay in pyrung. They cover
  conditions and the families in `legacy/`. Validation drops one of them where laddercodec
  reports the same fault on that operand. Its advice joins laddercodec's finding.
- `CLK_BANK_NOT_WRITABLE`, `CLK_BANK_WRONG_ROLE` and `CLK_TIMER_PAIR_MISMATCH` belong to the
  checks in `hardware.py`. Those checks read their rows from the modules in `legacy/`, and
  from `timer_pairs` of a family. When you delete your module, its rows go with it, and the
  checks stop reporting the code for your family.
- `CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED` belongs to the immediate check in
  `portability.py`.

The change after the last family deletes those checks and codes, with the bank tables. The
orchestrator owns that change.

## 1. Find the classes and the fields

List the pyrung instruction classes of the family and the attributes that hold each
argument of the call. List laddercodec's classes and their fields:

```python
import dataclasses, laddercodec
[f.name for f in dataclasses.fields(laddercodec.Search)]
# ['table_start', 'table_end', 'value', 'result', 'found', 'op', 'continuous', 'oneshot']
```

laddercodec's codes are in `laddercodec.Code`. The pyrung checks and codes that the family
replaces are in `legacy/<family>.py`. Its `CODES` lists the codes, and its `CHECKS` lists
the checks.

pyrung's `SearchInstruction` holds the table in `search_range`, the value in `value` and
the operator in `condition`. It holds the keywords in `result`, `found`, `continuous` and
`oneshot`. `legacy/search.py` held `_evaluate_search_banks`, with the codes
`CLK_SEARCH_RANGE_BANK` and `CLK_SEARCH_RESULT_NOT_DS`. It also held Search's row of
`WRITE_TARGET_FIELDS`.

## 2. Declare the call on each class

A finding prints the author's call, such as `search(DS1..DS10 == 5, result=Slot,
found=C1)`. Each class declares its call in four class attributes. The comment above them
in `core/instruction/base.py` says what each holds.

- `_call_operands` pairs the DSL's name for each positional argument with the attribute
  that holds it.
- `_call_keyword_operands` pairs the name of each keyword argument that holds a tag, a
  range or a number with its attribute.
- `_call_keywords` names the flags and options. It defaults to `("oneshot",)`. A flag
  shows as `True` when it is set. An option shows its value, such as `unit='seconds'`. The
  call leaves out a flag or an option that holds the default of the class's constructor.
- `_call_chained` pairs the name of each chained call, such as `.reset()`, with the
  attribute that holds its condition.

The For family (`core/instruction/control.py`):

```python
    _call_operands = (("count", "count"),)
```

The Search family (`core/instruction/advanced.py`):

```python
    _call_operands = (("comparison", ("search_range", "value")),)
    _call_keyword_operands = (("result", "result"), ("found", "found"))
    _call_keywords = ("continuous", "oneshot")

    def _call_argument(self, name: str, text: Callable[[Any], str]) -> str:
        """Return the comparison as the call writes it, such as ``DS1..DS10 >= 100``."""
        return f"{text(self.search_range)} {self.condition} {text(self.value)}"
```

An argument that holds several attributes names them in a tuple. It shows as the text of
the class's `_call_argument()`. Without one, it shows as the structure whose fields the
attributes hold. A timer's UDT is one: `(("timer", ("done_bit", "accumulator")),)` shows
the structure's name, such as `Timer` or `Fill[2]`.

Shift's clock and reset are chained calls:
`_call_chained = (("clock", "clock_condition"), ("reset", "reset_condition"))`.

The coil, Calc, timer and counter calls have a special case in `render.py`. Leave it. A
family that declares one of these calls keeps the frame of every other finding, and the
family's own findings show the declared call. The change after the last family deletes
these special cases.

### When the declarations do not fit

Declare what you can, and override a hook of the family's `Family` for the rest. The hooks
live in your module, so you edit no shared code.

- `operands(instruction)` returns an `Operand` for each argument that holds tags. Each
  gives the argument, its plain word, and each attribute with the value that the author
  wrote. The `CLK_BANK_UNRESOLVED` check, laddercodec's names and the caret's label read
  it. Without it, `declared_operands` reads `_call_operands`, `_call_keyword_operands` and
  `_call_chained`.
- `call_text(instruction)` returns the `Call` that the family's findings print, with the
  place of each argument. Build it with `call_of(verb, arguments)` from `render.py`. Give
  a list argument as a list of item texts, such as `("outputs", ["Out1", "X009"],
  "keyword")`. The call then keeps the place of each item. Without the hook,
  `declared_call` reads the declarations, and a list attribute shows its items.
- `place(refusal)` returns the `Place` that a laddercodec refusal marks: an argument, and
  the index of an item of a list argument. Without it, `default_place` looks the field up
  in `arguments`. laddercodec names one item of a list with its index, such as
  `Drum.outputs[1]`, and the default marks item 1 of the argument that holds
  `Drum.outputs`. A refusal of a pair, such as two outputs that are the same bit, names
  its first field, and the default marks that one. Override `place` to mark another.

A hook runs with no guard, so an exception in it stops validation. A tag's `==` and `!=`
build a condition, and a condition raises when Python asks if it is true. So never compare
a value that can be a tag with `==` or `!=`. Check its type with `isinstance` first. Test
each hook with a tag in every argument that can hold one.

`classes` still names every attribute that the instruction writes, even when you override
`operands`. `tests/core/test_instruction_tables.py` reads `classes` and not the hook. For
`send()` and `receive()`, that means the status bits, and `receive()`'s `dest`.

`send()` needs both `operands` and `call_text`. Its target and remote start hold no tag. A
status bit that the author leaves off holds a hidden tag, which would get a false
`CLK_BANK_UNRESOLVED`. This override of `operands` drops those tags. It imports
`is_modbus_status_tag` from `pyrung.core.instruction.send_receive`, as the export does:

```python
def _send_operands(instruction: Any) -> list[Operand]:
    operands = [Operand("source", "source", (("source", instruction.source),))]
    for name in ("sending", "success", "error", "exception_response"):
        bit = getattr(instruction, name)
        # A status bit that the author left off holds a hidden tag, which CLICK never sees.
        if not is_modbus_status_tag(bit.name):
            operands.append(Operand(name, f"{name.replace('_', ' ')} flag", ((name, bit),)))
    return operands
```

`send()`'s laddercodec class takes a `ModbusTcpTarget` or a `ModbusRtuTarget`, and the
target refuses its own fields, such as `ModbusTcpTarget.port`. So `arguments` maps every
field of both target classes. `_build` builds the target first. If laddercodec refuses the
target, `_build` returns those refusals, because there is no target to build the `Send`
with.

`tests/click/test_family_hooks.py` declares `send()` with the `operands` and `call_text`
hooks, and `event_drum()` with all three. Copy it when your call has quirks like these. A
drum's `.jump(cond, step=...)` holds its step inside a chained call, which `call_of` cannot
write. Build that `Call` yourself, with a span for each argument and for each item of a
list.

## 3. Write the declaration

Create `families/<family>.py` with a `FAMILY`. This is `families/forloop.py` without its
imports:

```python
# The argument of the pyrung call that holds each field of laddercodec's ForLoop.
_FIELD_ARGUMENTS: dict[str, int | str] = {
    "ForLoop.count": 0,
    "ForLoop.oneshot": "oneshot",
}

# pyrung's advice for a code, where the advice names pyrung's own calls or arguments.
_ADVICE: dict[str, str] = {
    "for-count-bank": "Copy the count into a DS or DD tag and loop on that.",
}


def _advice(code: str, field: str, instruction: Any, resolver: _ResolutionCache) -> str | None:
    return _ADVICE.get(code)


def _build(instruction: Any, exporter: Any) -> list[Any]:
    """Return laddercodec's refusals of the For that the export writes."""
    count = render_operand(exporter, instruction.count, "count", instruction)
    oneshot = bool(instruction.oneshot)
    return refusals_of(lambda: ForLoop(count=count, oneshot=oneshot))


FAMILY = Family(
    name="forloop",
    classes={"ForLoopInstruction": {"count": "count"}},
    build=_build,
    arguments=_FIELD_ARGUMENTS,
    advice=_advice,
    replaced_codes={"CLK_FORLOOP_COUNT_BANK": "for-count-bank"},
)
```

Each field of `Family` is documented in `fields.py`. In short:

- `classes` maps each pyrung class name to the attributes of its call. Each attribute that
  holds an operand or a condition gets the plain word for it in a `CLK_BANK_UNRESOLVED`
  finding, such as "destination". The attributes of an argument that holds several share
  one word. Search's is `{"SearchInstruction": {"search_range": "comparison", "value":
  "comparison", "result": "result", "found": "found flag"}}`.
- `build` returns `refusals_of(lambda: <laddercodec class>(...))`. Write each operand as
  the export writes it, with `render_operand`. It raises `Unbuildable` when the export
  cannot write the operand, and pyrung then reports nothing for the instruction. Copy any
  other choice that the export makes, such as hex constants or a converter. Read the
  family's function in `click/ladder/instructions.py` for those choices. Search's `_build`
  writes its value in hex for a hex table, as the export does.
- `arguments` maps each laddercodec field to the call's argument. An int is the index of a
  positional argument. A str is the name of a keyword argument or of a chained call. The
  caret goes under that argument. Search maps `table_start`, `table_end`, `value` and `op`
  to 0, the comparison. A field that no one argument holds goes to the argument that sets
  it. A timer's `Timer.retained` and `Timer.pin_rows` go to `"reset"`, because
  `.reset()` makes the timer retentive and adds its pin. A counter's
  `Counter.down_enabled` goes to `"down"`, and its `Counter.reset_enabled` and
  `Counter.pin_rows` go to `"reset"`. Map a list field without an index, such as
  `Drum.outputs`. A refusal of one item, such as `Drum.outputs[1]`, then marks that item.
- `advice` returns pyrung's advice for a refusal, or None. It receives the code and
  laddercodec's field, such as `"Search.result"` or `"Drum.outputs[1]"`. laddercodec's
  explanation already leads the hint. Add advice only when it names a pyrung call or
  argument. Take the text from the `suggestion=` of the check that you delete. Search gives `bank-not-allowed` its own advice
  on each of `table_start`, `result` and `found`.
- `ranges` lists the operands that hold a range of addresses. pyrung refuses a range that
  CLICK cannot hold as one with `CLK_COPY_RANGE_NOT_CONSECUTIVE`.
- `replaced_codes` maps each `CLK_` code that you delete to the laddercodec codes that
  replace it. `allow=` names them when a caller passes the old code.

An argument that holds no address, such as a number, still goes in `_call_operands` and
`classes`. `evaluate_family` reports `CLK_BANK_UNRESOLVED` only for a tag or range with no
address. It reports one finding for each argument, at the first of its attributes that has
no address.

## 4. Delete the family's legacy module

Delete `legacy/<family>.py`. Its checks, codes and table rows go with it. Then find what
still names an old code or a deleted function:

```sh
grep -rn "CLK_SEARCH_RANGE_BANK\|CLK_SEARCH_RESULT_NOT_DS\|_evaluate_search_banks" src tests docs
```

In a test, use laddercodec's code, such as `Code.BANK_NOT_ALLOWED`. Change only the code
that the test expects, and only in your family's tests. If a test then fails, the verdict
changed, and step 5 says what to do. In the docs, name the new code, and state the rule
that an author needs.

## 4b. Account for what the module fed to shared code

Your module's `TABLES` hold rows that shared checks in `hardware.py` and `portability.py`
read. When you delete the module, its rows go too. A shared check then reports nothing for
your family, and no family test fails. The coils branch lost four checks this way and
restored them: the bank role of a Done bit, the report of a Done bit that is not mapped, the
timer pair and the immediate range.

List what the module fed before you delete it:

```sh
grep -n "^TABLES" -A12 src/pyrung/click/validation/legacy/<family>.py
grep -rn "_family_rows(\|table(" src/pyrung/click/validation/*.py
```

Write down every table the module defines, every field in each of its rows, and every shared
check that reads them. Then decide where each one goes:

- Into the family. `timer_pairs`, `ranges` and `classes` of your `Family` carry the rows that a
  shared check still reads.
- To laddercodec, when its check reports the same fault.
- Nowhere, when no form reaches the check any more. Do not delete a shared check yourself.
  Tell the orchestrator, who owns those files.

Put one line for each table and each field in your hand-back, saying where it went. A table or
field with no line is one that you did not account for.

Three tests in `tests/click/test_families.py` check part of this.

- The first fails when a module in `legacy/` defines a table that no shared check reads.
- The second fails when a shared check reads a table that no module in `legacy/` defines.
- The third fails when a table in `_WAITING_FOR_THE_BANK_TABLES` is no longer read, or a module in `legacy/` defines it again.

A failure of the first two means that this step is not finished. Contacts and math follow this
step when they move.

## 5. Test

### The shared tests

These run on every family in `families/`. You add nothing to them.

`tests/click/test_families.py` checks these facts:

- `classes` names each attribute that the class declares for its call. A family that
  overrides `operands` skips this test.
- Each str in `arguments` is a keyword or a chained call of the class. A family that
  overrides `call_text` skips this test.
- Every field of each laddercodec class in `arguments` has an entry.
- No code in `replaced_codes` is still in `findings.py` or in `legacy/`.
- No class is in two families, and no class is in a family and in `legacy/`.

`tests/core/test_instruction_tables.py` checks that every attribute that an instruction
writes is checked on CLICK. An attribute that your family's `classes` names counts as
checked. If the test fails for your class, an attribute that the instruction
writes is missing from the declaration.

### The family's tests

Write `tests/click/test_<family>_validation.py`. Cover these cases:

- The caret marks the refused argument. Check `frame.lines` and `frame.caret`, which holds
  the line, the column and the width. Check a keyword argument if the call has one.
- The label under the caret gives the address and not the tag's name.
- The hint holds the family's advice and CLICK's error id, such as OBJ-004.
- A form that CLICK accepts has no findings.
- An unmapped operand gets one `CLK_BANK_UNRESOLVED` finding with the operand's plain word.
  A tag in a chained call's condition counts too.
- The export refuses a form that CLICK refuses.
- `allow=` with each old code:
  - A code that only your family reported is in your legacy module's `CODES`, and you
    put it in `replaced_codes`. Test that `allow=` with it raises `ValueError` and names
    the new codes.
  - A code that other checks still report, such as `CLK_BANK_NOT_WRITABLE`, stays in
    `findings.py`. `allow=` accepts it without an error, so leave it out of
    `replaced_codes`. Test that `allow=` with the old code no longer lets your family's
    finding through, and that `allow=` with the new code does.
- A pointer, if the call takes one.
- A range, if the family declares `ranges`.

Search's caret test for a keyword:

```python
def test_a_refused_found_flag_marks_the_found_keyword():
    (finding,) = _errors(lambda: search(ds.select(1, 10) == 5, result=ds[20], found=y[2]))
    assert finding.code == Code.BANK_NOT_ALLOWED
    line = "search(DS1..DS10 == 5, result=DS20, found=Y002)"
    assert _frame(finding) == ((line,), (0, 42, 4))
```

### The sweep against main

Write a script that builds each form of the family's calls and prints the codes of its
findings, one form per line. Cover every bank for each operand, constants at the ends of
their range, each keyword, a pointer, an unmapped tag and a block element. Build each form
with a fresh `ClickBlocks()`. A mapping in one form can change how another form exports.
Run the script on main and on your branch, and compare:

```sh
mkdir -p /tmp/sweep/main
git archive origin/main src | tar -x -C /tmp/sweep/main
PY=<worktree>/.venv/Scripts/python.exe
PYTHONPATH=/tmp/sweep/main/src timeout 300 $PY sweep.py > main.jsonl
timeout 300 $PY sweep.py > branch.jsonl
sed -e 's/CLK_SEARCH_RANGE_BANK/bank-not-allowed/' \
    -e 's/CLK_SEARCH_RESULT_NOT_DS/bank-not-allowed/' main.jsonl |
  diff --strip-trailing-cr - branch.jsonl
```

The `sed` maps each old code to its new code, so the diff shows only changed verdicts.
Give `sed` one `-e` for each old code. Keep the script out of the repo.

The For sweep tried 65 forms, and 5 changed. The Search sweep tried 591 forms, and 317
changed. These changes come from the shared code, so expect them in every family:

- An unmapped operand gets `CLK_BANK_UNRESOLVED`. Main's validation reported nothing, and
  main's export refused it with its own message.
- A pointer operand gets laddercodec's `pointer-outside-copy` in place of pyrung's
  `CLK_PTR_CONTEXT_ONLY_COPY`. pyrung drops its own pointer finding when laddercodec reports
  the same fault, and laddercodec reports no bank for a pointer.
- A number where laddercodec wants a whole number gets laddercodec's refusal. The For
  family refuses `forloop(3.5)` and `forloop(5.0)` with `not-an-address`. CLICK's For dialog
  refuses `1.5`, so the verdict is right. laddercodec's text calls the count an address, and
  laddercodec will change the text. Do not work around a laddercodec message in pyrung.
- A family whose rows leave `WRITE_TARGET_FIELDS` no longer gets `CLK_BANK_NOT_WRITABLE`.
  laddercodec's bank rule reports the same operand. Search's found flag in X, T, CT or SC
  had `CLK_BANK_NOT_WRITABLE`, and it gets `bank-not-allowed`.

### A changed verdict

A verdict changes when the branch refuses a form that main accepted, or accepts a form that
main refused. Do not change the code to keep main's verdict. Record each change as a row for
`ssweber.github.io/plan/rule-decisions.md`, in the columns of its tables, and send the rows
to the orchestrator. Read the file first. Its section C lists cases that Sam already ruled
on, such as a search "found" bit outside C.

If laddercodec is wrong about CLICK, send the case to laddy, laddercodec's session. Do not
work around it in pyrung. Pin the case with a test that fails until laddercodec is fixed:

```python
@pytest.mark.xfail(strict=True, reason="laddercodec refuses DS1 as a For count. Sent to laddy.")
```

If only laddercodec's text is wrong, send laddy the case and leave the text. A later move of
laddercodec's pin changes it.

A test that checks something else, such as how the export spells a quote, may need a form
that CLICK now refuses. Pass the new code to `allow=` in that test, and say why in a
comment. The two quote tests in `tests/click/test_ladder_export.py` do this.

## 6. Add the CHANGELOG lines

Add your lines at the end of "Behaviour changes" in Unreleased. Give each change its own
item. The first line says what changed and what pyrung did before. The second says what to
do. Say "as a CLICK does" when that is the change. The Search lines:

```markdown
- CLICK validation of `search()` now reports `bank-not-allowed` for a range, a result or a `found` flag in a bank that CLICK refuses (it reported `CLK_SEARCH_RANGE_BANK`, `CLK_SEARCH_RESULT_NOT_DS` or `CLK_BANK_NOT_WRITABLE`).
  Pass the new code to `allow=`.
- CLICK validation and the export now refuse these `search()` forms, which CLICK's editor rejects (they exported them):
  - A `found` flag in Y, such as `found=y[2]`. Map it to a C bit.
  - `>`, `>=`, `<` or `<=` on a TXT range. Use `==` or `!=`.
```

Name every old code that the new code replaces, the shared ones too. List the forms that are
newly refused, each with what to write instead.

`allow=` accepts an old code that other checks still report, and the code no longer lets
your finding through. Say so in the second line, because nothing else tells the user. Do
not write that `allow=` raises for such a code:

```markdown
- CLICK validation of `on_delay()` and `off_delay()` now reports `constant-out-of-range` for a preset above 32767 (it reported `CLK_TIMER_PRESET_OVERFLOW`).
  Pass the new code to `allow=`. `allow=["CLK_TIMER_PRESET_OVERFLOW"]` no longer lets this finding through.
```

Two family branches add lines next to each other, and git reports a conflict there. The
orchestrator resolves it when it merges the branches. Do not try to prevent it.

## 7. Check and hand back

Run these in your worktree, each with a time limit:

```sh
make lint
timeout 600 uv run --no-sync pytest tests/click tests/core/test_instruction_tables.py tests/core/test_validation_render.py -q -n 6
```

Then run the folders of `make test` one at a time. `make lint` rewrites files, so commit
what it changes.

Your hand-back gives the orchestrator these:

- The rows for `rule-decisions.md`, one for each changed verdict.
- The cases sent to laddy, with the test that pins each one.
- The sweep's counts: forms tried and forms changed.
- The commands that you ran and their counts.
- Where each table and field of your legacy module went (step 4b).
- Each step of this recipe that did not fit the family, and what you did.

## Steps that no family has tried

- A refusal on a chained call. Shift's `pin_rows` field covers both its clock and its
  reset, and `arguments` names one argument for it. A test covers how a chained call
  renders and names its tags. No family has run one through laddercodec.
- A timer's UDT with a real laddercodec Timer. At the default unit, the declared call is
  the special case's call, and a test checks it. With another unit, the declared call adds
  the unit, such as `unit='seconds'`, and the special case leaves it out.
- An operand that holds a list of tags, such as a drum's outputs. It needs two hooks.
  - The declared operands find no tags in a list, so laddercodec would quote those
    addresses without names. Override `operands`, and give each tag of the list as a value
    of its own.
  - A drum holds its outputs as a tuple, and the declared call shows a tuple as one
    argument. Override `call_text`, and give the list as item texts. The caret of a
    refusal such as `Drum.outputs[1]` then marks that item.

  `tests/click/test_family_hooks.py` checks both with refusals that it makes itself. No
  family has run a list through laddercodec.
- A drum's `.jump(cond, step=...)`. The step sits inside a chained call, so the declared
  call shows the condition and drops the step. Override `operands` and `call_text`, and
  build the `Call` yourself.
- Contacts. A contact is part of a rung's condition, and `evaluate_family` reads only
  instructions. Contacts need a change to shared code first.
