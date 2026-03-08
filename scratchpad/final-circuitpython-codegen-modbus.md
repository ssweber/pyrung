# Modbus TCP Server & Client for CircuitPython Codegen

## Context

The P1AM-200 running pyrung-generated CircuitPython needs to be a drop-in Click PLC replacement over Modbus TCP. C-more HMIs, pyclickplc, and SCADA systems should connect to it on port 502 and see the same register layout as a real Click PLC. Additionally, the P1AM-200 needs to act as a Modbus master to read/write remote devices (other Click PLCs, VFDs, etc.) via send/receive ladder instructions.

No usable CircuitPython Modbus library exists. We write ~300 lines of server and ~200 lines of client code, emitted inline by the codegen. Tags are accessed directly (no duplicate register bank) to save RAM.

## Phase 1: Modbus TCP Server Codegen

### 1.1 New config type

Create `src/pyrung/circuitpy/modbus.py`:

```python
@dataclass(frozen=True)
class ModbusServerConfig:
    ip: str                       # "192.168.1.200"
    subnet: str = "255.255.255.0"
    gateway: str = "192.168.1.1"
    dns: str = "0.0.0.0"
    port: int = 502
    max_clients: int = 2          # 1-7 (W5500 has 8 sockets, 1 for listener)
```

Export from `src/pyrung/circuitpy/__init__.py`.

### 1.2 Extend `generate_circuitpy()` signature

In `src/pyrung/circuitpy/codegen/generate.py`:

```python
def generate_circuitpy(
    program, hw, *,
    target_scan_ms, watchdog_ms=None, runstop=None,
    modbus_server: ModbusServerConfig | None = None,
    tag_map: TagMap | None = None,
) -> str:
```

- `modbus_server` requires `tag_map` (raise ValueError if server enabled without tag_map)
- `tag_map` without `modbus_server` is allowed (future: validation-only use)
- Store both on `CodegenContext`

### 1.3 Extend `CodegenContext`

In `src/pyrung/circuitpy/codegen/context.py`, add fields:

```python
modbus_server: ModbusServerConfig | None = None
tag_map: TagMap | None = None
```

### 1.4 New module: `src/pyrung/circuitpy/codegen/render_modbus.py`

Contains all Modbus-specific rendering functions, called from `render.py`. This keeps `render.py` from growing too large.

#### 1.4a Ethernet setup rendering

```python
def _render_ethernet_setup(ctx) -> list[str]:
```

Emits:
- `import digitalio` (if not already needed)
- `from adafruit_wiznet5k.adafruit_wiznet5k import WIZNET5K`
- `import adafruit_wiznet5k.adafruit_wiznet5k_socket as _mb_socket`
- SPI/CS pin setup for P1AM-ETH (`board.D5`)
- WIZNET5K initialization with static IP
- `_mb_socket.set_interface(_eth)`

#### 1.4b Accessor function generation

```python
def _render_modbus_accessors(ctx) -> list[str]:
```

At codegen time:
1. Walk `ctx.tag_map.mapped_slots()` to get all mapped Click addresses
2. For each `MappedSlot`, call `pyclickplc.modbus.plc_to_modbus(memory_type, address)` to compute the Modbus address
3. Group into coil-space and register-space entries
4. For register entries, determine the data type from `pyclickplc.banks.BANKS[memory_type].data_type`

Generate four accessor functions:

**`_mb_read_coil(addr) -> bool | None`** — if/elif chain:
- Mapped coil addresses → return tag symbol value (e.g. `_b_Slot1[0]`)
- Valid-but-unmapped ranges → `return False`
- Invalid → `return None`

**`_mb_write_coil(addr, val) -> bool`** — if/elif chain:
- Mapped writable addresses → set tag, `return True`
- Mapped read-only → `return True` (silently discard, like Click)
- Valid-but-unmapped → `return True`
- Invalid → `return False`

**`_mb_read_reg(addr) -> int | None`** — if/elif chain returning 16-bit unsigned:
- Width-1 types (DS/INT): `struct.unpack('<H', struct.pack('<h', val))[0]`
- Width-1 types (DH/HEX, TD/INT): similar with appropriate format
- Width-2 types (DD/INT2): two entries per mapped value (lo at base, hi at base+1)
  - `struct.unpack('<HH', struct.pack('<i', val))[0]` for lo
  - `struct.unpack('<HH', struct.pack('<i', val))[1]` for hi
- Width-2 types (DF/FLOAT): same pattern with `'<f'`
- TXT: two chars per register — `ord(lo_char) | (ord(hi_char) << 8)`
- Valid-but-unmapped → `return 0`
- Invalid → `return None`

