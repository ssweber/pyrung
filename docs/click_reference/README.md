# CLICK PLC Reference for pyrung

Essential documentation links for implementing pyrung's PLC simulation engine.

## Core Instructions

- [Instruction Menu](instruction_menu.md) - Full instruction overview

### Contacts
- [Normally Open Contact](contact_no.md)
- [Normally Closed Contact](contact_nc.md)
- [Edge Contact](contact_edge_rise_fall.md) - Rising/falling edge detection
- [Compare Contact](contact_compare.md) - Comparison operations (EQ, NE, LT, LE, GT, GE)

### Coils
- [Out Coil](coil_out.md)
- [Set Coil](coil_set.md) - Latch
- [Reset Coil](coil_reset.md)

### Timers & Counters
- [Timer](timers.md) - TON, TOF, RTON
- [Counter](counters.md) - CTU, CTD

### Copy Instructions
- [Copy Instruction: Single Copy](copy_single.md)
- [Copy Instruction: Block Copy](copy_block.md)
- [Copy Instruction: Fill](copy_fill.md)
- [Copy Instruction: Pack Copy](copy_pack.md) - Pack 16 bits into word
- [Copy Instruction: Unpack Copy](copy_unpack.md) - Unpack word to 16 bits
- [Byte Swap and Word Swap](copy_swap.md)

### Math Instructions
- [Advanced Instructions](math_advanced.md) - Overview
- [Math (Decimal)](math_decimal.md) - ADD, SUB, MUL, DIV, MOD, etc.
- [Math (Hex)](math_hex.md) - AND, OR, XOR, NOT, shift operations

### Drum & Shift
- [Drum Instruction: Time Base](drum_time.md) - Sequencer (time-driven)
- [Drum Instruction: Event Base](drum_event.md) - Sequencer (event-driven)
- [Shift Register](shift_register.md) - Bit/word shifting

### Search
- [Search Instruction](search.md) - Search arrays for values

## Program Structure

### Control Flow
- [Call Instruction](call.md) - Subroutine calls
- [Return Instruction](return.md)
- [For Instruction](for.md)
- [Next Instruction](next.md)
- [End Instruction](end.md)

## Data & Memory

### Reference Tables
- [Data Types](data_types.md) - BIT, INT, INT2, FLOAT, etc.
- [Memory Addresses](memory_addresses.md) - X, Y, C, T, CT, DS, DD, DF, DH banks
- [Data Compatibility Table](data_compatibility.md) - What can be used where
- [Pointer Addressing](pointer_addressing.md)
- [Casting Between Data types](casting.md)
- [ASCII Table](ascii_table.md) - For text/string operations
- [Error Codes and Messages List](error_codes_msgs.md) - Comprehensive error reference
- [System Reserved Characters](reserved_chars.md)

### System Memory
- [System Control Relays](system_control_relays.md) - SC bits (read-only system state)
- [System Data Registers](system_data_registers.md) - SD registers (read-only system data)
- [Managing Retentive Memory](retentive_memory.md)

## Setup Reference

- [Scan Time](scan_time.md) - How scan timing works
- [Interrupt Setup](interrupt_setup.md) - Interrupt programs

## Error Codes

- [PLC Error Code List](error_codes_plc.md)
- [Programming Error Codes](error_codes_programming.md)
