# CLICK `send/receive` + `send_click/receive_click` V1 Plan

## Goal

Add CLICK communication instructions to `pyrung.click` with scan-safe async behavior:

- `send(...)`
- `receive(...)`
- `send_click(...)`
- `receive_click(...)`

These are ladder DSL instructions (not utility helpers), designed for rung/state-machine usage where the rung must remain true until success/error.

---

## Locked Decisions

1. **V1 transport scope:** Modbus TCP only.
2. **Public API parameter name:** `device_id` (not `slave` / `slave_id`).
3. **Module surface:** all 4 functions exported from `pyrung.click`.
4. **Runtime model:** internal background async worker loop (scan engine stays synchronous).
5. **CLICK fidelity:** explicit status tags for send/receive.
6. **Retry policy:** global defaults only in v1 (no per-instruction retry knobs).
7. **Rung false behavior:** cancel/discard in-flight request and clear status outputs.
8. **Rung true after completion:** auto-restart next scan.
9. **Out-of-scope (v2):** serial/RTU and COM-port settings (baud/parity/stop/RTS/char timeout/response delay).

---

## Public API Signatures (V1)

```python
from typing import Literal
from pyrung.core import Tag, BlockRange

def send(
    *,
    host: str,
    port: int,
    function_code: Literal[5, 6, 15, 16],
    remote_address: int,
    source: Tag | BlockRange,
    sending: Tag,
    success: Tag,
    error: Tag,
    exception_response: Tag,
    device_id: int = 1,
    count: int | None = None,
) -> None: ...

def receive(
    *,
    host: str,
    port: int,
    function_code: Literal[1, 2, 3, 4],
    remote_address: int,
    dest: Tag | BlockRange,
    receiving: Tag,
    success: Tag,
    error: Tag,
    exception_response: Tag,
    device_id: int = 1,
    count: int | None = None,
) -> None: ...

def send_click(
    *,
    host: str,
    port: int,
    remote_start: str,  # CLICK address, e.g. "C101", "DS200"
    source: Tag | BlockRange,
    sending: Tag,
    success: Tag,
    error: Tag,
    exception_response: Tag,
    device_id: int = 1,
    count: int | None = None,
    function_code: Literal[5, 6, 15, 16] | None = None,
) -> None: ...

def receive_click(
    *,
    host: str,
    port: int,
    remote_start: str,  # CLICK address, e.g. "C101", "DS200"
    dest: Tag | BlockRange,
    receiving: Tag,
    success: Tag,
    error: Tag,
    exception_response: Tag,
    device_id: int = 1,
    count: int | None = None,
    function_code: Literal[1, 2, 3, 4] | None = None,
) -> None: ...
```

Notes:

- `count=None` means derive from local operand length (`Tag` -> 1, `BlockRange` -> number of tags).
- `exception_response` should be `INT` or `DINT` destination tag.

---

## Expected Ladder Usage Pattern

Typical state machine pattern:

```python
with Rung(ReadStep == 1):
    receive_click(
        host="192.168.1.20",
        port=502,
        remote_start="DS100",
        count=4,
        dest=ds.select(1, 4),
        receiving=CommRx,
        success=CommOk,
        error=CommErr,
        exception_response=CommExCode,
        device_id=1,
    )

with Rung(ReadStep == 1, CommOk):
    copy(2, ReadStep)

with Rung(ReadStep == 1, CommErr):
    copy(900, FaultStep)
```

Behavior contract:

- While request is in flight: `receiving/sending = True`.
- On success: `success=True`, `error=False`, `busy=False`.
- On error: `success=False`, `error=True`, `busy=False`.
- If rung drops false before completion: request is canceled/discarded and outputs clear.

---

## Runtime State Machine (Per Instruction Instance)

Instruction object state:

1. `IDLE` (no in-flight future)
2. `PENDING` (future in progress)
3. `TERMINAL` (success/error latched for this scan)

Per-scan logic:

1. Evaluate captured rung-enable condition inside instruction.
2. If rung false:
   - cancel in-flight future if present
   - clear status tags to defaults
   - remain/re-enter `IDLE`
3. If rung true and `IDLE`:
   - clear `success/error/exception_response`
   - set `sending/receiving=True`
   - submit async request to worker
   - move to `PENDING`
4. If rung true and `PENDING`:
   - if future not done: keep busy true
   - if done: apply result -> terminal outputs, busy false
5. If rung true after terminal:
   - start new request on next scan (auto-restart behavior)

`success`/`error` should remain visible at least one scan before next request starts.

---

## File-by-File Implementation Plan

## 1) `src/pyrung/click/modbus_worker.py` (new)

Purpose: background asyncio loop + request submission for synchronous scan engine.

Responsibilities:

1. Own a daemon thread with dedicated asyncio event loop.
2. Keep Modbus TCP client pool keyed by `(host, port)`.
3. Expose synchronous submit API returning `concurrent.futures.Future`.
4. Normalize client method unit argument:
   - call with `device_id=...` first
   - fallback to `slave=...` only for compatibility if needed

Defaults/constants:

- `DEFAULT_TIMEOUT_S = 1.0`
- `DEFAULT_RETRIES = 0`
- `TRANSPORT_FAILURE_EXCEPTION_CODE = 0`

Request model (dataclass):

- operation kind (`send_raw`, `receive_raw`)
- function_code
- remote_address
- count
- payload (for send)
- host / port / device_id

Result model (dataclass):

- `ok: bool`
- `exception_code: int` (modbus exception code, or 0 for transport failure)
- `data: list[bool | int] | None` (for receive)

