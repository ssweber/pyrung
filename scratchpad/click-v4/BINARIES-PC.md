# CLICK v4.00 PC binaries: what the compiler reveals (feasibility stake)

Read-only static survey of `C:\Program Files (x86)\AutomationDirect\CLICK Ver4.00` (diffed against Ver3.92).
Copies, scripts and dumps are in the session scratchpad (`...\scratchpad\click-pc\`: `v4/`, `v392/`, `tools/`,
`v4.compile.dis.txt`, `v4.optable.txt`, `v4.codetable.txt`, `v4.vecmap`). Nothing was run. Throughout,
**[E]** marks direct evidence and **[I]** marks inference.

## Answers to the four questions

### 1. Where does compiled output land? Is there a callable export?

- **On disk, as files, at least for the duration of a compile.** [E] `CLICK.exe` names the outputs
  `Mnemonic.MN`, `Mnemonic.bin`, `Mnemonic.tbl`, `Mnemonic.txt`, `Mnemonic.bin.IAddrList.txt`,
  `MnemonicInfo.tmp`, `scp.bin` / `scp.bin.mn` / `scp.bin.pa`, `S%03dR%05d.dat.reform` and `BIN.MN`. It also logs
  "======== Compile: Rung No.%d ========" (CLICK.exe UTF-16 strings at 0x2581f0..0x258a20 and 0x263d80..0x264060).
  Compile.dll opens `<path>.MN` with `CFile::Open(..., modeCreate|modeWrite)` at 0x100105f2 and writes `.tbl`, `.txt`
  and `.reform` (0x10008805..0x10008cf9, format `"%02X,%02X=%04X:%04X:%02X"`).
  The live temp project `%TEMP%\CLICK (xxxx)\` has an empty `Compile\` folder, so [I] the files are written there
  during a compile, Syntax Check or Write Project, and then deleted. I haven't confirmed this.
- `Mnemonic.MN` is [I] a text listing in Koyo DirectLOGIC-style IL (LD/AND/OR/OUT/TMR/CNT/DRUM/FOR/NEXT...). It uses
  internal address letters (`R`, `K`, `I`, `Q`, `M`, `T`, `C`, `S`, `P`, `O`, `$`), which the operand parsers at
  0x1000f370 and 0x1000fbe0 check for. `Mnemonic.bin` is the assembled form: 3-byte cells with an odd-parity bit
  (writer 0x10010660; "Program Size (Total: 8,000 steps)" in PLCReadWrite.dll).
- **Exports** [E]: Compile.dll (MSVC6/MFC42, "Koyo Electronics", "Compile DLL 1.0.4.0") exports only `DoCompile`,
  `DoMakeMnemonic` and `_DoReverseEngineering@8`. They are not standalone. On init the DLL does
  `GetProcAddress(GetModuleHandle(NULL), "LADDER_Handler")` (0x10001253), which is a callback into CLICK.exe that
  supplies the ladder data. Calling them from a harness would mean faking that callback and its data structures,
  which is a reverse-engineering job in itself. **Practical route: capture the files rather than call the DLL.**
- A debug switch exists [E]. `GetPrivateProfileIntA("App", "Debug", 0, <exe dir>\SC_debug.ini)` (0x10001126) turns
  on a logger that writes `>>Start` / `>>End` and per-step messages (OutputDebugStringA is imported). It needs a
  file in Program Files, so it would be the user's call, and it is not needed for the file capture.

### 2. Is per-instruction lowering visible? (Out variants, Break/Continue)

**Yes, and the answer is that nothing is lowered.** Each v4 instruction is its own native mnemonic and opcode.
The PC side never expands one into timer primitives.

The v3.92 -> v4 diff of Compile.dll's `.data` strings is exactly these new mnemonics [E]:

| ladder | func code (laddercodec tag 0x3218) | mnemonic | opcode pair (table A, per CPU family) | operand format | instruction-library entry |
|---|---|---|---|---|---|
| Toggle | 8224 | `OTOG` | 0x0428 / 0x0160 | 8 (same as OUT) | 0x12036c |
| Toggle + immediate | 8225 | `OTOG.Y` | 0x0429 / 0x0161 | 8 | 0x120370 |
| Debounce ms / sec | 8226 / 8227 | `ODEB` / `ODEB.s` | 0x042a-b / 0x0162-3 | 0x1b | 0x120374 / 78 |
| Flasher ms / sec | 8228 / 8229 | `OFLA` / `OFLA.s` | 0x042c-d / 0x0164-5 | 0x1b | 0x12037c / 80 |
| Timed ms / sec / **m** | 8230-8232 | `OTIME` `.s` `.m` | 0x042e-30 / 0x0166-8 | 0x1c | 0x120384..8c |
| Timed, Output Maintained | 8233-8235 | `OTIMEOM` `.s` `.m` | 0x0431-3 / 0x0169-b | 0x1c | 0x120390..98 |
| Continue | 9232 | `CONTINUE` | table B | 0xf (as FOR/NEXT), **1 rung input** | 0x12029c |
| Break | 9233 | `BREAK` | table B | 0xf, **1 rung input** | 0x120290 |
| Drum, DS durations (Time base) | 10249.. | `DRUM.DS` `.DSS` `.DSM` `.DSH` `.DSD`, `JDRUM.DS*` | table B | block type 0xd | 0x120398..0x1203bc |
| Drum, Time & Event | 10505..10538 | `TEDRUM.DS*`, `TEJDRUM.DS*` (no plain `TEDRUM`) | table B | block type 0xe | 0x120498..0x1204bc |
| MAX MIN MDN AVG RAND ABS | (inside MATH 8451-8454) | formula tokens 0x1e..0x23 | n/a | n/a | n/a |

Sources: func code -> mnemonic table in DoMakeMnemonic (0x10046d94..; it agrees with every laddercodec code,
e.g. 8193 OUT, 8197 OUTDI, 9218 FOR, 9219 FORP, 8451 MATH, 9221 CAL). Mnemonic registration table A
(0x10036xxx-0x10037xxx: opcode slot, format, entry) and table B (0x10038e00-0x1003a1b4). Opcode pairs come from
loader 0x1001af30 (e.g. `push 0x162; push 0x42a` for ODEB). Formula tokens: 0x10005ce9..0x10005e99.

- **O6 (does the Out coil write T<n>?)** The coil is one opcode that calls one firmware instruction-library routine
  (entry 0x120374 for ODEB). The routine is in the encrypted firmware, so **O6 can't be answered from the PC side**.
  [I] The assembler for format 0x1b (0x100107c0) emits the opcode plus a single operand cell, parsed by the
  `K`/`O` constant parser 0x1000fbe0. That suggests the TDs and presets travel as a parameter block or extra
  mnemonic lines, not as timer instructions. Only a captured `Mnemonic.MN` would show whether a `T`-letter operand
  is passed.
- **Break/Continue** don't emit jumps on the PC side. They are firmware routines with format 0xf, the same as
  FOR/FORP/NEXT, and they need exactly one rung input (table B field 0x7c = 1). Elsewhere in table B that field
  counts power-rail legs: DRUM 2, JDRUM 3, SR 3. This confirms C000B/C000C ("needs a contact") from the compiler
  side. For and Next have 0 in that field.
- **Native code exists, but only on the old path.** [E] Compile.dll does emit raw H8S machine code for MATH
  formulas: `79 0r imm16` MOV.W, `7A 0r imm32` MOV.L, `01 00 6B Ar 00 FF 8A xx` MOV.L to a work area, and
  `5E 12 03 xx` JSR into a firmware math vector table (0x1003b5d0, 0x1003c650). That code generator handles tokens
  only up to 0x1d (POW). The new MAX/MIN/MDN/AVG/RAND/ABS tokens (0x1e..0x23) never reach it. [I] So the native H8
  path is the legacy C0-0x CPU (which lacks v4 features), and the v4-capable CPUs run interpreted opcodes. The
  rounding for M1/M2 lives in firmware either way.

### 3. Cost of a per-instruction spec (opcode + operand layout + lowering)

- **Opcode and func code for every v4 instruction: done** (table above, about 3 hours). It is directly useful to
  laddercodec. It predicts the 0x3218 func codes before any clipboard capture.
- **Operand layout:** about **a day** per family by capture: compile one-rung projects in CLICK and copy the
  `Compile\` files before they vanish, then read `Mnemonic.MN`. Static-only, it is **several days**. The
  per-format assemblers (switch at 0x1000fe61 over formats 0..0x20) and DoMakeMnemonic's per-func-code operand
  formatting (switch at 0x1004351d) can be read, but linear capstone is painful there. A real decompiler (Ghidra) is
  what turns that from days into hours. It is needed only if the capture route fails.
- **Lowering/semantics: not obtainable at any cost from the PC side.** There is no lowering. Toggle, Debounce,
  Flasher, Timed, T&E drum, Break/Continue and the new math functions each dispatch to a firmware routine. The
  firmware (`Click_fw0400.clk`) is a ZIP with AES-encrypted entries (`Click_fw0400.bin`,
  `clicketh_fwV0400.bin`, `clickplus_main/sub_fwV0400.bin`). Decrypting it would mean pulling a key out of
  CLICK.exe. I did not try, and I'd consider it out of bounds.

### 4. Verdict

**Not worth pursuing as a semantics oracle. Keep the real-PLC conformance tests as the only source of truth for
O1-O6, D1-D3, M1-M4 and F1-F16.** Reading Compile.dll can't verify "pyrung does what CLICK's compiled code says",
because the compiled code doesn't say anything: it is an opcode plus operands that hand off to encrypted firmware.

What's worth keeping, cheaply:
- The func-code table above, for the laddercodec v4 captures. Treat it as an answer key to check captures against.
- Optionally, one capture session of `Compile\` files (`Mnemonic.MN`). It confirms operand layouts and settles
  O7-style unit questions for about an hour, alongside the clipboard captures already planned.

---

## Other findings (for reference)

- **Timed has a third unit in the compiler.** [E] `OTIME.m` / `OTIMEOM.m` (func codes 8232/8235) exist next to
  `""` (ms) and `.s`. The help (Content/350.htm) and the dialog say "ms, sec" only. `OTIMEOM.m`'s library entry
  (0x120398) collides with `DRUM.DS`'s, so [I] `.m` is probably an unexposed or unfinished "minutes" variant.
  Debounce and Flasher have only ms/sec. **O7:** the UI offers ms/sec, the compiler has a hidden third Timed unit;
  pyrung should accept ms/sec only.
- **Drum duration registers** [E]: the Time base gets a separate register-duration family `DRUM.DS*`/`JDRUM.DS*`
  (block type 0xd vs 5 for constant presets), with one mnemonic per unit (ms/S/M/H/D). **Time & Event exists only
  in the `.DS` form** (block type 0xe). [I] Constant presets in a T&E step are carried in the same per-step block,
  which fits "a step may have no preset" (OBJ-1019). Relevant to D2: the operand class is "DS register", nothing
  else is visible.
- **Range order** [E]: the legacy native SUM code generator swaps low/high when low > high (0x1003b87e). v4 rejects
  unordered ranges at edit time anyway (OBJ-1014).
- **RAND parse quirk** [E]: the formula parser special-cases token 0x22 (RAND) together with token 0x1b (`:` range
  separator) at 0x10004f5e. [I] RAND takes `lo,hi` (comma token 0x24) and the parser keeps `:` from being read as a
  range there.
- **Toggle is format 8, the same as OUT/SET/RST**: one bit operand, plus an `.Y` immediate variant. There are no
  range (`BOUT`-style) or one-shot (`PLS`-style) Toggle variants, which matches the dialog.
- **SC_InstructionSet.ini is legacy** (identical to v3.92). The real tables are compiled into Compile.dll. SC_.ini
  changes only the toolbox label ("Next/Break/Continue": Break/Continue live in InstCtl_Next.dll) and adds
  `InstructionLib=200`. [I] That is the firmware instruction-library version, and the 0x12xxxx entries index it.
- **PLCType.ini (v4)** makes SC162, SC165, SC168 and SC171 writable (they were read-only inside SC122..302 in
  v3.92). Their purpose isn't documented in the help. Worth a look in the v4 system-control-relay list.
- **Out-family drawing/dialog DLLs** (InstCtl_Out, Inst_Out) only carry UI labels: "Toggle", "Debounce", "Flasher",
  "Timed", "On Time", "Off Time", "Unit", "Output Maintained". Inst_Out.dll's 1.4 MB `.rsrc` is mostly
  timing-chart bitmaps. They hold no semantics.

## What would need what

| Question | PC binaries | Decompiler on Compile.dll | Firmware | Real PLC |
|---|---|---|---|---|
| func codes / mnemonics / opcodes | **done** | — | — | — |
| operand layout per instruction | capture `Mnemonic.MN` (cheap) | speeds static route | — | — |
| O1-O6, D1, D3, M1-M4, F1-F16 semantics | no | no | yes (encrypted) | **yes** |
| O7 units | done (ms/sec in UI; hidden `.m` for Timed) | — | — | — |

## Recommended next step

During the v4 clipboard capture session, also save the contents of `%TEMP%\CLICK (xxxx)\Compile\` right after a
Syntax Check / compile of a project holding one of each new instruction (copy them while CLICK is still open). Then
check the func codes in laddercodec against the table above. Everything else goes to the conformance suite on a
real PLC.
