# Click-Compatible Modbus TCP Server & Client for P1AM-200 CircuitPython Codegen

## Objective

Build a minimal Modbus TCP server and client for CircuitPython on the P1AM-200, embedded in pyrung's CircuitPython code generator output. Both must be **byte-compatible with a real AutomationDirect Click PLC** — verified by recording actual Click PLC traffic and replaying it as test fixtures.

**Server:** The P1AM-200 exposes its tags as Click-compatible Modbus registers so it can be a drop-in Click PLC replacement. C-more HMIs, pyclickplc, SCADA systems, and any Modbus master talk to it without modification, using the same addresses and nickname CSV exports pyclickplc already provides.

**Client:** The P1AM-200 acts as a Modbus master to read/write registers on other devices — other Click PLCs, remote I/O, VFDs, etc. This supports pyrung's send/receive ladder instructions. Uses CircuitPython's asyncio (supported on SAMD51) for non-blocking operation within the scan loop.

This is NOT a general-purpose Modbus library. It is a Click-address-aware, ~300-line server (supporting 2 simultaneous clients) and ~200-line client that speaks just enough Modbus TCP to interoperate with the AutomationDirect ecosystem.

## Context: What Already Exists

### pyclickplc (reference implementation — source of truth)

pyclickplc is an existing Python library. Its Modbus mapping functions define the byte-level contract the CircuitPython implementation must reproduce.

**Address model:**
- Canonical normalized Click addresses: X, Y, C, T, CT, SC, DS, DD, DH, DF, XD, YD, TD, CTD, SD, TXT
- `parse_address(str) → (memory_type, mdb_address)` — e.g., `"X001" → ("X", 1)`, `"XD0u" → ("XD", 1)`
- `normalize_address(str) → str` — e.g., `"x1" → "X001"`, `"ds1" → "DS1"`
- `format_address_display(memory_type, mdb_address) → str`

**Modbus mapping (the critical contract):**
- `plc_to_modbus(bank, index) → (modbus_address, register_count)` — Click address → Modbus address
- `modbus_to_plc(address, is_coil) → (bank, display_index) | None` — Modbus address → Click address
- These functions define the exact register/coil layout. If the CircuitPython server maps addresses differently, C-more won't work.

**Value packing:**
- `pack_value(value, data_type) → list[int]` — Python value → Modbus register(s)
- `unpack_value(registers, data_type) → value` — Modbus register(s) → Python value
- Supports all Click data types: BIT, INT, INT2, HEX, FLOAT, TXT

**Bank configurations:**
```python
# pyclickplc BANKS — this is the register layout we're replicating
"X":   BankConfig("X",   1, 816,  BIT,   valid_ranges=_SPARSE_RANGES)
"Y":   BankConfig("Y",   1, 816,  BIT,   valid_ranges=_SPARSE_RANGES)
"C":   BankConfig("C",   1, 2000, BIT)
"T":   BankConfig("T",   1, 500,  BIT,   interleaved_with="TD")
"CT":  BankConfig("CT",  1, 250,  BIT,   interleaved_with="CTD")
"SC":  BankConfig("SC",  1, 1000, BIT)
"DS":  BankConfig("DS",  1, 4500, INT)
"DD":  BankConfig("DD",  1, 1000, INT2)
"DH":  BankConfig("DH",  1, 500,  HEX)
"DF":  BankConfig("DF",  1, 500,  FLOAT)
"XD":  BankConfig("XD",  0, 16,   HEX)
"YD":  BankConfig("YD",  0, 16,   HEX)
"TD":  BankConfig("TD",  1, 500,  INT,   interleaved_with="T")
"CTD": BankConfig("CTD", 1, 250,  INT2,  interleaved_with="CT")
"SD":  BankConfig("SD",  1, 1000, INT)
"TXT": BankConfig("TXT", 1, 1000, TXT)
```

**Existing server/client (CPython reference):**
- `ClickServer` — Async Modbus TCP server with `MemoryDataProvider` (this is what we validate fixtures against before writing CircuitPython code)
- `ClickClient` — Async Modbus TCP client with bank accessors (`plc.ds[1]`, `plc.addr.read("DS1")`, etc.)
- `ModbusService` — Sync wrapper around ClickClient for UI/service callers

**File I/O:**
- `read_csv`/`write_csv` for Click nickname CSV files — the CSV export is what C-more imports for tag configuration
- `read_cdv`/`write_cdv` for DataView CDV files

