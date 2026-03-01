# Modbus TCP/RTU on P1AM-200 with CircuitPython: Research Report

## Executive Summary

CircuitPython on the P1AM-200 **can** support Modbus TCP (server and client) and Modbus RTU, but there is no production-ready, turnkey library today. The most viable path for your pyrung project is to **write a minimal, purpose-built Modbus TCP server** (~200–300 lines) on top of the existing `adafruit_wiznet5k` socket layer, targeting only the function codes you need. This is realistic and achievable. Below is the full analysis.

---

## 1. Modbus TCP Server on CircuitPython

### Socket Layer: Fully Capable

The `adafruit_wiznet5k` library provides a complete TCP socket API for the W5500, including server-side operations. The critical primitives are all present:

- `socket_listen()` — listen on any port (including 502)
- `socket_accept()` — accept incoming TCP connections
- `socket_read()` / `socket_write()` — raw byte I/O
- `settimeout(0)` — non-blocking mode (returns immediately if no data)
- The W5500 hardware supports up to **8 simultaneous sockets** (the chip manages TCP/IP offload in hardware)

The library also provides a `SocketPool` abstraction via `adafruit_wiznet5k_socketpool` that mirrors CPython's `socket` module interface. A WSGI web server example already ships with the library, demonstrating that TCP server operation on the W5500 is proven and stable from CircuitPython.

**Bottom line:** Hosting a server on port 502 that accepts TCP connections and exchanges raw bytes is fully supported. The W5500's hardware TCP/IP stack handles the networking heavy lifting; CircuitPython only needs to handle the Modbus application layer (MBAP header + PDU).

### Existing Modbus Libraries: Gaps and Issues

**TwinDimension CircuitPython Modbus Fork** (`TwinDimensionIOT/TwinDimension-CircuitPython-Modbus`):
- Forked from `brainelectronics/micropython-modbus`
- Advertises both TCP and RTU, client and host modes
- **Problem:** Despite the name "CircuitPython" in the repo title, it is essentially a MicroPython library. It depends on MicroPython-specific APIs (`machine`, `network`, `mip`, `upip`) that do not exist in CircuitPython. The TCP layer uses MicroPython's native `socket` module and `network.WLAN`, not `adafruit_wiznet5k`
- **Problem:** Has not been tested or adapted for SAMD51 + W5500 hardware. The examples target ESP32 with WiFi
- **Assessment:** Would require significant porting work to CircuitPython. The Modbus protocol parsing logic (PDU construction, function code dispatch, CRC calculation) could be salvaged, but the transport layer needs a complete rewrite

**Freeno83 Circuit-Python-Modbus** (`Freeno83/Circuit-Python-Modbus`):
- A CircuitPython port of MinimalModbus
- **RTU Client (master) only** — supports FC1, FC2, FC3, FC4, FC5, FC6, FC15, FC16
- No server/slave mode
- No TCP support
- Works with `busio.UART` for RS-485
- **Assessment:** Useful reference for RTU client framing and CRC, but does not address your primary need (TCP server)

**No other CircuitPython Modbus libraries exist.** The pyModbusTCP and pymodbus libraries are CPython-only and depend on `threading`, `asyncio`, and full `socket` modules that CircuitPython lacks.

### What a Minimal Implementation Looks Like

Modbus TCP is a simple protocol. A server needs to:

1. Listen on port 502
2. Accept a TCP connection
3. Read 7+ bytes (MBAP header: transaction ID [2], protocol ID [2], length [2], unit ID [1])
4. Read `length - 1` more bytes (the PDU)
5. Dispatch on function code byte
6. Read/write the appropriate register bank (a `bytearray` in memory)
7. Build and send the response (MBAP header + response PDU)

The entire Modbus TCP application protocol (MBAP + PDU parsing + FC1/2/3/4/5/6/15/16 dispatch) can be implemented in roughly **200–300 lines of CircuitPython** with no external dependencies beyond `adafruit_wiznet5k`. There is no need for threads, asyncio, or complex state machines.

### Supported Function Codes

All standard function codes you listed are straightforward to implement:

| FC | Name | Operation |
|----|------|-----------|
| 01 | Read Coils | Read N bits from coil bank |
| 02 | Read Discrete Inputs | Read N bits from DI bank |
| 03 | Read Holding Registers | Read N×16-bit from HR bank |
| 04 | Read Input Registers | Read N×16-bit from IR bank |
| 05 | Write Single Coil | Write 1 bit to coil bank |
| 06 | Write Single Register | Write 1×16-bit to HR bank |
| 15 | Write Multiple Coils | Write N bits to coil bank |
| 16 | Write Multiple Registers | Write N×16-bit to HR bank |

---

## 2. Modbus TCP Client on CircuitPython

