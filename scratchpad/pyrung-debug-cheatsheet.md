# pyrung Debug Cheat Sheet

## Condition Expressions

Use these anywhere VS Code asks for a breakpoint condition.
Same syntax as your rung definitions. With commas, comparisons
just work. With `&`/`|`, wrap comparisons in parens — same as Python.

```
Fault                          tag is truthy
~Fault                         tag is falsy
MotorTemp > 100                comparison  (==  !=  <  <=  >  >=)
Fault, Pump                    comma = implicit AND
Fault, MotorTemp > 100         implicit AND with comparison
Fault & Pump                   & works for truthy tags
Running | ~Estop               | and ~ work for truthy tags
Fault & (MotorTemp > 100)      & with comparison needs parens
Running | (Mode == 1)          | with comparison needs parens
Running | ~Estop, Mode == 1    mix commas and operators freely
all_of(Fault, Pump, Valve)     explicit AND (same as commas)
any_of(Low, High, Emergency)   explicit OR
```

> **Tip:** Commas are the easiest way to combine conditions — no parens
> needed. With `&`/`|`, truthy tags work bare (`Run & ~Stop`) but
> comparisons need parens (`Run & (Temp > 100)`). `~` negates a single
> tag only. Or just use commas: `Run, ~Stop, Temp > 100`.

---

## Breakpoints

| What you want | How to do it |
|---|---|
| Stop on a rung | Click the gutter next to the rung |
| Stop when a condition is true | Right-click gutter → **Add Conditional Breakpoint...** → type expression |
| Stop after the Nth hit | Right-click breakpoint → **Edit Hit Count...** → type a number |
| Edit a condition later | Right-click breakpoint → **Edit Condition...** |
| Disable without removing | Uncheck in the **Breakpoints** panel |

Hit count is cyclical: value 2 stops on the 2nd, 4th, 6th... eligible hit.

## Logpoints (non-stopping breakpoints)

| What you want | How to do it |
|---|---|
| Log a message when a rung executes | Right-click gutter → **Add Logpoint...** → type message |
| Log only when a condition is true | Add logpoint, then right-click → **Edit Condition...** |
| Take a snapshot | Logpoint message: `Snapshot: my_label` (logs + captures state) |
| Conditional snapshot | Logpoint `Snapshot: fault_case` + condition `MotorTemp > 100` |

Logpoint output appears in the **Debug Console** during both Continue and stepping commands.

## Snapshots & Labels

After a snapshot logpoint fires, find it:

- **Command Palette** → `pyrung: Find Label` → type the label name
- Returns the scan ID and timestamp of the match

## Monitors

| What you want | How to do it |
|---|---|
| Watch a tag value | **Command Palette** → `pyrung: Add Monitor` → type tag name |
| See current values | **Variables** panel → **PLC Monitors** scope (updates on each stop) |
| Live change log | **Output** panel → **pyrung: Debug Events** channel |
| Stop watching | **Command Palette** → `pyrung: Remove Monitor` → pick from list |

## Data Breakpoints (stop on tag change)

| What you want | How to do it |
|---|---|
| Break when any tag changes | Right-click a tag in **PLC Monitors** → **Break When Value Changes** |
| Break on change + condition | Set data breakpoint, then edit its condition in the **Breakpoints** panel |

## Force Values

Type in the **Debug Console**:

```
force TagName value
unforce TagName
```

## Keyboard Shortcuts

| Action | Shortcut |
|---|---|
| Continue | `F5` |
| Step (next scan) | `F10` |
| Pause | `F6` |
| Toggle breakpoint | `F9` |
| Open Command Palette | `Ctrl+Shift+P` |


