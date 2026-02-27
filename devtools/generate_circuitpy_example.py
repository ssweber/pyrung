from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
for root in (str(PROJECT_ROOT), str(SRC_ROOT)):
    if root not in sys.path:
        sys.path.insert(0, root)

from pyrung.circuitpy import P1AM, generate_circuitpy
from pyrung.core.program import Program


def _parse_slot(value: str) -> tuple[int, str]:
    number_text, sep, module = value.partition(":")
    if not sep or not number_text or not module:
        raise argparse.ArgumentTypeError(
            f"Invalid --slot value {value!r}. Expected format: <number>:<module>"
        )
    try:
        number = int(number_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid slot number {number_text!r} in --slot {value!r}"
        ) from exc
    return number, module


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate CircuitPython code from an example Program."
    )
    parser.add_argument(
        "--module",
        default="examples.circuitpy_codegen_review",
        help=(
            "Python module containing a Program object "
            "(default: examples.circuitpy_codegen_review)."
        ),
    )
    parser.add_argument(
        "--program",
        default="logic",
        help="Attribute name of the Program object in the module (default: logic).",
    )
    parser.add_argument(
        "--output",
        default="scratchpad/generated_circuitpy.py",
        help="Output file path (default: scratchpad/generated_circuitpy.py).",
    )
    parser.add_argument(
        "--target-scan-ms",
        type=float,
        default=10.0,
        help="Target scan time in milliseconds (default: 10.0).",
    )
    parser.add_argument(
        "--watchdog-ms",
        type=int,
        default=500,
        help="Watchdog timeout in milliseconds (default: 500).",
    )
    parser.add_argument(
        "--slot",
        action="append",
        type=_parse_slot,
        default=None,
        help=(
            "Slot mapping in <number>:<module> format. "
            "Repeat this flag for multiple slots. "
            "Default: --slot 1:P1-08SIM --slot 2:P1-08TRS"
        ),
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Also print generated source to stdout.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    os.environ.setdefault("PYRUNG_DAP_ACTIVE", "1")

    try:
        module = importlib.import_module(args.module)
    except Exception as exc:
        print(f"Failed to import module {args.module!r}: {exc}", file=sys.stderr)
        return 1

    program_obj = getattr(module, args.program, None)
    if not isinstance(program_obj, Program):
        print(
            f"{args.module}.{args.program} is not a Program object.",
            file=sys.stderr,
        )
        return 1

    slot_specs = args.slot or [(1, "P1-08SIM"), (2, "P1-08TRS")]
    hw = P1AM()
    for number, module_name in slot_specs:
        hw.slot(number, module_name)

    source = generate_circuitpy(
        program_obj,
        hw,
        target_scan_ms=args.target_scan_ms,
        watchdog_ms=args.watchdog_ms,
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(source, encoding="utf-8")

    print(f"Wrote {output_path} ({len(source.splitlines())} lines)")
    if args.stdout:
        print(source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