**Yes, fully feasible.** The `adafruit_wiznet5k` library supports outbound TCP connections (`socket_connect`, `socket_write`, `socket_read`). Building a Modbus TCP client is even simpler than a server:

1. Open a TCP socket to the target device (e.g., Click PLC at 192.168.x.x:502)
2. Build an MBAP header + PDU for the desired read/write
3. Send the request, read the response
4. Parse the response PDU

This is ~100 lines of code. The Freeno83 library's PDU construction logic could be adapted for TCP framing (strip CRC, add MBAP header).

---

## 3. Modbus RTU on CircuitPython

### Hardware Layer: Working

The P1AM-SERIAL shield provides RS-485 via two configurable serial ports. Facts Engineering provides:

- **`CircuitPython_rs485_wrapper`** — wraps `busio.UART` with automatic DE/RE pin control for RS-485 half-duplex
- **`p1am_200_helpers.set_serial_mode(port, 485)`** — configures the P1AM-SERIAL hardware for RS-485 mode

The basic setup is:
```python
import board, busio
from rs485_wrapper import RS485
from p1am_200_helpers import set_serial_mode

port1_de = set_serial_mode(1, 485)
uart = busio.UART(board.TX1, board.RX1, baudrate=9600, receiver_buffer_size=512)
comm = RS485(uart, port1_de)
```

**Note on `busio.UART` RS-485 limitation:** CircuitPython's built-in `rs485_dir` parameter on `busio.UART` is explicitly **not supported on SAMD** (per the docs: "RS485 is not supported on SAMD, Nordic, Broadcom, Spresense, or STM"). This is why the external `rs485_wrapper` library exists — it handles DE/RE pin toggling in software, which is adequate for Modbus RTU at typical baud rates (9600–115200).

### RTU Protocol Libraries

**Freeno83/Circuit-Python-Modbus:**
- RTU Client/Master: **Working.** Supports FC1–6, 15, 16 over RS-485 using `busio.UART`
- RTU Server/Slave: **Not implemented**

**TwinDimension fork:**
- Has RTU server logic in the `umodbus` package, but requires porting from MicroPython APIs to CircuitPython (`machine.UART` → `busio.UART`, etc.)

**Building RTU server from scratch:**
- Modbus RTU adds framing (3.5 character silent intervals) and CRC-16 to the same PDU structure as TCP
- The CRC-16 algorithm is well-defined and can be implemented in ~20 lines
- Silent interval detection is the tricky part on CircuitPython (no hardware timers/interrupts), but can be approximated with `time.monotonic()` and short read timeouts
- Total implementation: ~200–250 lines for a basic RTU server

---

## 4. Arduino Comparison

On the Arduino side, this is a solved problem with mature libraries:

| Capability | Arduino | CircuitPython |
|------------|---------|---------------|
| Modbus TCP Server | ArduinoModbus + Ethernet | Manual (~250 lines) |
| Modbus TCP Client | ArduinoModbus + Ethernet | Manual (~100 lines) |
| Modbus RTU Master | ArduinoModbus + ArduinoRS485 + P1AM_Serial | Freeno83 lib (working) |
| Modbus RTU Slave | ArduinoModbus + ArduinoRS485 + P1AM_Serial | Manual (~200 lines) |
| Working P1AM examples | AutomationDirect GitHub repo | None exist |
| Community support | Extensive (ACC Automation blog, AD forums) | Minimal |

**Key gap:** AutomationDirect provides ready-to-run Arduino examples for P1AM ↔ Click PLC Modbus TCP communication (both client and server). No equivalent exists for CircuitPython. The Arduino ArduinoModbus library handles all the protocol details; on CircuitPython, you're building the protocol layer yourself.

**However:** The CircuitPython side has one significant advantage for your use case — the P1AM-200's `CircuitPython_P1AM` library provides a clean, Pythonic interface to the P1000 I/O modules (`base[slot][channel].value`), which maps naturally to your pyrung ladder logic model. Arduino requires more boilerplate for the same I/O access.

---

## 5. Memory and Performance Analysis

### Memory Budget

| Component | Estimated RAM |
|-----------|--------------|
| CircuitPython runtime | ~80–100 KB |
| P1AM base controller library | ~5–10 KB |
| WIZnet5k driver + socket pool | ~10–15 KB |
| Modbus register banks (Click-compatible) | ~4–8 KB |
| Modbus TCP server code | ~5–10 KB |
| Scan loop + ladder logic | ~10–30 KB |
| **Total** | **~120–175 KB** |
| **Available (256 KB)** | **~80–130 KB headroom** |

This is tight but workable. The W5500's hardware TCP/IP stack is a major advantage — it handles TCP reassembly, checksums, and ARP in silicon, so CircuitPython only processes complete Modbus PDUs.