Docs: https://ssweber.github.io/pyclickplc/
Full API reference: https://ssweber.github.io/pyclickplc/llms.txt

### pyrung (the deploying framework)

pyrung's CircuitPython dialect generates self-contained `.py` files that run on the P1AM-200. Key concepts:

- **TagMap:** Maps ladder logic tags to Click addresses. The same slot-to-address convention used for Click hardware applies:
  - Slot I/O → X/Y banks with Click's slot-based address ranges (slot 1 = X/Y 100-series, slot 2 = 200-series, etc.)
  - Internal Bool → C bank
  - Internal Int → DS bank
  - Internal DInt → DD bank
  - Internal Float → DF bank
  - Timer done/acc → T/TD banks
  - Counter done/acc → CT/CTD banks
- **Scan loop:** `read_inputs() → execute_rungs() → write_outputs() → pace_scan()`
- **Send/Receive instructions:** Ladder instructions that read/write remote Modbus devices — the client implements these

Docs: https://ssweber.github.io/pyrung/
CircuitPython dialect: https://ssweber.github.io/pyrung/dialects/circuitpy/index.md
Click dialect (TagMap): https://ssweber.github.io/pyrung/dialects/click/index.md

### CircuitPython environment on P1AM-200

- SAMD51 microcontroller, CircuitPython runtime
- Ethernet via WIZnet W5500 with `adafruit_wiznet5k` library — provides raw socket API
- `asyncio` supported on SAMD51 — relevant for non-blocking client operations
- P1AM-200's built-in EEPROM contains a unique MAC address via `p1am_200_helpers.get_ethernet()`
- Memory-constrained: every byte matters, no pymodbus, no heavy abstractions
- No existing CircuitPython Modbus library is suitable (confirmed by research)

## Architecture

### Where it lives

Both server and client code are **emitted by the code generator**, not separate libraries. They become part of the self-contained `code.py` on the CIRCUITPY drive. No additional dependencies beyond `adafruit_wiznet5k` (which ships with the P1AM-200).

### Networking setup

```python
import board
import busio
import digitalio
from adafruit_wiznet5k.adafruit_wiznet5k import WIZNET5K
import adafruit_wiznet5k.adafruit_wiznet5k_socket as socket

cs = digitalio.DigitalInOut(board.D5)  # P1AM-ETH CS pin
spi = busio.SPI(board.SCK, MOSI=board.MOSI, MISO=board.MISO)
eth = WIZNET5K(spi, cs)
# MAC from EEPROM via p1am_200_helpers.get_ethernet()
```

### Scan loop integration

The generated scan loop gains up to two additional steps:

```
while True:
    read_inputs()
    execute_rungs()
    write_outputs()
    service_modbus_server()   # ← non-blocking, <0.1ms when idle
    service_modbus_client()   # ← non-blocking, processes pending send/receive
    pace_scan()
```

Both `service_modbus_server()` and `service_modbus_client()` are non-blocking. They return immediately when idle. Only the steps enabled by codegen parameters are emitted.

### Tag storage

The generated code already declares tag variables (bools, ints, floats, lists for blocks). Both server and client read and write these same variables directly — no separate register bank copy.

- **Server:** Modbus reads see the latest scan output; Modbus writes take effect on the next scan input. Matches Click PLC behavior.
- **Client:** Send/receive results are written to tag variables between scans. Ladder logic reads them on the next scan.

---

## Part 1: Modbus TCP Server

### Protocol

- **Transport:** TCP on port 502, up to 2 simultaneous client connections (matching Click PLC behavior). W5500 has 8 hardware sockets so this is not resource-constrained.
- **Framing:** 7-byte MBAP header (transaction ID, protocol ID, length, unit ID) + PDU
- **Supported function codes:** FC01 (read coils), FC02 (read discrete inputs), FC03 (read holding registers), FC04 (read input registers), FC05 (write single coil), FC06 (write single register), FC15 (write multiple coils), FC16 (write multiple registers)
- **Error responses:** Standard Modbus exception codes (illegal function, illegal data address, illegal data value)

### Non-blocking server pattern

```python
server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server_socket.bind(('', 502))
server_socket.listen(2)  # 2 simultaneous clients, matching Click PLC
server_socket.settimeout(0)  # non-blocking

# Track up to 2 client sockets
client_sockets = [None, None]

def service_modbus_server():
    # Accept new connections into empty client slots
    # Check both client sockets for pending data
    # Parse MBAP header (transaction ID routes response to correct client)
    # Dispatch PDU to function code handler
    # Read/write from tag arrays (same variables the scan loop uses)
    # Send response on the originating client socket
    # Returns immediately if nothing pending
```

