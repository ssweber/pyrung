"""Connect to P1AM-200 serial console. Press Ctrl+C to exit."""
# /// script
# dependencies = ["pyserial"]
# ///
import serial
import serial.tools.list_ports
import sys

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

with serial.Serial(port, 115200, timeout=1) as ser:
    print(f"Connected to {port} — Ctrl+C to exit, Ctrl+D to soft-reboot board\n")
    while True:
        line = ser.readline()
        if line:
            print(line.decode("utf-8", errors="replace"), end="")
