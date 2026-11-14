# CLICK v4.00 firmware image — feasibility survey

Date: 2026-10-22. Read-only survey. Nothing in Program Files was modified, nothing was extracted or
executed, and no attempt was made to recover a password or key.
Inputs: `C:\Program Files (x86)\AutomationDirect\CLICK Ver4.00\Firmware\Click_fw0400.clk` (297,598,166 B)
and, for comparison, `CLICK Ver3.92\Firmware\Click_fw0392.clk` (297,567,088 B). A `Ver3.71` install with
`Click_fw0371.clk` also exists.

**Result: the firmware is AES-256 encrypted at the container layer. Every probe below stays open.
The firmware route ends here unless someone decides to recover the archive password (see (d)).**

---

## (a) Container, images, architecture, encryption

### Container

A standard ZIP that uses WinZip AES encryption on every entry.

- Offset 0: `50 4B 03 04` (local file header), version-needed `0x33` (5.1, the AES level),
  general-purpose flag `0x0001` (encrypted), method `0x63` = 99 (WinZip AES).
- Every entry carries extra field `0x9901` with vendor `AE`, version 2 (AE-2, CRC not stored),
  **strength 3 = AES-256**. The real method underneath is 8 (deflate), or 0 (stored) for the nested zips
  and `.info.ini`.
- The archive comment is empty and no data trails the end-of-central-directory record, so nothing is
  appended (no external signature block).
- The central directory isn't encrypted, so names, sizes and dates can be read without the password.
  That's all this survey had to work with.

### Contents (v4.00 vs v3.92, uncompressed sizes)

| Entry | v3.92 | v4.00 | Delta | Likely role (inferred from name) |
|---|---|---|---|---|
| `.info.ini` | 49 | 49 | 0 | version manifest |
| `history.txt` | 72 | 72 | 0 | changelog stub |
| `a_c203cpu_06_impl1.cpl` | 21,552 | 21,552 | 0 (same 2022 date) | CPLD/FPGA bitstream for C2-03CPU (`impl1` is Lattice Diamond naming) |
| `c2dcm_fw0102.bin` -> `c2dcm_fw0103.bin` | 53,172 | 58,216 | +5,044 | C2 comm/option module |
| `clicketh_fwV0392.bin` -> `V0400` | 297,104 | 333,168 | +36,064 | CLICK Ethernet CPU (C0-1x?) |
| `clickplus_main_fwV0392.bin` -> `V0400` | 805,649 | 857,201 | **+51,552** | CLICK PLUS (C2) main CPU, the probable ladder interpreter |
| `clickplus_sp_main_fwV0351.bin` | 630,213 | 630,213 | 0 (identical, 2025 date) | special/variant main, not rebuilt for v4 |
| `clickplus_sub_fwV0392.bin` -> `V0400` | 1,213,264 | 1,213,264 | 0 (compressed differs by 3 B) | C2 sub-processor, effectively unchanged |
| `Click_fw0392.bin` -> `Click_fw0400.bin` | 262,143 | 262,143 | 0 (fixed 256 KiB-1 flash image) | original CLICK CPU (C0-0x / basic C0-1x?); compressed +425 B |
| `nred.zip` | 203 MB | same | 0 | Node-RED bundle |
| `opcua.zip` | 92 MB | same | 0 | OPC UA bundle |

The mapping from CPU model to image is inferred from file names only. `PLCType.ini` lists memory maps,
not image names.

### Architecture and inner encryption

- **Architecture: unknown.** No plaintext bytes from any image were seen.
- **Inner images are very probably plaintext** once the ZIP layer is off (inference, but a strong one).
  Deflate shrinks them a lot: `clickplus_main` 857,201 -> 486,802 (57%), `clickplus_sub` 65%,
  `clicketh` 55%, `c2dcm` 53%. Encrypted or already-compressed data doesn't deflate. The ZIP AES layer is
  probably the only protection, though there could be a signature that the bootloader checks.
- The PC tool ships `7za.exe` next to `FWWriter.exe`. That suggests the flasher shells out to 7-Zip with a
  password, so the password probably lives in a CLICK PC binary. I didn't look for it.

### Metadata-only hints (weak)