### Register mapping and default values

The generated server allocates storage only for tags that are actually mapped via the TagMap. However, it does **not** return Modbus exceptions for unmapped addresses. Instead, it follows real Click PLC behavior:

- **Mapped address (in TagMap):** Returns the current tag value, accepts writes that update the tag.
- **Unmapped but valid Click address (within a BANKS range):** Returns the default value — 0 for registers, False for coils. Writes are accepted and silently discarded (or optionally stored if we want write-then-readback to work for unmapped addresses).
- **Invalid address (outside all Click bank ranges):** Returns Modbus exception code 02 (illegal data address).

This matters for C-more compatibility. If a C-more screen reads DS100 but the program only maps DS1-DS10, the panel gets a clean 0 — exactly what a real Click would return for an uninitialized register. No Modbus errors, no broken HMI screens.

The Modbus address mapping follows pyclickplc's `plc_to_modbus()` and `modbus_to_plc()` exactly. These are the source of truth.

---

## Part 2: Modbus TCP Client

### Purpose

Implements pyrung's send/receive ladder instructions. The P1AM-200 acts as Modbus master, reading/writing registers on remote devices (other Click PLCs, remote I/O modules, VFDs, sensors).

### Protocol

- **Transport:** TCP on configurable IP:port per target device
- **Framing:** Same MBAP + PDU as server, but constructing requests and parsing responses
- **Function codes:** FC03 (read holding registers), FC06 (write single register), FC16 (write multiple registers) — expand as ladder instructions require
- **Timeout:** Configurable per-device, with error flag visible to ladder logic

### Non-blocking client pattern

Uses CircuitPython asyncio (supported on SAMD51) for non-blocking operation:

```python
def service_modbus_client():
    # Check for pending send/receive requests from ladder logic
    # If a request is pending and no transaction in flight:
    #   - Build MBAP + PDU for the request
    #   - Send to target device
    #   - Set state to "waiting for response"
    # If waiting for response and data available:
    #   - Parse response
    #   - Write results to tag variables
    #   - Set done/error flags for ladder logic
    # Returns immediately if nothing pending or waiting
```

### Integration with ladder logic

Send/receive instructions in ladder logic set request parameters (target address, register range, values) and check done/error flags. The client processes these between scans:

- **Send (write):** Ladder sets target + values → client writes to remote device → sets done flag
- **Receive (read):** Ladder sets target + register range → client reads from remote device → writes results to tags → sets done flag
- **Error handling:** Timeout or exception response → sets error flag + error code, ladder logic decides what to do

---

## Part 3: Bootstrap Strategy — Packet Recording

The core testing strategy is to record real Modbus TCP transactions from a physical Click PLC and replay them as test fixtures against every implementation.

### Phase 1: Build a Recording Proxy

Write a TCP proxy that sits between any Modbus client and a real Click PLC:

```
Client :5020 → Proxy → Click PLC :502
Client :5020 ← Proxy ← Click PLC :502
```

Records every (request_bytes, response_bytes) pair. Modbus TCP framing is self-contained — the MBAP header (7 bytes) includes a length field, so extracting complete transactions is straightforward.

**Implementation:**
- Simple asyncio TCP proxy, ~100 lines
- Binds on local port (e.g., 5020), forwards to Click PLC IP on 502
- Logs raw bytes for each complete Modbus TCP transaction
- Tags each recording with metadata: source client, Click firmware version, timestamp
- Serializes to JSON fixtures with hex-encoded byte strings
- Can live in `pyclickplc/tools/` or a standalone script

### Phase 2: Generate Traffic Through the Proxy

Run existing client implementations through the proxy to generate comprehensive fixtures:

1. **pyclickplc test suite** — Point the test suite at the proxy instead of a direct Click connection. Existing tests become the traffic generator. Every test that reads/writes produces a fixture for free.
2. **pyclickplc client manual exploration** — Systematically read/write across all Click address types: X, Y, C, T, CT, DS, DD, DH, DF, XD, YD, TD, CTD, SD, TXT. Cover edge cases: first address, last address, boundary of each bank, multi-register types (DD, DF, TXT).
3. **clickplc (third-party library)** — Run the same operations through the `clickplc` package to get a second implementer's perspective on how they construct requests.
4. **C-more HMI** — If available, let a C-more panel poll the Click through the proxy for several minutes. This captures the exact request patterns C-more uses in practice — which function codes, what poll sizes, what address ranges. This is the most valuable fixture set.