**`_mb_write_reg(addr, val) -> bool`** — if/elif chain:
- Width-1: unpack and set tag
- Width-2: read-modify-write (read current 32-bit value, replace one half, write back)
- TXT: split into lo/hi bytes, write both chars
- Valid-but-unmapped → `return True`
- Invalid → `return False`

**Valid address ranges** — derived from `MODBUS_MAPPINGS` and `BANKS` at codegen time:
- Coils: X [0, 272), Y [8192, 8464), C [16384, 18384), T [45056, 45556), CT [49152, 49402), SC [61440, 62440)
- Registers: DS [0, 4500), DD [16384, 18384), DH [24576, 25076), DF [28672, 29672), TXT [36864, 37364), TD [45056, 45556), CTD [49152, 49652), XD [57344, 57361), YD [57856, 57873), SD [61440, 62440)

These are computed from `MODBUS_MAPPINGS[bank].base` and `BANKS[bank].max_addr` and emitted as literal range checks.

#### 1.4c Protocol handler rendering

```python
def _render_modbus_protocol(ctx) -> list[str]:
```

Emits ~120 lines of static protocol code (same for every program):

**`_mb_handle(data, n) -> bytes | None`**:
- Parse MBAP: `tid`, `pid` (must be 0), `length`, `uid`
- Extract FC from `data[7]`
- Dispatch to FC handler
- Return response bytes or None on parse error

**FC handlers** (only emit handlers for FCs that have mapped addresses):
- **FC 1/2 (read coils/discrete inputs)**: Parse start+count, iterate calling `_mb_read_coil()`, pack bits into response bytes
- **FC 3/4 (read holding/input registers)**: Parse start+count, iterate calling `_mb_read_reg()`, pack 16-bit values into response
- **FC 5 (write single coil)**: Parse address+value (0xFF00=True, 0x0000=False), call `_mb_write_coil()`
- **FC 6 (write single register)**: Parse address+value, call `_mb_write_reg()`
- **FC 15 (write multiple coils)**: Parse start+count+bytes, unpack bits, iterate `_mb_write_coil()`
- **FC 16 (write multiple registers)**: Parse start+count+bytes, unpack registers, iterate `_mb_write_reg()`

**`_mb_err(tid, uid, fc, code) -> bytes`**: Build exception response.

#### 1.4d Server socket management

```python
def _render_modbus_server(ctx) -> list[str]:
```

Emits:
- Server socket setup: bind, listen, settimeout(0)
- Client socket array: `_mb_clients = [None] * max_clients`
- Pre-allocated receive buffer: `_mb_buf = bytearray(260)`

**`service_modbus_server()`**:
- Accept new connections into empty client slots
- For each active client: non-blocking recv into `_mb_buf`
- If data received: call `_mb_handle()`, send response
- Handle disconnections (OSError → close + clear slot)
- Return immediately if nothing pending

### 1.5 Integrate into `render.py`

In `_render_code()`, insert after section 3 (hardware bootstrap):
- If `ctx.modbus_server`: call `_render_ethernet_setup(ctx)` for ethernet init

After section 5 (tag declarations):
- If `ctx.modbus_server`: call `_render_modbus_server(ctx)` for socket setup + buffer

After section 11 (main function) / before section 12 (I/O helpers):
- If `ctx.modbus_server`: call `_render_modbus_accessors(ctx)` + `_render_modbus_protocol(ctx)`

In `_render_scan_loop()`, after `_write_outputs()` and before edge prev snapshots:
- If `ctx.modbus_server`: emit `service_modbus_server()` call **unconditionally** (outside the run/stop block — Modbus stays active even in STOP mode, matching Click behavior)

### 1.6 Imports handling

The ethernet-specific imports (`adafruit_wiznet5k`, `digitalio` for CS pin) are only emitted when `modbus_server` is configured. The codegen already conditionally emits imports (e.g., `digitalio` for board switch/LED).

## Phase 2: Test Infrastructure

### 2.1 Fixture generation

Create `tests/fixtures/generate_modbus_fixtures.py` (run once, committed):
- Start a `ClickServer` with `MemoryDataProvider`
- Pre-populate known values across all bank types
- For each FC and bank type, construct raw Modbus TCP requests and capture (request, response) pairs
- Save as `tests/fixtures/modbus_fixtures.json`

Cover: FC01-06, FC15-16 across DS, DD, DH, DF, TXT, TD, CTD, XD, YD, SD, X, Y, C, T, CT, SC. Edge cases: first/last address per bank, multi-register reads spanning width-2 boundaries, error responses for invalid addresses.

### 2.2 Unit tests