- **`clickplus_main` (+51.5 KB, +6.4%) is where most of the v4 work went.** That fits the main C2 CPU
  hosting the new Out variants, Time&Event drum, MAX/MIN/MDN/AVG/RAND/SUMBIT and Break/Continue/nesting.
- `Click_fw0400.bin` grew by only ~425 B compressed. If that image is the C0-0x/basic CPU, this fits the
  research doc's note that C0-0x lacks the new instructions. It can't be confirmed without decrypting.
- `clicketh` (+36 KB) also grew a lot. That fits the Readme's "Embedded Discrete Message now on C0-1x" and
  possibly the new instructions on Ethernet C0-1x CPUs.

---

## (b) Per-probe status

**None answered. No interpreter code was visible.** Every probe stays a real-PLC conformance question,
as RESEARCH.md section 6 already plans.

| Probe | Status | Notes |
|---|---|---|
| O1-O5, O7 | open | no code visible |
| **O6** (does an Out variant write T<n>) | open | stays the top real-PLC probe |
| D1-D3 | open | |
| M1 (AVG/MDN rounding), M2 (MDN even count) | open | |
| M3 (RAND lo>hi), M4 (MAX/MIN on DH signedness) | open | |
| F1-F16 (incl. F3, count <= 0) | open | |

The only firmware evidence relevant to any probe is the size table in (a): `clickplus_main` is where the
new code is. That's location, not semantics.

---

## (c) What would need a real decompiler (only if the image were decrypted)

Steps, if the plaintext `clickplus_main_fwV0400.bin` and `V0392` were in hand:

1. Identify the ISA and load address. This is cheap: capstone/entropy, vector table, and string xrefs.
   ARM Cortex-M/A or Renesas RX are the likely candidates.
2. Diff v3.92 against v4.00 (BinDiff/Diffaphore-style function matching). That needs **Ghidra** (or IDA)
   for function boundaries and a call graph. capstone alone gives a linear disassembly, which isn't
   enough to match functions across builds.
3. Find the instruction dispatch, a jump table indexed by opcode. Then tie opcodes to the laddercodec
   func codes (Out 8193..8220) through `Compile.dll`, which is plaintext x86 in the PC install. Both sides
   need a decompiler.
4. Read the handlers. These probes are the most decompiler-friendly: **O6** (is the T-bit array written
   from the Out handler?), **M1/M2** (the divide and any `+n/2` before it), **F3** (the loop-count compare:
   `<= 0` skip vs `max(1, n)`), **M4** (signed vs unsigned compare on DH). The timing probes (O3, O5, D1)
   are readable too, but they're state machines and slower to get right.

Rough effort, after decryption: a day or two of Ghidra work to get the dispatch and 3-4 handlers with
moderate confidence. The real-PLC conformance runs would still be needed to confirm them.

---

## (d) Recommended next step

1. **Drop the firmware route and run the probes on a real PLC.** The conformance plan in RESEARCH.md
   section 6 is now the only evidence path for O*/D*/M*/F*. Put O6, F2, F3, M1/M2 and D1 first.
2. Going further needs the archive password. Recovering it from vendor binaries would mean getting around
   the vendor's encryption, and the EULA and DMCA 1201(f) interoperability-exemption questions are the
   user's to decide. This survey didn't do it. Even with the password, step (c)'s Ghidra work is the
   bigger cost, and its answers would still need confirming on a PLC.
3. A cheaper, allowed side lever that is *not* runtime semantics: the PC-side plaintext DLLs
   (`Inst_Out.dll`, `InstCtl_Out.dll`, `Inst_For.dll`, `Inst_Math.dll`, `Compile.dll`). They can show what
   parameters each new instruction carries into the compiled object (does Debounce's object include the
   T number? is there a per-side TD?). They help with laddercodec typed blobs, but they can't say what the
   firmware does at runtime.

Scripts used (central-directory listing only): the copied archives were deleted afterwards.
`C:\Users\Sam\AppData\Local\Temp\claude\C--Users-Sam-documents-github-pyrung\4138f4d6-e9fe-4691-9a62-0ec484f688ec\scratchpad\click-fw\scripts\listzip.py`,
`...\scripts\tail.py`.