### Scan Loop Timing

Target: 10–50ms scan cycle.

**Achievable with careful structure.** The critical insight is that checking for a Modbus TCP request can be done **non-blocking** with a zero timeout on the server socket:

```python
# Pseudocode for main scan loop
server_socket.settimeout(0)  # Non-blocking

while True:
    scan_start = time.monotonic_ns()
    
    # === SCAN: Read inputs ===
    inputs = read_p1000_inputs()
    
    # === SCAN: Execute ladder logic ===
    execute_rungs(inputs, registers)
    
    # === SCAN: Write outputs ===
    write_p1000_outputs(registers)
    
    # === COMMS: Check for Modbus request (non-blocking) ===
    try:
        client, addr = server_socket.accept()  # Returns immediately if no connection
        handle_modbus_request(client, registers)
        client.close()
    except OSError:
        pass  # No pending connection — continue scan
    
    scan_time = (time.monotonic_ns() - scan_start) / 1_000_000
```

**Timing estimates (SAMD51 @ 120MHz, CircuitPython):**

| Operation | Estimated Time |
|-----------|---------------|
| P1000 I/O read (SPI) | 1–3 ms |
| Ladder logic (50 rungs) | 0.5–2 ms |
| P1000 I/O write (SPI) | 1–3 ms |
| Non-blocking socket check (no request) | < 0.1 ms |
| Handle Modbus request (when present) | 1–3 ms |
| **Typical scan (no Modbus)** | **~3–8 ms** |
| **Scan with Modbus request** | **~5–12 ms** |

The 10–50ms target is realistic. CircuitPython on SAMD51 is roughly 10–30x slower than compiled C, but the P1000 SPI I/O and W5500 hardware offload dominate the timing, not Python interpretation.

### Interrupts and Polling

CircuitPython does **not** support interrupts on the P1AM-200 (confirmed in the P1AM-200 documentation: "Interrupt functionality not available when using CircuitPython"). This means:

- **Polling is the only option** — check for Modbus requests once per scan cycle
- This is actually **fine** for Modbus TCP. Modbus is inherently request/response, and the W5500 buffers incoming TCP data in its hardware FIFO. As long as you check within the Modbus timeout (typically 1–5 seconds), no data is lost
- For a 10–50ms scan cycle, you're checking 20–100 times per second, which is more than sufficient for any HMI/SCADA polling interval

### Persistent Connections

For better performance with HMI/SCADA systems that maintain persistent TCP connections (common with Modbus TCP), you'd keep the client socket open across scan cycles rather than accept/close each time:

```python
client_socket = None

while True:
    # ... scan logic ...
    
    # Accept new connection if none active
    if client_socket is None:
        try:
            client_socket, addr = server_socket.accept()
            client_socket.settimeout(0)
        except OSError:
            pass
    
    # Check existing connection for data
    if client_socket is not None:
        try:
            data = client_socket.recv(260)  # Max Modbus TCP frame
            if data:
                response = process_modbus(data, registers)
                client_socket.send(response)
        except OSError:
            client_socket.close()
            client_socket = None
```

The W5500 supports 8 hardware sockets, so you could serve 7 simultaneous Modbus clients (1 socket for listening) if needed.

---

## 6. Practical Recommendation

### Recommended Path: Write a Minimal Modbus TCP Server

For your pyrung code generator, the best approach is a **purpose-built, minimal Modbus TCP server module** (~250–350 lines total) that:

1. Uses `adafruit_wiznet5k` + `adafruit_wiznet5k_socketpool` for networking
2. Implements only FC1, 2, 3, 4, 5, 6, 15, 16 (the function codes Click PLCs use)
3. Uses flat `bytearray` register banks that map to Click's memory layout
4. Provides a non-blocking `poll()` method that integrates into the scan loop
5. Is a single `.py` file that drops into the CircuitPython `lib/` folder

### Why Not Port micropython-modbus?

The `brainelectronics/micropython-modbus` library (and its TwinDimension fork) is designed for a different ecosystem:
- Depends on `machine`, `network`, `uos` — none exist in CircuitPython
- Uses MicroPython's native socket module — incompatible with `adafruit_wiznet5k`
- Complex JSON-based register definition system — overkill for your use case
- Would require porting the transport layer, the register system, and testing every code path
- The actual Modbus protocol parsing is only ~200 lines of the 2000+ line codebase

It's faster and more reliable to write the ~250 lines you need than to port and debug 2000+ lines of someone else's code.

### Register Bank Design (Click PLC Compatible)

