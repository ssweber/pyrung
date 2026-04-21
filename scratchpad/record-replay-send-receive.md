# Record-and-Replay for Send/Receive Instructions

## Context

Send/receive instructions do real Modbus I/O during live execution. During history
replay (`replay_to()`, `seek()`, `rewind()`), instructions are inert (`_is_live()`
returns False → early return). I/O-related tags (`sending`, `success`, `error`,
`exception_response`, dest tags for receive) never update during replay, producing
incorrect reconstructed states. Ladder logic like `with Rung(Sending): ...` sees
wrong values.

**Goal:** Record I/O results during live execution. During replay, the instruction
runs its state machine with recorded results (interpreted path), or applies
pre-computed tag writes (compiled path).

## Design

### No Memory Needed for Pending State

The `sending`/`receiving` tag persists in committed state across scans. During replay,
the instruction checks its own tag to detect in-flight status:
- Submit scan: submit event in log → set `sending=True`
- In-flight scans: `sending` already True from previous commit → no-op
- Drain scan: drain event in log → apply result, set `sending=False`

### Data Model

Two record types, both carrying `tag_writes` for compiled replay:

```python
# scan_log.py
@dataclass(frozen=True)
class IoSubmitRecord:
    tag_writes: tuple[tuple[str, Any], ...]

@dataclass(frozen=True)  
class IoResultRecord:
    ok: bool
    exception_code: int
    values: tuple[Any, ...]            # final typed values (receive) or () (send)
    tag_writes: tuple[tuple[str, Any], ...]  # all tag mutations for compiled replay
```

### Files to Modify

#### 1. `src/pyrung/core/scan_log.py`

Add `IoSubmitRecord` and `IoResultRecord` frozen dataclasses.

Add to `ScanLog.__init__()`:
```python
self._io_submits_by_scan: dict[int, dict[str, IoSubmitRecord]] = {}
self._io_drains_by_scan: dict[int, dict[str, IoResultRecord]] = {}
```

Add methods:
- `record_io_submit(scan_id, key, record)` 
- `record_io_drain(scan_id, key, record)`

Add to `ScanLogSnapshot`:
```python
io_submits_by_scan: Mapping[int, Mapping[str, IoSubmitRecord]]
io_drains_by_scan: Mapping[int, Mapping[str, IoResultRecord]]
```

Update `snapshot()`, `trim_before()`, `bytes_estimate()`.

#### 2. `src/pyrung/core/context.py` — ScanContext

New constructor param:
```python
replay_io: tuple[Mapping[str, IoSubmitRecord], Mapping[str, IoResultRecord]] | None = None
```

New instance attrs:
```python
self._io_submit_staging: dict[str, IoSubmitRecord] = {}
self._io_drain_staging: dict[str, IoResultRecord] = {}
self._replay_io_submits: Mapping[str, IoSubmitRecord] = {}  # from replay_io[0]
self._replay_io_drains: Mapping[str, IoResultRecord] = {}   # from replay_io[1]
self._is_replay_io: bool = replay_io is not None
```

New methods:
```python
def record_io_submit(self, key: str, record: IoSubmitRecord) -> None
def record_io_drain(self, key: str, record: IoResultRecord) -> None
def has_replay_io_submit(self, key: str) -> bool
def get_replay_io_drain(self, key: str) -> IoResultRecord | None

@property
def is_replay_io(self) -> bool
```

Also add these to `_KernelRuntimeContext` in `compiled_plc.py` — but as no-op stubs
(compiled kernel never calls them; they exist so the cast-to-ScanContext type is satisfied).

#### 3. `src/pyrung/core/instruction/send_receive/_core.py`

Restructure both `ModbusSendInstruction.execute()` and `ModbusReceiveInstruction.execute()`:

```python
def execute(self, ctx, enabled):
    if ctx.is_replay_io:
        return self._execute_replay(ctx, enabled)
    return self._execute_live(ctx, enabled)
```

**`_execute_live()`** — Current code with recording added:
- On submit: build tag_writes dict, call `ctx.record_io_submit(key, IoSubmitRecord(tag_writes))`
- On drain: build tag_writes dict, call `ctx.record_io_drain(key, IoResultRecord(...))`
- For receive drain: `values` = final typed values after `_unpack_result` + `_store_copy_value_to_tag_type`

**`_execute_replay()`** — New:
```
1. drain = ctx.get_replay_io_drain(key)
   if drain: apply result to tags, return
2. if ctx.get_tag(self.sending/receiving.name) is True:
   in-flight, return (tag persists from previous scan)
3. if not enabled: return
4. if ctx.has_replay_io_submit(key):
   set sending/receiving=True + clear status tags
```

Extract shared helpers for applying drain results (same tag-write logic, live and replay).

#### 4. `src/pyrung/core/runner.py` — Wire recording and injection

**`_prepare_scan()` (~line 1951):**
```python
replay_io = getattr(self, '_next_scan_replay_io', None)
self._next_scan_replay_io = None
ctx = ScanContext(..., replay_io=replay_io)
```

**`_commit_scan()` (~line 2008, after existing scan log recording):**
```python
if ctx._io_submit_staging:
    for key, record in ctx._io_submit_staging.items():
        self._scan_log.record_io_submit(new_scan_id, key, record)
if ctx._io_drain_staging:
    for key, record in ctx._io_drain_staging.items():
        self._scan_log.record_io_drain(new_scan_id, key, record)
```

**`_apply_log_entries_for_scan()` (~line 1105):**
```python
submits = log.io_submits_by_scan.get(scan_id, {})
drains = log.io_drains_by_scan.get(scan_id, {})
if submits or drains:
    replay._next_scan_replay_io = (submits, drains)
```

#### 5. `src/pyrung/core/runner.py` — Compiled replay (`_replay_to_compiled`, ~line 1198)

After `replay.step_replay()`, apply I/O tag writes directly to the kernel:

```python
replay.step_replay()

# Apply I/O tag writes (runtime would have done this between scans)
for record in log.io_submits_by_scan.get(scan_id, {}).values():
    for tag_name, value in record.tag_writes:
        replay._kernel.tags[tag_name] = value
for record in log.io_drains_by_scan.get(scan_id, {}).values():
    for tag_name, value in record.tag_writes:
        replay._kernel.tags[tag_name] = value
```

This is correct because in the original compiled execution, the runtime processes I/O
AFTER the kernel step. `step_replay()` runs the kernel; I/O tags are then applied
before the next scan reads them.

#### 6. `src/pyrung/core/compiled_plc.py` — `_KernelRuntimeContext` stubs

Add no-op I/O methods to satisfy the ScanContext protocol (the compiled kernel
never calls these, but the cast requires them):
```python
@property
def is_replay_io(self) -> bool:
    return False
def record_io_submit(self, key, record): pass
def record_io_drain(self, key, record): pass
def has_replay_io_submit(self, key) -> bool: return False
def get_replay_io_drain(self, key): return None
```

## Verification

1. `make lint` — must pass
2. `make test` — must pass (existing tests unaffected; no live I/O in tests)
3. New test file `tests/core/test_send_receive_replay.py`:
   - Submit/drain events are recorded in scan log during live-like execution
   - Interpreted replay reconstructs correct sending/success/error/exception_response tag values
   - Receive replay reconstructs destination tag values
   - `with Rung(Sending): ...` evaluates correctly during replay
   - Multi-scan in-flight period (submit, N in-flight scans, drain)
   - Compiled replay applies correct tag writes via `_kernel.tags`
   - Forks remain inert (no recorded I/O applied)
