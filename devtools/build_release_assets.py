"""Build release assets: starter project zip and VSIX extension.

Usage (from repo root):
    uv run python devtools/build_release_assets.py [--out-dir dist/assets]

Produces:
    pyrung-starter-<version>.zip   — example project with CSV round-trip
    pyrung-debug-<version>.vsix    — VS Code debugger extension
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTENSION_DIR = ROOT / "editors" / "vscode" / "pyrung-debug"


def _get_version() -> str:
    """Read version from pyproject.toml via importlib.metadata."""
    import importlib.metadata

    return importlib.metadata.version("pyrung")


# ---------------------------------------------------------------------------
# Starter zip
# ---------------------------------------------------------------------------


def _build_starter_zip(out_dir: Path, version: str) -> Path:
    """Generate the starter project zip from click_starter.py."""
    import os
    import tempfile

    from pyrung.click import ladder_to_pyrung_project, pyrung_to_ladder

    # Suppress the example's simulation output on import.
    os.environ["PYRUNG_DAP_ACTIVE"] = "1"

    # Force-reload so the guard suppresses output
    if "examples.click_starter" in sys.modules:
        del sys.modules["examples.click_starter"]
    sys.path.insert(0, str(ROOT))
    from examples.click_starter import logic, mapping

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # 1. Export pyrung → Click CSV
        csv_dir = tmp / "click_csv"
        bundle = pyrung_to_ladder(logic, mapping)
        bundle.write(csv_dir)
        mapping.to_nickname_file(csv_dir / "nicknames.csv")

        # 2. Import Click CSV → scaffold project
        project_dir = tmp / "pyrung_project"
        ladder_to_pyrung_project(
            csv_dir,
            nickname_csv=csv_dir / "nicknames.csv",
            output_dir=project_dir,
        )

        # 3. Copy original source
        original_dir = tmp / "original"
        original_dir.mkdir()
        shutil.copy2(ROOT / "examples" / "click_starter.py", original_dir)

        # 4. Zip everything
        zip_name = f"pyrung-starter-{version}.zip"
        zip_path = out_dir / zip_name
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for subdir in (csv_dir, project_dir, original_dir):
                prefix = subdir.name
                for file in sorted(subdir.rglob("*")):
                    if file.is_file():
                        arcname = f"{prefix}/{file.relative_to(subdir)}"
                        zf.write(file, arcname)

    print(f"Built {zip_path} ({zip_path.stat().st_size:,} bytes)")
    return zip_path


# ---------------------------------------------------------------------------
# VSIX
# ---------------------------------------------------------------------------


def _build_vsix(out_dir: Path, version: str) -> Path:
    """Package the VS Code extension into a .vsix file."""
    vsix_name = f"pyrung-debug-{version}.vsix"
    vsix_path = out_dir / vsix_name

    # vsce package writes to cwd; run it in the extension dir, then move.
    try:
        subprocess.run(
            ["npx", "@vscode/vsce", "package", "--out", str(vsix_path)],
            cwd=EXTENSION_DIR,
            check=True,
        )
    except FileNotFoundError:
        print("WARNING: npx not found — skipping VSIX build.", file=sys.stderr)
        print("Install Node.js to build the VSIX.", file=sys.stderr)
        return vsix_path

    print(f"Built {vsix_path} ({vsix_path.stat().st_size:,} bytes)")
    return vsix_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Build pyrung release assets.")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "dist" / "assets",
        help="Output directory for built assets (default: dist/assets)",
    )
    args = parser.parse_args()

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    version = _get_version()
    print(f"Building release assets for pyrung {version}")

    _build_starter_zip(out_dir, version)
    _build_vsix(out_dir, version)

    print(f"\nAssets written to {out_dir}")


if __name__ == "__main__":
    main()
