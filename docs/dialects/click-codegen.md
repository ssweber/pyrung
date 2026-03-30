# Click Python Codegen

`ladder_to_pyrung()` and `ladder_to_pyrung_project()` convert Click ladder data back into executable pyrung Python source. This is the reverse of [`pyrung_to_ladder()`](click.md#ladder-csv-export) — import from Click instead of export to Click.

## Single-file codegen

`ladder_to_pyrung()` accepts a file path (to a CSV or directory) or a `LadderBundle` for in-memory round-trip without disk I/O.

```python
from pyrung.click import ladder_to_pyrung

code = ladder_to_pyrung("main.csv")                    # from CSV file
code = ladder_to_pyrung("ladder_dir/")                  # from directory with subroutines/*.csv
code = ladder_to_pyrung(bundle)                         # from LadderBundle (no disk)
code = ladder_to_pyrung("main.csv", output_path="generated.py")  # write to file
```

### Round-trip

```python
from pyrung.click import pyrung_to_ladder, ladder_to_pyrung

bundle = pyrung_to_ladder(logic, mapping)
code = ladder_to_pyrung(bundle)          # no CSV files needed
```

### Nickname substitution

Three ways to provide nicknames for readable variable names:

1. `nickname_csv=` — path to a Click nickname CSV (Address.csv). Recommended, because it also enables structured type inference (see below).
2. `nicknames=` — pre-parsed `{operand: nickname}` dict (e.g. `{"X001": "start_button"}`).
3. Neither — raw operand names used as-is (`X001`, `DS1`, etc.).

Cannot provide both `nickname_csv` and `nicknames`.

```python
code = ladder_to_pyrung("main.csv", nickname_csv="Address.csv")

code = ladder_to_pyrung("main.csv", nicknames={"X001": "start_button", "Y001": "motor"})
```

### Structured type inference

When `nickname_csv=` is provided, codegen calls `TagMap.from_nickname_file()` internally. It reconstructs semantic metadata only from explicit markers such as `:block`, `:udt`, and `:named_array(...)`. Bare tags remain grouping-only, so the generated code keeps them as flat tags or raw bank ranges instead of inventing pyrung structures.

Without `nickname_csv`, a named-array group comes back flat:

```python
Channel1_id = Int("Channel1_id")
Channel1_val = Int("Channel1_val")
Channel2_id = Int("Channel2_id")
Channel2_val = Int("Channel2_val")

# in the program:
copy(Channel1_id, Channel2_val)

# in TagMap:
mapping = TagMap({
    Channel1_id: ds[101],
    Channel1_val: ds[102],
    ...
})
```

With `nickname_csv=` pointing to a CSV that has named-array markers:

```python
@named_array(Int, count=2)
class Channel:
    id = 0
    val = 0

# in the program:
copy(Channel[1].id, Channel[2].val)

# in TagMap:
mapping = TagMap([
    *Channel.map_to(ds.select(101, 104)),
], include_system=False)
```

For UDTs (fields spanning different memory banks), per-field `map_to` is emitted:

```python
@udt(count=2)
class Motor:
    running: Bool = False
    speed: Int = 0

mapping = TagMap([
    Motor.running.map_to(c.select(101, 102)),
    Motor.speed.map_to(ds.select(1001, 1002)),
], include_system=False)
```

Singleton structures (count=1) use dotted access without indexing: `Config.timeout`, not `Config[1].timeout`.

For details on `@named_array` and `@udt` syntax, see the [Tag Structures guide](../guides/tag-structures.md).

### What codegen infers

Tag types from operand prefixes (`X`→Bool, `DS`→Int, etc.), block ranges from `DS100..DS102` notation, OR expansion via `any_of()`, branch conditions, timer/counter pin chains, `for`/`next` loops, and comments.

For the CSV format that codegen reads, see the [laddercodec CSV format guide](https://ssweber.github.io/laddercodec/guides/csv-format/).

### Round-trip guarantee

The generated code is designed to round-trip: `exec()` the output, then `pyrung_to_ladder(logic, mapping)` reproduces the original CSV. This is tested extensively.

## Multi-file project codegen

`ladder_to_pyrung_project()` generates a complete Python project instead of a single file. Each subroutine gets its own file with a `@subroutine` decorator, tags and the TagMap live in `tags.py`, and `main.py` ties everything together.

```python
from pyrung.click import ladder_to_pyrung_project

files = ladder_to_pyrung_project("ladder_dir/")
files = ladder_to_pyrung_project("ladder_dir/", nickname_csv="Address.csv")
files = ladder_to_pyrung_project("ladder_dir/", output_dir="pump_project_py/")
```

The return value is a `dict[str, str]` mapping relative paths to content:

```
tags.py                  # tag declarations, structures, TagMap
main.py                  # Program context, main rungs, call() statements
subroutines/
  __init__.py
  startup.py             # @subroutine("startup") decorated function
  alarm_handler.py
```

### How subroutines are represented

Each subroutine file defines a decorated function that auto-registers with the Program when called:

```python
# subroutines/startup.py
from pyrung import Rung, subroutine, out
from tags import SubLight

@subroutine("startup")
def startup():
    with Rung():
        out(SubLight)
```

`main.py` imports and calls it by reference (not by string name):

```python
from subroutines.startup import startup

with Program() as logic:
    with Rung(Button):
        call(startup)
```

### Per-file imports

Each generated file imports only what it uses. A subroutine that touches `X001` and `Y001` won't import `X002` or `DS1`. `tags.py` is the single source of truth for all tag declarations; other files import from it.

### Nickname and structure support

Same as `ladder_to_pyrung()` — pass `nickname_csv=` for readable variable names and automatic `@named_array` / `@udt` inference, or `nicknames=` for a pre-parsed dict. `tags.py` suppresses the inline `# X001` address comments since the TagMap and nickname CSV already provide that mapping.
