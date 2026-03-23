# CircuitPython Modbus TCP

`pyrung.circuitpy` can generate a Modbus TCP server, client, or both for the P1AM-200 via the P1AM-ETH shield. The register layout matches a real Click PLC — C-more HMIs, pyclickplc, and SCADA systems connect without translation. See [CircuitPython Dialect](circuitpy.md) for the base hardware model and code generation.

## Hardware requirements

The [P1AM-ETH](https://facts-engineering.github.io/modules/P1AM-ETH/P1AM-ETH.html) shield provides a W5500 Ethernet controller on SPI with chip-select on `board.D5`. Static IPv4 only — no DHCP. The `adafruit_wiznet5k` library must be installed on the CIRCUITPY drive alongside the CircuitPython P1AM library.

## Server

```python
from pyrung import Bool, Int, Program, Rung, out
from pyrung.circuitpy import ModbusServerConfig, P1AM, generate_circuitpy
from pyrung.click import TagMap, c, ds

# Hardware
hw = P1AM()
inputs  = hw.slot(1, "P1-08SIM")
outputs = hw.slot(2, "P1-08TRS")

Button   = inputs[1]
Light    = outputs[1]
Setpoint = Int("Setpoint")

# Logic
with Program() as logic:
    with Rung(Button):
        out(Light)

# Map to Click addresses for Modbus visibility
mapping = TagMap({
    Setpoint: ds[1],
    Light:    c[1],
})

# Generate with Modbus server
source = generate_circuitpy(
    logic, hw,
    target_scan_ms=10.0,
    watchdog_ms=500,
    modbus_server=ModbusServerConfig(ip="192.168.1.200"),
    tag_map=mapping,
)
```

The generated code starts a Modbus TCP listener on the configured IP and port. Any Modbus client reading DS1 gets the current value of `Setpoint`; writing DS1 updates it. Reading coil C1 returns the state of `Light`. The register layout is identical to a real Click PLC — same Modbus addresses, same data encoding.

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `ip` | `str` | — | Static IPv4 for the P1AM-ETH shield |
| `subnet` | `str` | `"255.255.255.0"` | |
| `gateway` | `str` | `"192.168.1.1"` | |
| `dns` | `str` | `"0.0.0.0"` | |
| `port` | `int` | `502` | 1–65535 |
| `max_clients` | `int` | `2` | 1–7 concurrent connections (W5500 has 8 sockets, 1 reserved for listener) |

Supported function codes: FC 1 (read coils), FC 2 (read discrete inputs), FC 3 (read holding registers), FC 4 (read input registers), FC 5 (write single coil), FC 6 (write single register), FC 15 (write multiple coils), FC 16 (write multiple registers).

## Client — send and receive

```python
from pyrung import Bool, Int, Block, Program, Rung, TagType
from pyrung.circuitpy import ModbusClientConfig, P1AM, generate_circuitpy
from pyrung.click import ModbusTcpTarget, TagMap, send, receive

hw = P1AM()
hw.slot(1, "P1-08SIM")

Enable        = Bool("Enable")
LocalSetpoint = Int("LocalSetpoint")
RemoteWords   = Block("RemoteWords", TagType.INT, 1, 4)

CommSending   = Bool("CommSending")
CommReceiving = Bool("CommReceiving")
CommSuccess   = Bool("CommSuccess")
CommError     = Bool("CommError")
CommEx        = Int("CommEx")

with Program() as logic:
    with Rung(Enable):
        send(
            target="plc1",
            remote_start="DS1",
            source=LocalSetpoint,
            sending=CommSending,
            success=CommSuccess,
            error=CommError,
            exception_response=CommEx,
        )

    with Rung(Enable):
        receive(
            target="plc1",
            remote_start="DS100",
            dest=RemoteWords.select(1, 4),
            receiving=CommReceiving,
            success=CommSuccess,
            error=CommError,
            exception_response=CommEx,
        )

source = generate_circuitpy(
    logic, hw,
    target_scan_ms=10.0,
    modbus_client=ModbusClientConfig(
        targets=(ModbusTcpTarget(name="plc1", ip="192.168.1.20"),)
    ),
    tag_map=TagMap(),
)
```

`send` writes local tag values to a remote Click address. `receive` reads remote Click addresses into local tags. The `target` string must match a `ModbusTcpTarget.name`. Remote addresses use Click address format (`DS1`, `C1`, `X001`, etc.).

### Raw Modbus addresses

When the remote device isn't a Click PLC, use `ModbusAddress` instead of a Click address string. This gives direct control over the register address, register type, and word order.

```python
from pyrung.core.instruction.send_receive import ModbusAddress, RegisterType, WordOrder

vfd = ModbusTcpTarget(name="vfd", ip="192.168.1.30")

with Program() as logic:
    # Read a 32-bit speed value from holding registers 0x200–0x201, high word first
    with Rung(Enable):
        receive(
            target="vfd",
            remote_start=ModbusAddress(0x200, RegisterType.HOLDING, WordOrder.HIGH_LOW),
            dest=Speed,
            receiving=CommReceiving,
            success=CommSuccess,
            error=CommError,
            exception_response=CommEx,
        )

    # Write a setpoint to a single holding register at 0x100
    with Rung(Enable):
        send(
            target="vfd",
            remote_start=ModbusAddress(0x100),
            source=Setpoint,
            sending=CommSending,
            success=CommSuccess,
            error=CommError,
            exception_response=CommEx,
        )
```

`ModbusAddress` fields:

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `address` | `int` | — | Modbus register/coil address (0–0xFFFE) |
| `register_type` | `RegisterType` | `HOLDING` | `HOLDING`, `INPUT`, `COIL`, or `DISCRETE_INPUT` |
| `word_order` | `WordOrder` | `HIGH_LOW` | Word ordering for 32-bit types (DINT, REAL) |

The codegen maps `register_type` to the correct Modbus function code:

| Type | Send | Receive |
|------|------|---------|
| `HOLDING` | FC 6 (single) / FC 16 (multiple) | FC 3 |
| `INPUT` | — | FC 4 |
| `COIL` | FC 5 (single) / FC 15 (multiple) | FC 1 |
| `DISCRETE_INPUT` | — | FC 2 |

`word_order` controls how DINT and REAL values are split across register pairs. `HIGH_LOW` puts the high word at the lower address (big-endian, common in VFDs and power meters). `LOW_HIGH` matches Click PLC native ordering.

RTU (serial) targets are not yet supported for CircuitPython code generation.

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `name` | `str` | — | Unique identifier, referenced by `target=` in send/receive |
| `ip` | `str` | — | Remote PLC address |
| `port` | `int` | `502` | |
| `device_id` | `int` | `1` | Modbus unit ID (0–255) |
| `timeout_ms` | `int` | `1000` | Per-transaction timeout |

Unlike the Click dialect's threaded `send`/`receive`, the CircuitPython versions generate a non-blocking state machine. Each transaction advances one step per scan (connect → send request → wait for response → apply result). The scan loop is never blocked. Status tags (`sending`/`receiving`, `success`, `error`, `exception_response`) update as the transaction progresses. When the rung condition goes false, status tags reset to defaults.

## TagMap and mapped_tag_scope

`tag_map` is required when `modbus_server` or `modbus_client` is set. It determines which tags are visible over Modbus — the TagMap maps semantic tags to Click hardware addresses, and the codegen uses those addresses as Modbus register addresses.

`mapped_tag_scope` controls how many TagMap entries get backing variables in the generated code:

| Value | Behavior |
|-------|----------|
| `"referenced_only"` (default) | Tags used in logic and tags with non-default initial values |
| `"all_mapped"` | Every entry in the TagMap gets a backing variable |

The default avoids allocating RAM for tags that no rung references and start with type-default values. Use `"all_mapped"` when an HMI or SCADA system needs to write values via Modbus even though no ladder rung touches them.

## Scan cycle with Modbus

1. Read physical inputs
2. Execute rungs
3. Write physical outputs
4. Service Modbus server
5. Service Modbus client
6. Edge snapshots, watchdog pet, scan sleep

The server and client service calls run unconditionally — including in STOP mode. This matches Click behavior: an HMI can still read tag state and see `sys.mode_run` as `False` while the PLC is stopped.

## Both server and client

The P1AM-200 can be both server and client simultaneously. A single Ethernet setup is shared.

```python
source = generate_circuitpy(
    logic, hw,
    target_scan_ms=10.0,
    modbus_server=ModbusServerConfig(ip="192.168.1.200"),
    modbus_client=ModbusClientConfig(
        targets=(ModbusTcpTarget(name="plc1", ip="192.168.1.20"),)
    ),
    tag_map=mapping,
)
```