### Phase 3: Fixture Format

```json
[
  {
    "description": "read DS1-DS10 via FC03",
    "request_hex": "00010000000601030000000A",
    "response_hex": "00010000001501030A00000000000000000000000000",
    "source_client": "pyclickplc",
    "click_firmware": "3.20",
    "timestamp": "2026-02-28T12:00:00Z",
    "click_address_range": "DS1-DS10",
    "function_code": 3
  }
]
```

### Phase 4: Replay Tests

```python
import pytest

@pytest.mark.parametrize("fixture", load_click_fixtures())
def test_server_matches_click_hardware(fixture, server):
    """Server must produce byte-identical responses to real Click PLC."""
    request = bytes.fromhex(fixture["request_hex"])
    expected = bytes.fromhex(fixture["response_hex"])
    actual = server.handle_raw_frame(request)
    # Normalize: skip MBAP transaction ID (bytes 0-1) as these vary per session
    assert actual[2:] == expected[2:]
```

For stateful sequences (write then read-back), replay fixtures in recorded order against a fresh server instance.

### Phase 5: Cross-Validation Matrix

| Test Target | Purpose |
|---|---|
| Real Click PLC (via proxy) | Ground truth — these generated the fixtures |
| pyclickplc ClickServer | CPython reference implementation |
| New CircuitPython server | The thing we're building |
| P1AM-200 on hardware | End-to-end validation on actual target |

All non-hardware targets should produce identical responses (modulo transaction IDs). Discrepancies between pyclickplc's ClickServer and the real Click reveal bugs in the reference implementation. Discrepancies between the CircuitPython server and the Click fixtures reveal bugs in the new implementation.

**Client validation follows the same pattern in reverse:** record the requests a real Click PLC client (or pyclickplc ClickClient) sends, then verify the CircuitPython client produces identical request bytes for the same operations.

---

## Part 4: Code Generator Integration

Add parameters to `generate_circuitpy()`:

```python
source = generate_circuitpy(
    logic, hw,
    target_scan_ms=10.0,
    watchdog_ms=500,
    # Server options
    modbus_server=True,
    modbus_ip="192.168.1.200",
    modbus_subnet="255.255.255.0",
    modbus_gateway="192.168.1.1",
    tag_map=my_tag_map,
    # Client options
    modbus_client=True,
    modbus_targets=[
        {"name": "plc2", "ip": "192.168.1.100", "port": 502, "device_id": 1},
    ],
)
```

When `modbus_server=True`, the generator emits:
1. Ethernet/socket imports and setup
2. Register lookup tables (mapping Modbus addresses → tag variables), generated from the TagMap
3. MBAP parser and PDU dispatcher
4. Function code handlers (FC01-06, FC15, FC16)
5. `service_modbus_server()` call in the scan loop

When `modbus_client=True`, the generator emits:
1. Client socket management per target device
2. Request builder functions for send/receive instructions
3. Response parser with timeout and error handling
4. `service_modbus_client()` call in the scan loop

When both are `False` (default), none of this is emitted — the generated code is unchanged from current behavior.

---

## Implementation Sequence

### Step 1: Recording Proxy
- Simple asyncio TCP proxy, ~100 lines
- Records to a JSON fixture file
- Lives in `pyclickplc/tools/` or standalone script

### Step 2: Capture Baseline Fixtures
- Run pyclickplc test suite through proxy against real Click
- Manual exploration of all bank types through proxy
- Run clickplc (third-party) through proxy for cross-reference
- Capture C-more polling patterns if HMI is available

### Step 3: Validate pyclickplc ClickServer Against Fixtures
- Before writing any CircuitPython code, confirm the existing ClickServer passes the fixture suite
- Fix any discrepancies — these are bugs in the reference server
- This gives you a known-good CPython server to develop against

### Step 4: Build CircuitPython Modbus Server
- Port the minimal subset needed from pyclickplc's server logic
- Raw socket handling via `adafruit_wiznet5k` socket API
- MBAP frame parsing with `struct`
- Click address mapping (reproduce `plc_to_modbus`/`modbus_to_plc` behavior for mapped addresses)
- Value packing for the data types pyrung actually uses
- Non-blocking `service_modbus_server()` integrated with scan loop
- Target: ~300 lines, no dependencies beyond CircuitPython stdlib + wiznet5k

