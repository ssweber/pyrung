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

### Send

```python
with Rung(Enable):
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
with Rung(Enable):
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

Both instructions require four status tags:

- **sending** / **receiving** (`BOOL`) — True while the transaction is in progress
- **success** (`BOOL`) — True for one scan after a successful transaction
- **error** (`BOOL`) — True for one scan after a failed transaction
- **exception_response** (`INT`) — Modbus exception code on error

### Remote addressing

`remote_start` can be a Click address string (e.g. `"DS1"`) for Click-to-Click communication, or a `ModbusAddress` for raw Modbus devices.

For more on the soft-PLC Modbus setup, see [Click PLC — ClickDataProvider](../dialects/click.md).
