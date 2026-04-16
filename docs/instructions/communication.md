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
- **success** (`BOOL`) — Latches True on successful completion; cleared when the next request starts
- **error** (`BOOL`) — Latches True on failure; cleared when the next request starts
- **exception_response** (`INT`) — Modbus exception code on error; cleared when the next request starts

Once a request is submitted, it runs to completion even if `Enable` drops — `sending` / `receiving` stays True until the response (or timeout) is processed. `success` and `error` then latch and persist across disabled scans. They are only cleared when the next request is submitted (the rising-edge of Enable, or any scan where Enable is still high after a previous request finished).

Because the flags latch, checking `success` or `error` directly in downstream logic will fire every scan while the value is stuck True. Use `rise(success)` / `rise(error)` to get a one-scan pulse on the completion edge:

```python
with Rung(rise(SendOK)):
    out(SendComplete)  # fires for exactly one scan on each success
```

This also handles a subtle hardware timing case: on real Click CPUs, if the TCP connection is busy with another Send/Receive, `sending` / `receiving` may not rise immediately — and during that brief delay, the previous cycle's `success` is still latched. `rise()` avoids triggering on the stale value.

### Remote addressing

`remote_start` can be a Click address string (e.g. `"DS1"`) for Click-to-Click communication, or a `ModbusAddress` for raw Modbus devices.

For more on the soft-PLC Modbus setup, see [Click PLC — ClickDataProvider](../dialects/click.md).
