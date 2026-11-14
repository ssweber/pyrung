"""Say which conformance parts still need a run on the CLICK.

Usage (from the pyrung repo root):

    uv run python -I devtools/conf_status.py            # every part
    uv run python -I devtools/conf_status.py follow_flags units

A case is done when confirmed.toml holds it at its current hash. A probe (a case whose expectation is a
`between(...)` range) never confirms anything by passing, so it is done when readings.toml holds a reading at
its current hash. A part needs a run when any case is not done. Otherwise check the open project with
devtools/ckp_check.py and move on.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.conformance import suite  # noqa: E402
from tests.conformance.probe_kit import asks_for_a_reading  # noqa: E402


def main(argv: list[str]) -> int:
    confirmed = tomllib.loads(suite.CONFIRMED_PATH.read_text(encoding="utf-8"))["cases"]
    readings = tomllib.loads(suite.READINGS_PATH.read_text(encoding="utf-8")).get("reading", [])
    read_at = {(r["case"], r["hash"]) for r in readings}
    for part in argv or [*suite.PARTS, *suite.APART_PARTS]:
        todo = []
        total = 0
        for g in suite.build(part).groups:
            for _i, case in g.cases:
                total += 1
                cid, h = f"{g.name}/{case.id}", g.case_hashes.get(case.id)
                if confirmed.get(cid, {}).get("hash") == h:
                    continue
                if asks_for_a_reading(case):
                    if (cid, h) in read_at:
                        continue
                    todo.append(f"{cid} (probe, no reading at this hash)")
                elif case.pending:
                    todo.append(f"{cid} (pending: pyrung differs)")
                elif cid in confirmed:
                    todo.append(f"{cid} (changed since it was confirmed)")
                else:
                    todo.append(f"{cid} (never confirmed)")
        verdict = "run it" if todo else "done: check the project only"
        print(f"{part}: {total - len(todo)}/{total} done, {verdict}")
        for line in todo:
            print(f"    {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
