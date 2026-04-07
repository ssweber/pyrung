"""Build release assets: starter project zip and VSIX extension.

Usage (from repo root):
    uv run python devtools/build_release_assets.py [--out-dir dist/assets]

Produces:
    pyrung-starter-<version>.zip   — example project with CSV round-trip
    pyrung-debug-<version>.vsix    — VS Code debugger extension
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTENSION_DIR = ROOT / "editors" / "vscode" / "pyrung-debug"


def _mpy_cross_path() -> Path:
    """Return the platform-appropriate mpy-cross binary."""
    if platform.system() == "Windows":
        return ROOT / "devtools" / "mpy-cross-windows-8.2.3.static.exe"
    return ROOT / "devtools" / "mpy-cross-linux-amd64-8.2.3.static"


def _get_version() -> str:
    """Read version from pyproject.toml via importlib.metadata."""
    import importlib.metadata

    return importlib.metadata.version("pyrung")


# ---------------------------------------------------------------------------
# Starter zip
# ---------------------------------------------------------------------------


def _build_starter_zip(out_dir: Path, version: str) -> Path:
    """Generate the starter project zip from click_conveyor.py."""
    import os
    import tempfile

    from pyrung.circuitpy import RunStopConfig, generate_circuitpy
    from pyrung.click import ladder_to_pyrung_project, pyrung_to_ladder

    # Suppress the example's simulation output on import.
    os.environ["PYRUNG_DAP_ACTIVE"] = "1"

    # Force-reload so the guard suppresses output
    for mod in list(sys.modules):
        if mod.startswith("examples."):
            del sys.modules[mod]
    sys.path.insert(0, str(ROOT))
    from examples.click_conveyor import logic, mapping

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

        # 3. Generate CircuitPython output
        from examples.circuitpy_conveyor import hw
        from examples.circuitpy_conveyor import logic as cpy_logic

        cpy_result = generate_circuitpy(
            cpy_logic,
            hw,
            target_scan_ms=10.0,
            watchdog_ms=5000,
            runstop=RunStopConfig(),
            force_runtime=True,
        )

        circuitpy_dir = tmp / "circuitpy"
        circuitpy_dir.mkdir()
        (circuitpy_dir / "code.py").write_text(cpy_result.code, encoding="utf-8")
        if cpy_result.runtime:
            rt_py = circuitpy_dir / "pyrung_rt.py"
            rt_py.write_text(cpy_result.runtime, encoding="utf-8")

            # Compile pyrung_rt.py → lib/pyrung_rt.mpy (mirrors CIRCUITPY layout)
            lib_dir = circuitpy_dir / "lib"
            lib_dir.mkdir()
            mpy_cross = _mpy_cross_path()
            if mpy_cross.exists():
                try:
                    subprocess.run(
                        [str(mpy_cross), str(rt_py), "-o", str(lib_dir / "pyrung_rt.mpy")],
                        check=True,
                    )
                except (subprocess.CalledProcessError, OSError) as exc:
                    print(f"WARNING: mpy-cross failed ({exc}) — .mpy omitted.", file=sys.stderr)
            else:
                print(
                    f"WARNING: mpy-cross not found at {mpy_cross} — .mpy omitted.",
                    file=sys.stderr,
                )

        # 4. Copy original sources
        original_dir = tmp / "original"
        original_dir.mkdir()
        shutil.copy2(ROOT / "examples" / "click_conveyor.py", original_dir)
        shutil.copy2(ROOT / "examples" / "circuitpy_conveyor.py", original_dir)

        # 5. Zip everything
        zip_name = f"pyrung-starter-{version}.zip"
        zip_path = out_dir / zip_name
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for subdir in (csv_dir, project_dir, circuitpy_dir, original_dir):
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
