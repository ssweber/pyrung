"""The whole conformance suite: every group, on the checked-in manifest.

Export the CLICK project with::

    make conformance-export OUT=path/to/dir
"""

from __future__ import annotations

import sys
from pathlib import Path

from pyrung.conformance import ConformanceProgram, build_program, load_manifest
from tests.conformance import test_rung_semantics

MANIFEST_PATH = Path(__file__).with_name("manifest.toml")

#: Every group in the suite.  A new case module adds its ``group`` here.
ALL_GROUPS = [
    test_rung_semantics.group,
]


def build() -> ConformanceProgram:
    return build_program(ALL_GROUPS, load_manifest(MANIFEST_PATH))


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m tests.conformance.suite OUT_DIR")
        return 2
    program = build()
    out = program.export(argv[0])
    print(f"Wrote {out} (layout {program.program_hash} in DS1)")
    for g in program.groups:
        print(
            f"  {g.subroutine}: hash {g.group_hash} in DS{g.hash_register}, "
            f"{len(g.cases)} case(s), slots DS{g.register(1)}..DS{g.layout.last_register}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
