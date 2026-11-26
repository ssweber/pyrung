# Communication

For an introduction to the DSL vocabulary, see [Core Concepts](../getting-started/concepts.md).

## Modbus send / receive

`send` writes local values to a remote device. `receive` reads remote values into local tags.

### Target

Define the remote device as a `ModbusTcpTarget` or `ModbusRtuTarget`:

```python
peer = ModbusTcpTarget(name="peer", ip="192.168.1.10", port=502)
rtu_device = ModbusRtuTarget(name="sensor", serial_port="/dev/ttyUSB0", device_id=2)
```

For codegen-only programs (no live simulation), pass a plain string name instead.

A CLICK target can also name its port and take its device ID from a register:

```python
peer = ModbusTcpTarget(name="peer", ip="192.168.1.10", com_port="wlan")
DriveId = Int("DriveId")  # map it to a DS register
drive = ModbusRtuTarget(name="drive", com_port="cpu3", device_id=DriveId)
```

`com_port="wlan"` sends over the CLICK's WLAN port. A TCP target with no `com_port` uses Ethernet. An RTU target takes `"cpu2"` (the default), `"cpu3"`, `"slot0_1"`, `"slot0_2"`, `"slot1_1"` or `"slot1_2"`.

`device_id` is a number or a DS tag. The instruction reads the tag as each request starts, so the program can change which device it talks to. CLICK accepts only a DS register there. pyrung rejects any tag except an `Int` when you build the target. The CLICK export rejects an `Int` that is mapped outside DS.

A TCP device ID runs 0..255 and an RTU one 0..247. A `receive()` from an RTU device rejects device ID 0, the broadcast address.

### Send

```python
with rung(Enable):
    send(
        target=peer,
        remote_start="DS1",
        source=DS.select(1, 10),
        sending=Sending,
        success=SendOK,
        error=SendErr,
        exception_response=ExCode,
    )
```

### Receive

```python
with rung(Enable):
    receive(
        target=peer,
        remote_start="DS1",
        dest=DS.select(11, 20),
        receiving=Receiving,
        success=RecvOK,
        error=RecvErr,
        exception_response=ExCode,
    )
```

### Status tags

Both instructions accept four status tags. Each is optional, as in CLICK. If you leave one off, the instruction uses a hidden tag, and the export writes that status as off:

- **sending** / **receiving** (`BOOL`) — True while the transaction is in progress
- **success** (`BOOL`) — Latches True on success. The next request clears it.
- **error** (`BOOL`) — Latches True on failure. The next request clears it.
- **exception_response** (`INT`) — Modbus exception code on error. The next request clears it.

The bank of the local tag must suit the bank of the remote start. Validation and the CLICK export refuse any other pair, such as `source=x[5]` with `remote_start="DS1"`.

| `send()` source | `remote_start` bank |
|---|---|
| X, Y, C, T, CT, SC | C or Y |
| DS, TD, SD | DS or TD |
| DD, CTD | DD or CTD |
| DH, XD, YD | DH or YD |
| DF | DF |
| TXT | TXT |

| `receive()` destination | `remote_start` bank |
|---|---|
| Y, C | X, Y, C, T, CT or SC |
| DS | DS, TD or SD |
| DD | DD or CTD |
| DF | DF |
| DH | DH, or XD or YD for one register |
| YD | DH, XD or YD, for one register |
| TXT | TXT |

Both tables come from CLICK's editor.

Once a request is submitted, it runs to completion even if `Enable` drops. `sending` / `receiving` stays True until the response (or timeout) is processed. `success` and `error` then latch and persist across disabled scans. They are only cleared when the next request is submitted (the rising-edge of Enable, or any scan where Enable is still high after a previous request finished).

Because the flags latch, checking `success` or `error` directly in downstream logic will fire every scan while the value is stuck True. Use `rise(success)` / `rise(error)` to get a one-scan pulse on the completion edge:

```python
with rung(rise(SendOK)):
    out(SendComplete)  # fires for exactly one scan on each success
```

This also covers a hardware timing case. On a real CLICK, if the TCP connection is busy with another Send/Receive, `sending` / `receiving` may not rise immediately. During that delay, the previous cycle's `success` is still latched. `rise()` avoids triggering on the stale value.

### how()

`how()` sends no Modbus request. It may set these tags the way it sets an input:

- a `receive()` destination
- the success or error bit of a `send()` or `receive()`

A plan step that sets one of these tags stands for the device's answer. The busy bit and the exception code keep their values. The forks that `how()` returns do not talk to the device either.

### Remote addressing

`remote_start` can be a CLICK address string (e.g. `"DS1"`) for CLICK-to-CLICK communication, or a `ModbusAddress` for raw Modbus devices.

```python
receive(
    target=peer,
    remote_start=ModbusAddress(0x100, RegisterType.HOLDING),
    dest=Txt.select(1, 3),
)
```

From a raw holding or input register, `receive()` reads two characters into TXT tags. `send()` of TXT tags to a raw holding register writes two characters into each register. The low byte holds the first character. Both refuse a coil or a discrete input with TXT tags. Both need an even number of TXT tags, as CLICK does.

For more on the soft-PLC Modbus setup, see [CLICK PLC — ClickDataProvider](../dialects/click.md).