Based on Click PLC Modbus addressing (from pyclickplc's BANKS):

```python
# Click PLC Modbus memory map
# Coils/DI:      X/Y addresses → Modbus 0x0000–0x1FFF (bits)
# Input Regs:    XD addresses  → Modbus 0x0000–0x1FFF (16-bit)
# Holding Regs:  DS addresses  → Modbus 0x0000–0x1FFF (16-bit)

class ModbusRegisters:
    def __init__(self):
        self.coils = bytearray(256)       # 2048 coils (bit-packed)
        self.discrete_in = bytearray(256) # 2048 discrete inputs
        self.input_regs = bytearray(4096) # 2048 input registers (16-bit each)
        self.holding_regs = bytearray(4096) # 2048 holding registers
```

This uses ~8.75 KB of RAM — well within budget.

### Architecture for pyrung Integration

```
┌─────────────────────────────────────────┐
│              code.py (generated)         │
│                                         │
│  ┌──────────┐  ┌──────────┐  ┌────────┐│
│  │ P1AM I/O │  │  Ladder  │  │ Modbus ││
│  │  Driver   │  │  Logic   │  │ Server ││
│  └────┬─────┘  └────┬─────┘  └───┬────┘│
│       │              │             │     │
│       ▼              ▼             ▼     │
│  ┌──────────────────────────────────────┐│
│  │         Register Bank (shared)       ││
│  │  coils | discrete_in | holding_regs  ││
│  └──────────────────────────────────────┘│
│                                         │
│  while True:                            │
│      read_inputs() → register_bank      │
│      execute_rungs(register_bank)       │
│      write_outputs(register_bank)       │
│      modbus_server.poll(register_bank)  │
│                                         │
└─────────────────────────────────────────┘
```

The generated `code.py` from pyrung would:
1. Import the Modbus server module
2. Initialize P1AM I/O, Ethernet, and the Modbus server
3. Run the scan loop with `modbus_server.poll()` called once per cycle
4. The register bank is shared between ladder logic and the Modbus server — when an HMI writes to a holding register via Modbus, the ladder logic sees it on the next scan; when the ladder logic updates a register, the HMI reads the new value on its next poll

### Estimated Development Effort

| Component | Lines | Effort |
|-----------|-------|--------|
| Modbus TCP server (`modbus_tcp.py`) | 250–350 | 2–3 days |
| Register bank with Click mapping | 50–100 | 0.5 day |
| Ethernet init + DHCP helper | 30–50 | 0.5 day |
| Integration into pyrung generator | varies | 1–2 days |
| Testing with actual Click PLC | — | 1–2 days |
| **Total** | **~400–500** | **~5–8 days** |

### RTU Support (Optional/Later)

If you also need Modbus RTU (for serial devices), add a `modbus_rtu.py` module (~200 lines) that:
- Uses `rs485_wrapper` + `busio.UART` for the physical layer
- Adds CRC-16 and 3.5-character framing on top of the same PDU dispatch logic
- Shares the same register bank

The PDU parsing is identical between TCP and RTU — only the transport framing differs.

---

## Key Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Socket timeout bugs in wiznet5k | Use `settimeout(0)` (non-blocking) with explicit `try/except`; avoid blocking reads entirely |
| Memory pressure under load | Pre-allocate all buffers at startup; avoid dynamic allocation in the scan loop |
| CircuitPython garbage collection pauses | Call `gc.collect()` at a known point in the scan cycle (e.g., after Modbus poll) |
| W5500 socket exhaustion | Limit to 1–2 Modbus clients; close stale connections aggressively |
| Scan time variability | Monitor scan time with `time.monotonic_ns()`; log max scan time for diagnostics |
| RS-485 DE/RE timing | The `rs485_wrapper` software toggle is adequate at ≤115200 baud on SAMD51 @ 120MHz |

---

## References

- P1AM-200 Documentation: https://facts-engineering.github.io/modules/P1AM-200/P1AM-200.html
- P1AM-SERIAL Documentation: https://facts-engineering.github.io/modules/P1AM-SERIAL/P1AM-SERIAL.html
- CircuitPython_rs485_wrapper: https://github.com/facts-engineering/CircuitPython_rs485_wrapper
- CircuitPython_P1AM: https://github.com/facts-engineering/CircuitPython_P1AM
- Adafruit WIZnet5k Library: https://docs.circuitpython.org/projects/wiznet5k/en/stable/api.html
- TwinDimension Modbus Fork: https://github.com/TwinDimensionIOT/TwinDimension-CircuitPython-Modbus
- Freeno83 CircuitPython Modbus: https://github.com/Freeno83/Circuit-Python-Modbus
- AutomationDirect P1AM Arduino Examples: https://github.com/AutomationDirect/P1AM-Examples
- Modbus TCP Specification: MBAP header (7 bytes) + PDU, port 502
- CircuitPython busio.UART RS-485 limitation: "RS485 is not supported on SAMD" (requires external wrapper)
