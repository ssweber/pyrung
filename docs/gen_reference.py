"""Generate MkDocs API reference pages for pyrung modules."""

from pathlib import Path

import mkdocs_gen_files

PACKAGE = "pyrung"
ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src" / PACKAGE

overview_lines = [
    "# API Reference",
    "",
    "This section is generated from source using `mkdocstrings`.",
    "",
    "## Modules",
]

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
    with mkdocs_gen_files.open(doc_rel_path, "w") as fd:
        fd.write(f"::: {identifier}\n")

    mkdocs_gen_files.set_edit_path(doc_rel_path, module_path.relative_to(ROOT))
    overview_target = doc_rel_path.relative_to("reference").as_posix()
    overview_lines.append(f"- [`{identifier}`]({overview_target})")

with mkdocs_gen_files.open("reference/index.md", "w") as fd:
    fd.write("\n".join(overview_lines) + "\n")
