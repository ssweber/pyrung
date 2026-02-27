**1. Truthiness for Integer Tags**
Allow `Int` tags to be evaluated as booleans (nonzero = True) in `Rung` conditions.
*   **Current:** `with Rung(sub.SubName_xCall == 1):` [1]
*   **Proposed:** `with Rung(sub.SubName_xCall):` (Logic engine handles `!= 0`).
*   *Note:* Validation layers can still enforce strict typing for specific hardware targets (like Click) if desired.

**2. Live Tag Proxies**
Bind `Tag` objects to the active `PLCRunner` instance to simplify testing and simulation scripts.
*   **Current:** `runner.patch({sub.SubName_xCall: 1})` then `val = state.tags.get('SubName_xCall')`
*   **Proposed:** `sub.SubName_xCall.value = 1` and `print(sub.SubName_xCall.value)`

**3. Dataclasses for Structures**
Replace the proprietary `PackedStruct`/`Struct` builder with standard Python dataclasses.
*   **Current:** `SubNameDs = PackedStruct("SubName", TagType.INT, ...)` [1]
*   **Proposed:**
    ```python
    @plc_struct
    class SubNameDs:
        xCall: int = 0
        Trans: int = 0
    ```

**4. Dot-Notation for State Inspection**
Return a namespace object from `runner.current_state` to allow autocomplete-friendly debugging.
*   **Current:** `state.tags['SubName_CurStep']`
*   **Proposed:** `state.SubName_CurStep`

**7. Automatic Tag Naming (PEP 487)**
Currently, tags are manually named via strings, such as `Bool("Step1_Event")` [1].
*   **Proposed:** Use `__set_name__` or a descriptor-based approach so tags automatically inherit the variable name assigned to them.
*   **Example:** `Step1_Event = Bool()` would automatically set the internal name to "Step1_Event" without redundant string input.