---

## 2) `src/pyrung/click/modbus_instructions.py` (new)

Purpose: DSL functions and instruction classes.

Contents:

1. DSL entry functions: `send`, `receive`, `send_click`, `receive_click`.
2. Two core instruction classes (raw send/raw receive) derived from `Instruction`.
3. Wrapper logic for click variants that translate CLICK addresses into raw parameters.
4. Validation helpers for:
   - function-code compatibility
   - local operand type/length
   - status tag types
5. Operand helpers:
   - resolve `Tag|BlockRange` to list of concrete tags
   - read local source values from `ScanContext`
   - write receive values back to destination tags with tag-type conversion

Instruction integration pattern:

- capture rung condition via `ctx._rung._get_combined_condition()` (same style as timers/counters).
- each instruction implements `always_execute() -> True` and evaluates enable condition internally.

Status tag typing rules:

- `sending/receiving/success/error` must be `BOOL`.
- `exception_response` must be `INT` or `DINT`.

Raw send behavior by FC:

- FC5: single coil write (count must be 1)
- FC15: multiple coils
- FC6: single register write (count must be 1)
- FC16: multiple registers

Raw receive behavior by FC:

- FC1/FC2: coil/discrete bit reads
- FC3/FC4: holding/input register reads

Count rules:

- If `count` omitted, derive from local operand.
- If provided, must match local operand length.

---

## 3) `src/pyrung/click/__init__.py` (edit)

Add exports:

- `send`
- `receive`
- `send_click`
- `receive_click`

---

## 4) `spec/dialects/click.md` (edit)

Add communication instruction section documenting:

1. API signatures (with `device_id`)
2. rung-on/rung-off behavior
3. busy/success/error/exception semantics
4. transport-failure exception code = `0`
5. v1 scope and v2 serial defer

---

## CLICK Wrapper Translation Rules

Implemented in `send_click` / `receive_click`:

1. Parse `remote_start` with `pyclickplc.parse_address`.
2. Use `plc_to_modbus(bank, index)` for raw start.
3. Use `MODBUS_MAPPINGS[bank]` + `BANKS[bank]` for:
   - allowed function codes
   - coil vs register space
   - width (1 or 2 registers per value)
4. `count` is in **values**, not raw registers.

FC defaulting when wrapper `function_code=None`:

1. `send_click`:
   - BIT bank + single value -> FC5
   - BIT bank + multi value -> FC15
   - register bank + single value -> FC6
   - register bank + multi value -> FC16
2. `receive_click`:
   - choose lowest supported read FC for that bank (prefer FC1 over FC2, FC3 over FC4 when available)

---

## Value Conversion Rules

Send side:

1. Coils: convert to `bool`.
2. Registers: convert each source value to unsigned 16-bit word (`int(value) & 0xFFFF`).

Receive side:

1. FC1/FC2 -> booleans.
2. FC3/FC4 -> register words.
3. Store to destination tags using tag-aware conversion equivalent to existing copy-store semantics:
   - `INT`/`DINT` clamp behavior for copy-family operations
   - `BOOL` bool conversion
   - `WORD` unsigned wrap
   - `REAL` float cast if explicitly targeted

---

## Error and Exception Semantics

On terminal status:

1. Success:
   - `success=True`, `error=False`, `exception_response=0`
2. Modbus exception response:
   - `success=False`, `error=True`, `exception_response=<modbus exception code>`
3. Transport/runtime failure (timeout, connect, request canceled due to rung false):
   - `success=False`, `error=True`, `exception_response=0`

When rung false:

- clear all status outputs to defaults (including ex-code -> 0).

---

## Tests (Required)

Create `tests/click/test_modbus_instructions.py`.

Use fake async client + fake response objects (no real network).

Test cases:

1. `send` FC5 success path toggles status correctly.
2. `send` FC16 multi-register payload conversion works.
3. `receive` FC1 populates BOOL destination block.
4. `receive` FC3 populates INT destination block.
5. Modbus exception sets `error=True` and correct exception code.
6. Transport failure sets `error=True` and ex-code `0`.
7. Rung false mid-flight cancels/discards and clears outputs.
8. Rung stays true after completion starts next request next scan.
9. `send_click` maps CLICK start to raw address correctly.
10. `receive_click` width-2 bank handling (`DD/DF/CTD`) count/address logic.
11. Signature-level validation:
    - invalid status tag types
    - invalid function code
    - count mismatch with local operand
12. `device_id` path is used in request calls (with compatibility fallback behavior covered by test doubles).

Optional additional unit tests:

- `tests/click/test_modbus_worker.py` for client pooling and method argument compatibility.

---

## Acceptance Criteria

Implementation is complete when:

1. All 4 DSL functions exist and are exported from `pyrung.click`.
2. Public signatures use `device_id`.
3. Rung-based async semantics match this document.
4. CLICK wrapper translation uses `pyclickplc` mapping correctly.
5. V1 tests pass under `uv run pytest tests/click/test_modbus_instructions.py`.
6. `spec/dialects/click.md` is updated with final behavior contract.

---

## Explicit V2 Backlog (Not in this PR)

1. Modbus RTU/serial transport.
2. Serial config fields:
   - node address
   - baud
   - parity
   - stop bits
   - communication data bits
   - response delay
   - character timeout
   - RTS on/off delay
3. Per-instruction timeout/retry settings.
4. Dynamic runtime parameters (dynamic device id, dynamic remote address/count).

