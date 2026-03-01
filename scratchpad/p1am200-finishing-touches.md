## Immediate: P1AM-200 Deployment
1. **Get basic deployment working** — flash generated CircuitPython code to the P1AM-200 and verify the scan loop runs
2. **Fix bugs found during deployment** — you already published to PyPI, so patch as you go

## Onboard Peripherals & Getting Started Examples
3. **Add `board` module to `pyrung.circuitpy.p1am`** — `from pyrung.circuitpy.p1am import board`
   - `board.switch` → InputTag (bool), maps to `board.SWITCH` via digitalio
   - `board.led` → OutputTag (bool), maps to `board.LED` via digitalio
   - `board.neopixel.r`, `.g`, `.b` → OutputTag (int 0-255), codegen emits `pixel[0] = (r, g, b)`
   - These are separate from the slot-based P1AM module system

4. **Example 1 — Start/stop motor latch (hello world):** Toggle switch + yellow LED, zero wiring, with a side-by-side showing pyrung code vs generated CircuitPython

5. **Example 2 — Traffic light on the neopixel:** Adapt existing quickstart traffic light, use `copy` for RGB values (three lines per color state), demonstrates timers + state transitions

6. **Add side-by-side to getting started docs** — pyrung code on left, generated CircuitPython on right, using the motor latch example

## DAP Plugin
7. **Publish the VS Code DAP extension** to the marketplace

## Modbus on P1AM-200
8. **Implement minimal Modbus TCP server** (~250-350 lines) embedded in codegen output
   - Non-blocking socket check once per scan via `adafruit_wiznet5k`
   - Click-compatible register mapping via TagMap
   - FC1, FC2, FC3, FC4, FC5, FC6, FC15, FC16
   - New parameters on `generate_circuitpy()`: `modbus_server`, `modbus_ip`, `tag_map`, etc.
   - You have a full research document and implementation prompt ready for this

9. **Modbus TCP client (send/receive)** — same PDU framing as server, reversed direction. Consider asyncio for non-blocking request/response across scans. Prompt needs a client section added.

10. **Use Click's slot-based address convention** for P1AM-200 I/O mapping — same TagMap, same Modbus layout, so a P1AM-200 is a drop-in replacement for a Click on the network. Restrict the hardware mapping to match Click conventions even though the P1AM-200 could be more flexible.

## Content & Marketing (after hardware works)
11. **PackML/TR88 example** — ship as an example in the pyrung repo, not a separate package. Generalize your existing Click implementation for others to learn from.

12. **Blog post / video** of the P1AM-200 deployment — the "Python to real hardware" story. Ideally showing terminal + board with LEDs.

13. **Post to r/PLC, AutomationDirect forums, etc.** — get visibility for both pyrung and pyclickplc

## Not Doing
- ❌ Structured Text codegen for CODESYS — wrong market, dilutes the pitch
- ❌ Neopixel color presets — nice-to-have later, plain `copy` is fine for now
- ❌ Multiple Modbus client connections — single connection for v1
- ❌ DHCP — static IP only for v1
- ❌ Modbus RTU server — future work, shares PDU dispatch with TCP