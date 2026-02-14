import subprocess
import sys
from pathlib import Path

from funlog import log_calls
from rich import get_console, reconfigure
from rich import print as rprint

# Use canonical absolute paths so ty behaves consistently on Windows regardless
# of how cwd casing is typed (e.g., "Documents" vs "documents").
PROJECT_ROOT = Path.cwd().resolve()
SRC_PATHS = [
    str(PROJECT_ROOT / "src"),
    str(PROJECT_ROOT / "tests"),
    str(PROJECT_ROOT / "devtools"),
]
DOC_PATHS = [str(Path("README.md"))]

# No emojis on legacy windows.
reconfigure(emoji=not get_console().options.legacy_windows)


def main():
    rprint()

    errcount = 0

    # 1. Spell check
    errcount += run(["codespell", "--write-changes", *SRC_PATHS, *DOC_PATHS])

    # 2. Ruff Linter
    errcount += run(["ruff", "check", "--fix", *SRC_PATHS])

    # 3. Ruff Formatter
    errcount += run(["ruff", "format", *SRC_PATHS])

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

    rprint()

    if errcount != 0:
        rprint(f"[bold red]:x: Lint failed with {errcount} errors.[/bold red]")
    else:
        rprint("[bold green]:white_check_mark: Lint passed![/bold green]")
    rprint()

    return errcount


@log_calls(level="warning", show_timing_only=True)
def run(cmd: list[str]) -> int:
    rprint()
    # Join with native separators for display
    display_cmd = " ".join(cmd)
    rprint(f"[bold green]:arrow_forward: {display_cmd}[/bold green]")

    errcount = 0
    try:
        # We use subprocess.run with the list directly.
        # On Windows, Python handles the list-to-string conversion safely.
        subprocess.run(cmd, text=True, check=True)
    except subprocess.CalledProcessError as e:
        rprint(f"[bold red]Error: {e}[/bold red]")
        errcount = 1
    except FileNotFoundError as e:
        rprint(f"[bold red]Executable not found: {e}[/bold red]")
        errcount = 1

    return errcount


if __name__ == "__main__":
    # Ensure the script exits with the correct code for the Makefile
    sys.exit(main())
