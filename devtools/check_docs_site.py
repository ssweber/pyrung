"""Validate generated documentation inputs and their published routes."""

from __future__ import annotations

import argparse
import re
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

EXPECTED_REFERENCE_SOURCES = {
    PurePosixPath("reference/index.md"),
    PurePosixPath("reference/api/runtime.md"),
    PurePosixPath("reference/api/data-model.md"),
    PurePosixPath("reference/api/program-structure.md"),
    PurePosixPath("reference/api/instruction-set.md"),
    PurePosixPath("reference/api/click-dialect.md"),
    PurePosixPath("reference/api/circuitpy-dialect.md"),
}
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def ascii_text(value: object) -> str:
    return str(value).encode("ascii", errors="backslashreplace").decode("ascii")


def _route_for_source(source: PurePosixPath) -> PurePosixPath:
    if source.name == "index.md":
        return source.with_name("index.html")
    return source.with_suffix("") / "index.html"


def _route_for_url(url: str, site_prefix: str) -> PurePosixPath | None:
    parsed = urlparse(url)
    path = unquote(parsed.path)
    if not path.startswith(site_prefix):
        return None
    relative = path[len(site_prefix) :].lstrip("/")
    if not relative or path.endswith("/"):
        return PurePosixPath(relative) / "index.html"
    candidate = PurePosixPath(relative)
    return candidate if candidate.suffix else candidate / "index.html"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("docs", type=Path, help="Documentation source directory")
    parser.add_argument("site", type=Path, help="Generated site directory")
    args = parser.parse_args()

    docs = args.docs.resolve()
    site = args.site.resolve()
    errors: list[str] = []

    actual_sources = {
        PurePosixPath(path.relative_to(docs).as_posix())
        for path in (docs / "reference").rglob("*.md")
    }
    if actual_sources != EXPECTED_REFERENCE_SOURCES:
        missing = sorted(EXPECTED_REFERENCE_SOURCES - actual_sources)
        extra = sorted(actual_sources - EXPECTED_REFERENCE_SOURCES)
        if missing:
            errors.append("missing generated reference sources: " + ", ".join(map(str, missing)))
        if extra:
            errors.append("unexpected generated reference sources: " + ", ".join(map(str, extra)))

    sitemap_file = site / "sitemap.xml"
    sitemap = sitemap_file.read_text(encoding="utf-8") if sitemap_file.is_file() else ""
    for source in sorted(EXPECTED_REFERENCE_SOURCES):
        route = _route_for_source(source)
        if not (site / Path(route.as_posix())).is_file():
            errors.append(f"missing generated reference route: {route}")
        public_route = "/pyrung/" + route.parent.as_posix().strip(".") + "/"
        if public_route.replace("//", "/") not in sitemap:
            errors.append(f"generated reference route missing from sitemap: {public_route}")

    source_llms = docs / "llms.txt"
    built_llms = site / "llms.txt"
    if not source_llms.is_file():
        errors.append("missing generated docs/llms.txt")
    elif not built_llms.is_file():
        errors.append("missing published site/llms.txt")
    elif source_llms.read_bytes() != built_llms.read_bytes():
        errors.append("published llms.txt does not match the generated source")
    else:
        llms_text = built_llms.read_text(encoding="utf-8")
        for url in MARKDOWN_LINK.findall(llms_text):
            route = _route_for_url(url, "/pyrung/")
            if route is not None and not (site / Path(route.as_posix())).is_file():
                errors.append(f"llms.txt target is not published: {url}")

    if errors:
        for error in errors:
            print(f"ERROR: {ascii_text(error)}")
        print(f"ERROR: docs-site check found {len(errors)} problem(s).")
        return 1

    print("OK: generated docs inputs and routes match.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
