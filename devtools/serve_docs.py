"""Serve staged Zensical docs while synchronizing authored source changes."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from stage_docs import _is_publishable, stage

REPOSITORY = Path(__file__).resolve().parent.parent
SOURCE = REPOSITORY / "docs"
TARGET = REPOSITORY / "generated-docs"


def _snapshot() -> tuple[tuple[str, int, int], ...]:
    files = [
        path
        for path in SOURCE.rglob("*")
        if path.is_file()
        and (
            _is_publishable(path.relative_to(SOURCE))
            or path.name in {"gen_llms.py", "gen_reference.py"}
        )
    ]
    files.append(REPOSITORY / "mkdocs.yml")
    return tuple(
        sorted(
            (
                path.relative_to(REPOSITORY).as_posix(),
                path.stat().st_mtime_ns,
                path.stat().st_size,
            )
            for path in files
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev-addr", default="localhost:8000", help="Preview IP:PORT")
    args = parser.parse_args()

    stage(SOURCE, TARGET)
    snapshot = _snapshot()
    command = [sys.executable, "-m", "zensical", "serve", "--dev-addr", args.dev_addr]
    process = subprocess.Popen(command, cwd=REPOSITORY)
    try:
        while process.poll() is None:
            time.sleep(0.5)
            updated = _snapshot()
            if updated != snapshot:
                stage(SOURCE, TARGET)
                snapshot = updated
    except KeyboardInterrupt:
        process.terminate()
    return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
