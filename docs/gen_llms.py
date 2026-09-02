"""Generate the curated llms.txt index for the pyrung documentation site."""

from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin

import yaml

DOCS_DIR = Path(__file__).resolve().parent
CONFIG_FILE = DOCS_DIR.parent / "mkdocs.yml"

SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Home", ("index.md",)),
    (
        "Learn",
        (
            "learn/index.md",
            "learn/scan-cycle.md",
            "learn/tags.md",
            "learn/latch-reset.md",
            "learn/assignment.md",
            "learn/timers.md",
            "learn/counters.md",
            "learn/state-machines.md",
            "learn/branches.md",
            "learn/structured-tags.md",
            "learn/testing.md",
            "learn/hardware.md",
        ),
    ),
    (
        "Getting Started",
        (
            "getting-started/installation.md",
            "getting-started/quickstart.md",
            "getting-started/concepts.md",
        ),
    ),
    (
        "Instruction Reference",
        (
            "instructions/index.md",
            "instructions/rungs.md",
            "instructions/conditions.md",
            "instructions/coils.md",
            "instructions/timers-counters.md",
            "instructions/copy.md",
            "instructions/math.md",
            "instructions/drum-shift-search.md",
            "instructions/program-control.md",
            "instructions/communication.md",
        ),
    ),
    (
        "Guides — Essentials",
        ("guides/runner.md", "guides/testing.md", "guides/tag-structures.md"),
    ),
    (
        "Guides — Declare, Analyze, Commission",
        (
            "guides/commissioning.md",
            "guides/physical-harness.md",
            "guides/analysis.md",
            "guides/analysis-structure.md",
            "guides/ladder-lints.md",
            "guides/analysis-diagnosis.md",
            "guides/analysis-causal.md",
            "guides/analysis-coverage.md",
            "guides/verification.md",
        ),
    ),
    (
        "Guides — Platform",
        (
            "guides/click-quickstart.md",
            "guides/click-cheatsheet.md",
            "guides/circuitpy-quickstart.md",
        ),
    ),
    ("Guides — Tools", ("guides/dap-vscode.md", "guides/architecture.md")),
    (
        "Dialects",
        (
            "dialects/click.md",
            "dialects/click-codegen.md",
            "dialects/circuitpy.md",
            "dialects/circuitpy-modbus.md",
        ),
    ),
    (
        "API Reference",
        (
            "reference/index.md",
            "reference/api/runtime.md",
            "reference/api/data-model.md",
            "reference/api/program-structure.md",
            "reference/api/instruction-set.md",
            "reference/api/click-dialect.md",
            "reference/api/circuitpy-dialect.md",
        ),
    ),
)


def _load_config(config_file: Path) -> dict[str, Any]:
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected a mapping in {config_file}.")
    return data


def _nav_titles(nav: object) -> dict[str, str]:
    titles: dict[str, str] = {}
    if isinstance(nav, list):
        for item in nav:
            titles.update(_nav_titles(item))
    elif isinstance(nav, dict):
        for title, value in nav.items():
            if isinstance(value, str) and value.endswith(".md"):
                titles[value] = str(title)
            else:
                titles.update(_nav_titles(value))
    return titles


def _title(markdown_file: Path) -> str:
    for line in markdown_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    raise RuntimeError(f"Missing level-one heading in {markdown_file}.")


def _page_url(site_url: str, source_path: str) -> str:
    path = PurePosixPath(source_path)
    if path.name == "index.md":
        route = path.parent.as_posix()
    else:
        route = path.with_suffix("").as_posix()
    return urljoin(site_url.rstrip("/") + "/", route.rstrip("/") + "/")


def generate(
    output_file: Path = DOCS_DIR / "llms.txt",
    *,
    docs_dir: Path = DOCS_DIR,
    config_file: Path = CONFIG_FILE,
) -> Path:
    """Generate the curated HTML-route index used by public agents."""
    config = _load_config(config_file)
    site_name = str(config["site_name"])
    site_description = str(config["site_description"])
    site_url = str(config["site_url"])
    nav_titles = _nav_titles(config.get("nav"))
    lines = [f"# {site_name}", "", f"> {site_description}", ""]

    for section, source_paths in SECTIONS:
        lines.extend((f"## {section}", ""))
        for source_path in source_paths:
            markdown_file = docs_dir / Path(source_path)
            if not markdown_file.is_file():
                raise RuntimeError(f"Missing llms.txt source page: {markdown_file}.")
            title = nav_titles[source_path] if source_path in nav_titles else _title(markdown_file)
            lines.append(f"- [{title}]({_page_url(site_url, source_path)})")
        lines.append("")

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return output_file


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--docs-dir",
        type=Path,
        default=DOCS_DIR,
        help="Documentation source directory used to resolve indexed pages",
    )
    parser.add_argument(
        "--config-file",
        type=Path,
        default=CONFIG_FILE,
        help="Site configuration used for metadata, navigation titles, and URLs",
    )
    parser.add_argument(
        "--output-file",
        type=Path,
        default=DOCS_DIR / "llms.txt",
        help="Path to generated llms.txt",
    )
    args = parser.parse_args()
    generate(
        args.output_file.resolve(),
        docs_dir=args.docs_dir.resolve(),
        config_file=args.config_file.resolve(),
    )
    print("Generated llms.txt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
