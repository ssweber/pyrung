# CLICK Python Codegen

`ladder_to_pyrung()` and `ladder_to_pyrung_project()` convert CLICK ladder data back into executable pyrung Python source. This is the reverse of [`pyrung_to_ladder()`](click.md#ladder-csv-export) — import from CLICK instead of export to CLICK.

## Single-file codegen

`ladder_to_pyrung()` accepts a file path (to a CSV or directory) or a `LadderBundle` for in-memory round-trip without disk I/O.

Generated timer and counter calls use pyrung's modern surface syntax: presets render positionally, the default millisecond unit is omitted, and non-default timer units use friendly strings like `"sec"` and `"hour"`.

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

1. `nickname_csv=` — path to a CLICK nickname CSV (Address.csv). Recommended, because it also enables structured type inference (see below).
2. `nicknames=` — pre-parsed `{operand: nickname}` dict (e.g. `{"X001": "start_button"}`).
3. Neither — raw operand names used as-is (`X001`, `DS1`, etc.).

Cannot provide both `nickname_csv` and `nicknames`.

```python
code = ladder_to_pyrung("main.csv", nickname_csv="Address.csv")

code = ladder_to_pyrung("main.csv", nicknames={"X001": "start_button", "Y001": "motor"})
```

### Structured type inference

When `nickname_csv=` is provided, codegen calls `TagMap.from_nickname_file()` internally. It reconstructs semantic metadata only from explicit markers such as `:block`, `:udt`, and `:named_array(...)`. Bare tags remain grouping-only, so the generated code keeps them as flat tags or raw bank ranges instead of inventing pyrung structures.

Without `nickname_csv`, a named-array group comes back as slot aliases:

```python
ds.slot(101, name='Channel1_Id')
ds.slot(102, name='Channel1_Val')
ds.slot(103, name='Channel2_Id')
ds.slot(104, name='Channel2_Val')
Channel1_Id = ds[101]
Channel1_Val = ds[102]
Channel2_Id = ds[103]
Channel2_Val = ds[104]

# in the program:
copy(Channel1_Id, Channel2_Val)

# TagMap is empty — tags are block aliases, not standalone objects
mapping = TagMap({})
```

With `nickname_csv=` pointing to a CSV that has named-array markers:

```python
@named_array(Int, count=2)
class Channel:
    Id = 0
    Val = 0

# in the program:
copy(Channel[1].Id, Channel[2].Val)

# in TagMap:
mapping = TagMap([
    *Channel.map_to(ds.select(101, 104)),
], include_system=False)
```

Named-array instance windows round-trip as whole-instance selects when the ladder uses an exact aligned span:

```python
blockcopy(RecipeProfile.instance(2), WorkingRecipe.select(1, 3))
fill(0, RecipeProfile.instance_select(1, 2))
```

This works for both dense and sparse layouts — the range length must be an exact multiple of the stride and start at an instance boundary.

For UDTs (fields spanning different memory banks), per-field `map_to` is emitted:

```python
@udt(count=2)
class Motor:
    Running: Bool = False
    Speed: Int = 0

mapping = TagMap([
    Motor.Running.map_to(c.select(101, 102)),
    Motor.Speed.map_to(ds.select(1001, 1002)),
], include_system=False)
```

Singleton structures (count=1) use dotted access without indexing: `Config.Timeout`, not `Config[1].Timeout`. If the CSV uses numbered names for a singleton (e.g. `Config1_Timeout`), the importer infers `always_number=True` and emits `@named_array(Int, always_number=True)`.

For details on `@named_array` and `@udt` syntax, see the [Tag Structures guide](../guides/tag-structures.md). For the CSV marker format, see [CSV marker format](click.md#csv-marker-format).

### What codegen infers

Tag types from operand prefixes (`X`→Bool, `DS`→Int, etc.), block ranges from `DS100..DS102` notation, OR expansion via `Or()`, branch conditions, timer/counter pin chains, `for`/`next` loops, and comments.

For the CSV format that codegen reads, see the [laddercodec CSV format guide](https://pyrung.com/laddercodec/guides/csv-format/).

### Round-trip guarantee

The generated code is designed to round-trip: `exec()` the output, then `pyrung_to_ladder(logic, mapping)` reproduces the original ladder CSV. This is tested extensively.

Note that codegen always emits the CLICK-first style (slot aliases, no standalone tag constructors). If you started with pyrung-first code (hand-written `Int("SpeedCmd")` + `TagMap`), exporting to CLICK and re-importing produces the CLICK-first form. This is intentional — once the CLICK project is the source of truth, the generated code reflects that. The two styles represent different ownership models, not interchangeable formats.

Range instructions use integer addresses (`ds.select(1, 100)`) with boundary nickname comments (`# SpeedCmd..SpeedFbk`) when the endpoints have nicknames. This reflects reality: a CLICK Copy Block with source DS1 and destination DS100 is address-bound. You can't move `SpeedCmd` to `DS501` without rewriting every range instruction that spans it. The nicknames are context, not structure. If you want remappable ranges, add `:named_array` markers to the nickname CSV — that's the explicit act of design that gives you `RecipeProfile.instance_select(1, 2)` instead of raw addresses.

## Multi-file project codegen

`ladder_to_pyrung_project()` generates a complete Python project instead of a single file. Each subroutine gets its own file with a `@subroutine` decorator, tags and the TagMap live in `tags.py`, and `main.py` ties everything together.

```python
from pyrung.click import ladder_to_pyrung_project

files = ladder_to_pyrung_project("ladder_dir/")
files = ladder_to_pyrung_project("ladder_dir/", nickname_csv="Address.csv")
files = ladder_to_pyrung_project("ladder_dir/", output_dir="pump_project_py/")
```

Generated `README.md` and `AGENTS.md` guidance describes a temporary CLICK
workspace by default. When an integration stores the project in a durable
location, pass `workspace_kind="persistent"`. Pyrung marks the lifecycle
section in each file so an integration can refresh that generated section
without replacing surrounding user documentation.

The return value is a `dict[str, str]` mapping relative paths to content:

```
tags.py                  # slot aliases, structures, TagMap
main.py                  # Program context, main rungs, call() statements
subroutines/
  __init__.py
  _sub_startup.py        # @subroutine("startup") decorated function
  _sub_alarm_handler.py
```

### How subroutines are represented

Each subroutine file defines a decorated function that auto-registers with the Program when called:

```python
# subroutines/_sub_startup.py
from pyrung import rung, subroutine, out
from ..tags import SubLight

@subroutine("startup")
def _sub_startup():
    with rung():
        out(SubLight)
```

`main.py` imports and calls it by reference (not by string name):

```python
from .subroutines._sub_startup import _sub_startup

with Program() as logic:
    with rung(Button):
        call(_sub_startup)
```

The private `_sub_` prefix keeps generated function and module names separate
from CLICK nicknames. The decorator retains the original CLICK subroutine name.

### Per-file imports

Each generated file imports only what it uses. A subroutine that touches `X001` and `Y001` won't import `X002` or `DS1`. `tags.py` is the single source of truth for all tag declarations; other files import from it.

### Nickname and structure support

Same as `ladder_to_pyrung()` — pass `nickname_csv=` for readable variable names and automatic `@named_array` / `@udt` inference, or `nicknames=` for a pre-parsed dict. `tags.py` suppresses the inline `# X001` address comments since the slot configs and nickname CSV already provide that mapping.
