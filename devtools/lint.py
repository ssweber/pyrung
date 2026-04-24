import argparse
import subprocess
import sys
from pathlib import Path

# Use canonical absolute paths so ty behaves consistently on Windows regardless
# of how cwd casing is typed (e.g., "Documents" vs "documents").
PROJECT_ROOT = Path.cwd().resolve()
SRC_PATHS = [
    str(PROJECT_ROOT / "src"),
    str(PROJECT_ROOT / "tests"),
    str(PROJECT_ROOT / "devtools"),
]
VSCODE_JS_DIR = PROJECT_ROOT / "editors" / "vscode" / "pyrung-debug"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run lint/type checks for the repository.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run read-only checks suitable for CI (no autofix).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print()

    errcount = 0

    if args.check:
        errcount += run(["ruff", "check", *SRC_PATHS])
    else:
        errcount += run(["ruff", "check", "--fix", *SRC_PATHS])

    if args.check:
        errcount += run(["ruff", "format", "--check", *SRC_PATHS])
    else:
        errcount += run(["ruff", "format", *SRC_PATHS])

    errcount += run(["python", "devtools/semantic_lint.py"])

    errcount += run(
        [
            "ty",
            "check",
            "--project",
            str(PROJECT_ROOT),
            "--error",
            "unused-ignore-comment",
            *SRC_PATHS,
        ]
    )

    for js_file in sorted(VSCODE_JS_DIR.glob("*.js")):
        errcount += run(["node", "-c", str(js_file)])

    errcount += run(["node", str(PROJECT_ROOT / "devtools" / "check_webview_scripts.js")])

    print()

    if errcount != 0:
        print(f"Lint failed with {errcount} error(s).")
    else:
        print("Lint passed.")
    print()

    return errcount


def run(cmd: list[str]) -> int:
    print()
    print(f"==> {' '.join(cmd)}")

    try:
        subprocess.run(cmd, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        print(f"Error: {exc}")
        return 1
    except FileNotFoundError as exc:
        print(f"Executable not found: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