### Step 5: Test CircuitPython Server Against Fixtures
- Run fixture replay suite against the server (in CPython first for fast iteration, then on hardware)
- Byte-identical responses = compatible with Click ecosystem

### Step 6: Build CircuitPython Modbus Client
- Request builder: construct MBAP + PDU for FC03, FC06, FC16
- Response parser: validate MBAP, extract register values
- Non-blocking `service_modbus_client()` with state machine (idle → sending → waiting → done/error)
- Connection management per target device
- asyncio integration for non-blocking socket operations
- Target: ~200 lines

### Step 7: Test Client Against Fixtures
- Record what pyclickplc ClickClient sends for the same operations
- Verify CircuitPython client produces identical request bytes
- Test response parsing against recorded Click PLC responses

### Step 8: End-to-End Validation
- Deploy to P1AM-200 with P1AM-ETH
- Server: Point pyclickplc client at P1AM-200 — verify reads/writes match Click behavior
- Server: Point C-more at P1AM-200 — verify HMI works with imported nickname CSV
- Client: P1AM-200 reads/writes a real Click PLC — verify round-trip
- Capture all traffic via proxy to confirm wire-level compatibility

---

## Key pyclickplc Functions to Reference

These are the source of truth for Click Modbus behavior. The CircuitPython implementation doesn't import these — it reproduces their behavior for the subset of addresses pyrung uses.

- `plc_to_modbus(bank, index)` — Maps Click address → Modbus address + register count
- `modbus_to_plc(address, is_coil)` — Reverse maps Modbus address → Click address
- `pack_value(value, data_type)` — Python value → Modbus register(s)
- `unpack_value(registers, data_type)` — Modbus register(s) → Python value
- `BANKS` — Complete bank configuration (ranges, data types, interleaving)
- `BankConfig` — Per-bank metadata including sparse ranges for X/Y

## Reference Material

- **pyclickplc docs:** https://ssweber.github.io/pyclickplc/
- **pyclickplc llms.txt:** https://ssweber.github.io/pyclickplc/llms.txt
- **pyclickplc BANKS and addressing:** https://ssweber.github.io/pyclickplc/reference/api/advanced/index.md
- **pyrung docs:** https://ssweber.github.io/pyrung/
- **pyrung llms.txt:** https://ssweber.github.io/pyrung/llms.txt
- **pyrung CircuitPython dialect:** https://ssweber.github.io/pyrung/dialects/circuitpy/index.md
- **pyrung Click dialect (TagMap):** https://ssweber.github.io/pyrung/dialects/click/index.md
- **P1AM-ETH docs:** https://facts-engineering.github.io/modules/P1AM-ETH/P1AM-ETH.html
- **P1AM-SERIAL docs:** https://facts-engineering.github.io/modules/P1AM-SERIAL/P1AM-SERIAL.html
- **adafruit_wiznet5k:** https://github.com/adafruit/Adafruit_CircuitPython_Wiznet5k
- **P1AM-200 helpers (MAC, ethernet):** https://github.com/facts-engineering/CircuitPython_p1am_200_helpers

## Scope

**In scope:**
- Modbus TCP server embedded in generated code (~300 lines)
- Modbus TCP client embedded in generated code (~200 lines)
- Click-compatible register mapping via TagMap
- Non-blocking integration with scan loop
- Server: FC01, FC02, FC03, FC04, FC05, FC06, FC15, FC16
- Client: FC03, FC06, FC16 (expand as ladder instructions require)
- Up to 2 simultaneous client connections (server), multiple target connections (client)
- Default zero responses for valid but unmapped Click addresses
- Packet recording proxy for bootstrap testing
- Fixture-based cross-validation against real Click hardware

**Out of scope (future work):**
- Modbus RTU server/client (shares PDU dispatch, different transport — ~200 additional lines each)
- More than 2 simultaneous server client connections
- DHCP (static IP only for v1)

## Success Criteria

1. Recording proxy captures clean fixture files from real Click hardware
2. pyclickplc ClickServer passes 100% of fixtures (after any bug fixes)
3. CircuitPython server passes 100% of fixtures (modulo transaction IDs)
4. CircuitPython client produces byte-identical requests to pyclickplc ClickClient
5. C-more HMI connects to P1AM-200 and displays/controls values correctly using imported nickname CSV
6. pyclickplc client can read/write P1AM-200 identically to a real Click PLC
7. P1AM-200 client can read/write a real Click PLC correctly
8. Server fits in ~300 lines, client in ~200 lines, both within P1AM-200 memory constraints
