# Send/Receive ASCII — Design Notes

## What it is

Click ASCII Send/Receive is a **free-form serial text protocol** — fundamentally different from the structured Modbus send/receive that already exists. Key features:

- **Send**: Compose messages from static text, embedded ASCII codes (`$0D$0A` for CR/LF), and embedded memory addresses (DS, DD, DH, DF, XD, YD, TD, CTD, SD with formatting). Or send dynamic messages from TXT registers. Max 128 chars.
- **Receive**: Accept fixed or variable-length ASCII into TXT memory, with first-character and inter-character timeouts. Overflow flag at 128 chars.
- **Status flags**: Sending/Receiving, Success, and error flags (timeout, overflow) — same pattern as Modbus.

## Pros

1. **Completes Click serial coverage.** Modbus is done; ASCII is the other half of Click's COM port story. Many real Click programs use both.
2. **Real industrial use cases.** Barcode scanners, serial displays, weighing scales, GPS modules, label printers — tons of field devices speak ASCII over serial. This is arguably more common in small Click applications than Modbus device-to-device.
3. **Status tag pattern already exists.** The `sending`/`success`/`error` model from Modbus maps directly — extend it with ASCII-specific flags (first char timeout, interval timeout, overflow).
4. **Codegen value.** Generating the Click `.clk` representation of ASCII send/receive is useful for the roundtrip story.
5. **Testable in simulation.** A soft ASCII channel (in-memory string buffer or localhost socket) is actually simpler to mock than Modbus — it's just bytes, no protocol framing.

## Cons / Challenges

1. **The static message is a mini template language.** `"Temp: "` + `{DF1:6.2f}` + `$0D$0A` — need to parse and evaluate embedded memory addresses with Click-specific format specifiers. This is the hardest part and the biggest divergence from Modbus's clean "source block -> remote register" model.
2. **Deeply Click-specific.** The embedded address formatting, 128-char limit, byte swap modes, COM port numbering — none of this generalizes to a core instruction. This would be Click-dialect only, unlike `send()`/`receive()` which work with raw Modbus too.
3. **Timeouts are real-time serial concerns.** First-character timeout and inter-character timeout don't map cleanly to FIXED_STEP. Options: (a) model as scan-count heuristics, (b) only meaningful in REALTIME mode, or (c) simulation channel delivers complete messages and skips timeout modeling.
4. **Two send modes (static vs dynamic) multiply the surface area.** Static = template string with embedded codes/addresses. Dynamic = read from TXT block range. Both need different validation, different codegen, different test coverage.
5. **Serial port simulation.** Unlike Modbus TCP where two pyrung instances talk over localhost sockets, ASCII serial needs either virtual serial ports (platform-specific), a socket adapter, or a pure in-memory channel abstraction.

## Recommended Phasing

### Phase 1 — Dynamic-only send + receive with in-memory channel

- `send_ascii(target=..., source=txt[1:10], ...)` — send from TXT block range (dynamic mode)
- `receive_ascii(target=..., dest=txt[1:10], ...)` — receive into TXT block range
- Skip static template messages initially — they're the complex part and dynamic covers the common automation case
- Channel abstraction: `AsciiSerialTarget` with an in-memory backend for testing (same pattern as `ModbusTcpTarget`)
- Status flags: sending/receiving, success, error (collapse the three receive error types into one `error` flag for now)
- Codegen for Click

### Phase 2 (if needed) — Static template messages

- Template parser for embedded ASCII codes and memory addresses
- Format specifiers for numeric types
- Simulation evaluator

### Phase 3 (if needed) — Full timeout modeling

- First-character and inter-character timeout semantics in REALTIME mode
- Overflow detection

## Rationale

The dynamic TXT-based path gives 80% of the real-world value with 20% of the complexity. Most Click programs that use ASCII send/receive build the message in TXT registers with `copy()` anyway — the static template is a convenience feature in the Click IDE.
