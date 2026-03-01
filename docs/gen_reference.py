"""Generate curated MkDocs API reference pages for pyrung public exports."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path

import mkdocs_gen_files

PACKAGE = "pyrung"
ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src" / PACKAGE
PUBLIC_MODULES = ("pyrung", "pyrung.click", "pyrung.circuitpy")
SKIP_PACKAGE_PAGES = set(PUBLIC_MODULES)


@dataclass(frozen=True)
class ReferencePage:
    slug: str
    title: str
    tier: str
    summary: str
    symbols: tuple[str, ...]


CLICK_BLOCK_SYMBOLS: tuple[str, ...] = (
    "pyrung.click.Bit",
    "pyrung.click.Int2",
    "pyrung.click.Float",
    "pyrung.click.Hex",
    "pyrung.click.Txt",
    "pyrung.click.x",
    "pyrung.click.y",
    "pyrung.click.c",
    "pyrung.click.t",
    "pyrung.click.ct",
    "pyrung.click.sc",
    "pyrung.click.ds",
    "pyrung.click.dd",
    "pyrung.click.dh",
    "pyrung.click.df",
    "pyrung.click.xd",
    "pyrung.click.yd",
    "pyrung.click.xd0u",
    "pyrung.click.yd0u",
    "pyrung.click.td",
    "pyrung.click.ctd",
    "pyrung.click.sd",
    "pyrung.click.txt",
)

CLICK_HELPER_SYMBOLS: tuple[str, ...] = (
    "pyrung.click.TagMap",
    "pyrung.click.ClickDataProvider",
    "pyrung.click.validate_click_program",
    "pyrung.click.send",
    "pyrung.click.receive",
)


PAGES: tuple[ReferencePage, ...] = (
    ReferencePage(
        slug="runtime",
        title="Runtime API",
        tier="Stable Core",
        summary="Runner lifecycle, system points, and timebase helpers.",
        symbols=(
            "pyrung.PLCRunner",
            "pyrung.system",
            "pyrung.TimeMode",
            "pyrung.TimeUnit",
            "pyrung.Tms",
            "pyrung.Ts",
            "pyrung.Tm",
            "pyrung.Th",
            "pyrung.Td",
        ),
    ),
    ReferencePage(
        slug="data-model",
        title="Data Model API",
        tier="Stable Core",
        summary="Structured tags, IEC data types, and memory block primitives.",
        symbols=(
            "pyrung.Field",
            "pyrung.auto",
            "pyrung.udt",
            "pyrung.named_array",
            "pyrung.TagType",
            "pyrung.Bool",
            "pyrung.Int",
            "pyrung.Dint",
            "pyrung.Real",
            "pyrung.Char",
            "pyrung.Word",
            "pyrung.Block",
            "pyrung.InputBlock",
            "pyrung.OutputBlock",
            "pyrung.SlotConfig",
        ),
    ),
    ReferencePage(
        slug="program-structure",
        title="Program Structure API",
        tier="Stable Core",
        summary="Program/rung builders and control-flow composition primitives.",
        symbols=(
            "pyrung.Program",
            "pyrung.Rung",
            "pyrung.program",
            "pyrung.branch",
            "pyrung.forloop",
            "pyrung.subroutine",
        ),
    ),
    ReferencePage(
        slug="instruction-set",
        title="Instruction Set API",
        tier="Stable Core",
        summary="Instruction blocks, conditions, and copy/casting modifiers.",
        symbols=(
            "pyrung.out",
            "pyrung.latch",
            "pyrung.reset",
            "pyrung.copy",
            "pyrung.run_function",
            "pyrung.run_enabled_function",
            "pyrung.blockcopy",
            "pyrung.fill",
            "pyrung.pack_bits",
            "pyrung.pack_text",
            "pyrung.pack_words",
            "pyrung.unpack_to_bits",
            "pyrung.unpack_to_words",
            "pyrung.calc",
            "pyrung.call",
            "pyrung.return_early",
            "pyrung.count_up",
            "pyrung.count_down",
            "pyrung.event_drum",
            "pyrung.search",
            "pyrung.shift",
            "pyrung.on_delay",
            "pyrung.off_delay",
            "pyrung.time_drum",
            "pyrung.rise",
            "pyrung.fall",
            "pyrung.all_of",
            "pyrung.any_of",
            "pyrung.as_value",
            "pyrung.as_ascii",
            "pyrung.as_text",
            "pyrung.as_binary",
        ),
    ),
    ReferencePage(
        slug="click-dialect",
        title="Click Dialect API",
        tier="Dialect Surface",
        summary="Click prebuilt blocks, aliases, and validation/communication helpers.",
        # Symbols listed for manifest validation; _write_curated_page renders
        # this page via grouped mkdocstrings members instead of per-symbol directives.
        symbols=CLICK_BLOCK_SYMBOLS + CLICK_HELPER_SYMBOLS,
    ),
    ReferencePage(
        slug="circuitpy-dialect",
        title="CircuitPython Dialect API",
        tier="Dialect Surface",
        summary="P1AM hardware model, module catalog, validation, and code generation.",
        symbols=(
            "pyrung.circuitpy.CircuitPyFinding",
            "pyrung.circuitpy.CircuitPyValidationReport",
            "pyrung.circuitpy.ChannelGroup",
            "pyrung.circuitpy.MAX_SLOTS",
            "pyrung.circuitpy.MODULE_CATALOG",
            "pyrung.circuitpy.ModuleDirection",
            "pyrung.circuitpy.ModuleSpec",
            "pyrung.circuitpy.P1AM",
            "pyrung.circuitpy.RunStopConfig",
            "pyrung.circuitpy.ValidationMode",
            "pyrung.circuitpy.board",
            "pyrung.circuitpy.generate_circuitpy",
            "pyrung.circuitpy.validate_circuitpy_program",
        ),
    ),
)


def _validate_manifest() -> None:
    exported = {module: set(import_module(module).__all__) for module in PUBLIC_MODULES}
    assigned: dict[str, list[str]] = defaultdict(list)
    unknown_modules: set[str] = set()

    for page in PAGES:
        for symbol in page.symbols:
            module_name, sep, export_name = symbol.rpartition(".")
            if not sep:
                raise RuntimeError(f"Invalid symbol reference '{symbol}'.")
            if module_name not in exported:
                unknown_modules.add(module_name)
                continue
            assigned[module_name].append(export_name)

    problems: list[str] = []
    if unknown_modules:
        modules = ", ".join(sorted(unknown_modules))
        problems.append(f"Symbols reference unmanaged modules: {modules}")

    for module_name, exported_names in exported.items():
        counts = Counter(assigned[module_name])
        duplicates = sorted(name for name, count in counts.items() if count > 1)
        assigned_names = set(counts)
        missing = sorted(exported_names - assigned_names)
        extra = sorted(assigned_names - exported_names)

        if duplicates:
            problems.append(f"{module_name}: duplicate symbols: {', '.join(duplicates)}")
        if missing:
            problems.append(f"{module_name}: missing exports: {', '.join(missing)}")
        if extra:
            problems.append(f"{module_name}: unknown symbols: {', '.join(extra)}")

    if problems:
        message = "API reference manifest does not match module __all__. " + " ".join(problems)
        raise RuntimeError(message)


def _write_curated_page(page: ReferencePage) -> None:
    doc_rel_path = Path("reference/api") / f"{page.slug}.md"
    lines = [
        f"# {page.title}",
        "",
        f"**Tier:** {page.tier}",
        "",
        page.summary,
        "",
    ]
    if page.slug == "click-dialect":
        lines.extend(["## Mapping and Runtime Helpers", ""])
        lines.append("::: pyrung.click")
        lines.extend(
            [
                "    options:",
                "      show_root_heading: false",
                "      show_docstring_description: false",
                "      heading_level: 4",
                "      show_object_full_path: false",
                "      members:",
            ]
        )
        for symbol in CLICK_HELPER_SYMBOLS:
            lines.append(f"        - {symbol.rsplit('.', 1)[1]}")
        lines.append("")

        lines.extend(["## Prebuilt Blocks and Aliases", ""])
        lines.append("::: pyrung.click")
        lines.extend(
            [
                "    options:",
                "      show_root_heading: false",
                "      show_docstring_description: false",
                "      heading_level: 5",
                "      show_object_full_path: false",
                "      members:",
            ]
        )
        for symbol in CLICK_BLOCK_SYMBOLS:
            lines.append(f"        - {symbol.rsplit('.', 1)[1]}")
        lines.append("")
    else:
        for symbol in page.symbols:
            lines.append(f"::: {symbol}")
            lines.append("")

    with mkdocs_gen_files.open(doc_rel_path, "w") as fd:
        fd.write("\n".join(lines).rstrip() + "\n")
    mkdocs_gen_files.set_edit_path(doc_rel_path, Path("docs/gen_reference.py"))


def _write_index() -> None:
    stable = [page for page in PAGES if page.tier == "Stable Core"]
    dialect = [page for page in PAGES if page.tier == "Dialect Surface"]

    lines = [
        "# API Reference",
        "",
        "This section is generated from explicit, versioned API manifests.",
        "",
        "## Stable Core Pages",
        "",
    ]
    for page in stable:
        lines.append(f"- [{page.title}](api/{page.slug}.md)")

    lines.extend(["", "## Dialect Pages", ""])
    for page in dialect:
        lines.append(f"- [{page.title}](api/{page.slug}.md)")

    lines.extend(
        [
            "",
            "## Module Pages",
            "",
            "Additional module-level pages are generated for deep links and internal browsing.",
        ]
    )

    with mkdocs_gen_files.open("reference/index.md", "w") as fd:
        fd.write("\n".join(lines).rstrip() + "\n")
    mkdocs_gen_files.set_edit_path("reference/index.md", Path("docs/gen_reference.py"))


def _write_module_pages() -> None:
    for module_path in sorted(SRC_DIR.rglob("*.py")):
        if module_path.name == "__main__.py":
            continue

        rel = module_path.relative_to(SRC_DIR)
        if rel.name == "__init__.py":
            module_parts = [PACKAGE, *rel.parent.parts] if rel.parent.parts else [PACKAGE]
            if rel.parent.parts:
                doc_rel_path = Path("reference/api").joinpath(*rel.parent.parts).with_suffix(".md")
            else:
                doc_rel_path = Path("reference/api") / f"{PACKAGE}.md"
        else:
            module_parts = [PACKAGE, *rel.with_suffix("").parts]
            doc_rel_path = (
                Path("reference/api").joinpath(*rel.with_suffix("").parts).with_suffix(".md")
            )

        identifier = ".".join(module_parts)
        if identifier in SKIP_PACKAGE_PAGES:
            continue
        with mkdocs_gen_files.open(doc_rel_path, "w") as fd:
            fd.write(f"::: {identifier}\n")

        mkdocs_gen_files.set_edit_path(doc_rel_path, module_path.relative_to(ROOT))


_validate_manifest()
for reference_page in PAGES:
    _write_curated_page(reference_page)
_write_index()
_write_module_pages()
