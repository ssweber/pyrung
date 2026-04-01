"""Connect to P1AM-200 serial console. Ctrl+] to exit."""

# /// script
# dependencies = ["pyserial"]
# ///
import ctypes
import ctypes.wintypes
import msvcrt
import sys
import threading

import serial
import serial.tools.list_ports

# Disable Windows console processed-input so Ctrl+C is readable via msvcrt
_kernel32 = ctypes.windll.kernel32
_STD_INPUT_HANDLE = ctypes.wintypes.DWORD(-10)
_h_stdin = _kernel32.GetStdHandle(_STD_INPUT_HANDLE)
_orig_mode = ctypes.wintypes.DWORD()
_kernel32.GetConsoleMode(_h_stdin, ctypes.byref(_orig_mode))
_ENABLE_PROCESSED_INPUT = 0x0001
_kernel32.SetConsoleMode(_h_stdin, _orig_mode.value & ~_ENABLE_PROCESSED_INPUT)

# Auto-detect or use CLI arg
if len(sys.argv) > 1:
    port = sys.argv[1]
else:
    ports = [p for p in serial.tools.list_ports.comports() if "USB" in (p.description or "")]
    if not ports:
        ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("No COM ports found")
        sys.exit(1)
    print("Available ports:")
    for p in ports:
        print(f"  {p.device}: {p.description}")
    port = ports[0].device
    print(f"\nUsing {port}")

ser = serial.Serial(port, 115200, timeout=0.1)
print(f"Connected to {port} — Ctrl+] to exit\n")
print("  Keys are forwarded to the board.")
print("  Ctrl+C = interrupt program (enter REPL)")
print("  Ctrl+D = soft-reboot")
print("  Ctrl+] = exit monitor\n")

_stop = threading.Event()


def _reader():
    """Read from serial and print to stdout."""
    while not _stop.is_set():
        data = ser.read(256)
        if data:
            sys.stdout.write(data.decode("utf-8", errors="replace"))
            sys.stdout.flush()


reader_thread = threading.Thread(target=_reader, daemon=True)
reader_thread.start()

try:
    while True:
        if msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch == b"\x1d":  # Ctrl+] — exit monitor
                break
            ser.write(ch)
        else:
            _stop.wait(0.01)
finally:
    _stop.set()
    ser.close()
    # Restore original console mode
    _kernel32.SetConsoleMode(_h_stdin, _orig_mode.value)
    print("\nDisconnected.")
