"""Stage publishable docs and generated compatibility inputs for Zensical."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

BLOCKED_FILENAMES = {
    "agents.md",
    "claude.md",
    "gen_llms.py",
    "gen_reference.py",
    "llms.txt",
}
BLOCKED_DIRECTORIES = {"__pycache__", "internal", "reference"}


def _is_publishable(relative_path: Path) -> bool:
    lowered_parts = tuple(part.lower() for part in relative_path.parts)
    if any(part in BLOCKED_DIRECTORIES for part in lowered_parts[:-1]):
        return False
    if relative_path.suffix.lower() in {".pyc", ".pyo"}:
        return False
    return relative_path.name.lower() not in BLOCKED_FILENAMES


def _validate_target(repository: Path, source: Path, target: Path) -> None:
    if target == source or target == repository:
        raise RuntimeError(f"Unsafe docs staging target: {target}")
    if repository not in target.parents:
        raise RuntimeError(f"Docs staging target must stay inside {repository}: {target}")


def stage(source: Path, target: Path) -> tuple[int, int]:
    """Synchronize publishable sources and generated inputs into *target*."""
    source = source.resolve()
    target = target.resolve()
    repository = source.parent.resolve()
    _validate_target(repository, source, target)
    target.mkdir(parents=True, exist_ok=True)

    source_files = {
        path.relative_to(source): path
        for path in source.rglob("*")
        if path.is_file() and _is_publishable(path.relative_to(source))
    }
    removed = 0
    for staged_file in sorted(path for path in target.rglob("*") if path.is_file()):
        relative_path = staged_file.relative_to(target)
        if relative_path not in source_files:
            staged_file.unlink()
            removed += 1

    copied = 0
    for relative_path, source_file in sorted(source_files.items()):
        staged_file = target / relative_path
        staged_file.parent.mkdir(parents=True, exist_ok=True)
        if not staged_file.is_file() or staged_file.read_bytes() != source_file.read_bytes():
            shutil.copy2(source_file, staged_file)
            copied += 1

    for directory in sorted(
        (path for path in target.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass

    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source))
    from gen_llms import generate as generate_llms  # noqa: PLC0415
    from gen_reference import generate as generate_reference  # noqa: PLC0415

    generate_reference(target)
    generate_llms(
        target / "llms.txt",
        docs_dir=target,
        config_file=repository / "mkdocs.yml",
    )
    return copied, removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Authored documentation directory")
    parser.add_argument("target", type=Path, help="Ignored Zensical staging directory")
    args = parser.parse_args()
    copied, removed = stage(args.source, args.target)
    print(f"Staged docs: copied {copied}, removed {removed}, generated 8.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