Create `tests/circuitpy/test_modbus_codegen.py`:

**Accessor generation tests:**
- Given a TagMap with known mappings, verify generated accessor code contains correct address→symbol mappings
- Verify valid-but-unmapped ranges return defaults
- Verify invalid addresses return None/False

**Protocol handler tests** (the key test — exec generated code, feed raw bytes):
- Use `_run_single_scan_source()` pattern: exec generated code with mocked CircuitPython modules
- Call `_mb_handle(request_bytes, len)` directly in the exec'd namespace
- Compare response bytes to fixtures (skip MBAP transaction ID bytes 0-1)

**Integration tests:**
- Generate code for a program with known tag mappings
- Set tag values via the exec'd namespace
- Send Modbus read requests, verify returned values match
- Send Modbus write requests, verify tag values change

### 2.3 Cross-validation with pyclickplc ClickServer

Parameterized test: for each fixture, verify both pyclickplc's ClickServer and the generated CircuitPython handler produce identical responses.

## Phase 3: Modbus TCP Client Codegen (builds on Phase 1 infrastructure)

### 3.1 Client config

Add to `src/pyrung/circuitpy/modbus.py`:

```python
@dataclass(frozen=True)
class ModbusTarget:
    name: str           # logical name for this target
    ip: str             # target IP
    port: int = 502
    device_id: int = 1
    timeout_ms: int = 1000

@dataclass(frozen=True)
class ModbusClientConfig:
    targets: tuple[ModbusTarget, ...]
```

### 3.2 CircuitPython send/receive instructions

Create `src/pyrung/circuitpy/send_receive.py` — CircuitPython-native versions of the Click dialect's `send()` and `receive()` that compile to non-blocking state machine code instead of threaded ClickClient calls.

### 3.3 Client state machine codegen

Add to `render_modbus.py`:

**Per-target state**: socket, state enum (IDLE/CONNECTING/SENDING/WAITING/DONE/ERROR), request/response buffers, transaction ID counter.

**`service_modbus_client()`**: For each target with a pending request:
- IDLE → connect socket (non-blocking)
- CONNECTING → check connection status
- SENDING → send request bytes
- WAITING → non-blocking recv, parse response, write results to tags, set done/error flags
- DONE/ERROR → ladder logic reads flags, resets on next trigger

### 3.4 Extend `generate_circuitpy()`

Add `modbus_client: ModbusClientConfig | None = None` parameter. Requires `tag_map`. Emits client code + `service_modbus_client()` in scan loop.

## Files to Create/Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/pyrung/circuitpy/modbus.py` | Create | ModbusServerConfig, ModbusTarget, ModbusClientConfig |
| `src/pyrung/circuitpy/__init__.py` | Modify | Export new config types |
| `src/pyrung/circuitpy/codegen/generate.py` | Modify | Add modbus_server, tag_map params |
| `src/pyrung/circuitpy/codegen/context.py` | Modify | Add modbus fields to CodegenContext |
| `src/pyrung/circuitpy/codegen/render.py` | Modify | Call render_modbus functions at correct insertion points |
| `src/pyrung/circuitpy/codegen/render_modbus.py` | Create | All Modbus rendering (ethernet, accessors, protocol, sockets) |
| `tests/fixtures/generate_modbus_fixtures.py` | Create | Fixture generator script |
| `tests/fixtures/modbus_fixtures.json` | Create | Generated test fixtures |
| `tests/circuitpy/test_modbus_codegen.py` | Create | Server codegen + protocol tests |

## Key pyclickplc References (codegen-time only, not runtime)

- `pyclickplc.modbus.plc_to_modbus(bank, index)` — compute Modbus addresses for mapped slots
- `pyclickplc.modbus.MODBUS_MAPPINGS` — base addresses and valid ranges for range checks
- `pyclickplc.banks.BANKS` — max_addr for computing valid ranges
- `pyclickplc.banks.DataType` — determine pack/unpack format per bank
- `pyclickplc.modbus.MODBUS_WIDTH` / `STRUCT_FORMATS` — register width and struct format per type

## Verification

1. `make test` — all existing tests pass (no regressions)
2. New codegen tests: generate code with modbus_server enabled, exec with mocked modules, verify accessor functions return correct values for mapped/unmapped/invalid addresses
3. Protocol tests: feed raw Modbus TCP request bytes to `_mb_handle()`, compare response bytes to fixtures from pyclickplc ClickServer
4. Cross-validation: parameterized test proving generated handler and ClickServer produce identical responses for all fixtures
5. Manual hardware test (post-merge): deploy to P1AM-200, connect pyclickplc client and C-more HMI
