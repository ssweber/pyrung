"""Preserve generated API entries in Zensical's published sitemap."""

from __future__ import annotations

import argparse
import gzip
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin
from xml.etree import ElementTree

import yaml

SITEMAP_NAMESPACE = "http://www.sitemaps.org/schemas/sitemap/0.9"


def _route_for_source(source: PurePosixPath) -> str:
    if source.name == "index.md":
        route = source.parent.as_posix()
    else:
        route = source.with_suffix("").as_posix()
    return route.rstrip("/") + "/"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("docs", type=Path, help="Documentation source directory")
    parser.add_argument("site", type=Path, help="Generated site directory")
    args = parser.parse_args()

    docs = args.docs.resolve()
    site = args.site.resolve()
    config_file = docs.parent / "mkdocs.yml"
    config = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    site_url = str(config["site_url"]).rstrip("/") + "/"

    sitemap_file = site / "sitemap.xml"
    tree = ElementTree.parse(sitemap_file)
    root = tree.getroot()
    location_tag = f"{{{SITEMAP_NAMESPACE}}}loc"
    url_tag = f"{{{SITEMAP_NAMESPACE}}}url"
    existing = {node.text for node in root.iter(location_tag)}

    added = 0
    for markdown_file in sorted((docs / "reference").rglob("*.md")):
        source = PurePosixPath(markdown_file.relative_to(docs).as_posix())
        location = urljoin(site_url, _route_for_source(source))
        if location in existing:
            continue
        entry = ElementTree.SubElement(root, url_tag)
        ElementTree.SubElement(entry, location_tag).text = location
        existing.add(location)
        added += 1

    ElementTree.register_namespace("", SITEMAP_NAMESPACE)
    ElementTree.indent(tree, space="  ")
    xml = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
    sitemap_file.write_bytes(xml + b"\n")
    (site / "sitemap.xml.gz").write_bytes(gzip.compress(xml + b"\n", mtime=0))
    print(f"Added {added} generated API route(s) to sitemap.xml.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
