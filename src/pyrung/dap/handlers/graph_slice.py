"""Program graph, slice, and query request handling for the DAP adapter.

Owns pyrungGraph, pyrungSlice, and pyrungQuery custom requests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pyrung.core.analysis.dataview import DataView

HandlerResult = tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]

_ROLE_PREFIXES = {
    "i": "inputs",
    "p": "pivots",
    "t": "terminals",
    "x": "isolated",
}

_SLICE_PREFIXES = {"upstream", "downstream"}


def _parse_query(query: str, view: DataView) -> DataView:
    """Parse a query string and chain DataView operations.

    Syntax (space-separated tokens, applied left to right):
    - ``btn``                bare text → ``.contains("btn")``
    - ``i:``                 role prefix → ``.inputs()``
    - ``i:btn``              role + text → ``.inputs().contains("btn")``
    - ``upstream:Running``   slice → ``.upstream("Running")``
    - ``downstream:Tag``     slice → ``.downstream("Tag")``
    """
    tokens = query.split()
    for token in tokens:
        if ":" in token:
            prefix, _, arg = token.partition(":")
            prefix_lower = prefix.lower()

            if prefix_lower in _ROLE_PREFIXES:
                method = getattr(view, _ROLE_PREFIXES[prefix_lower])
                view = method()
                if arg:
                    view = view.contains(arg)
            elif prefix_lower in _SLICE_PREFIXES:
                if not arg:
                    continue
                method = getattr(view, prefix_lower)
                view = method(arg)
            else:
                # Unknown prefix — treat whole token as contains text
                view = view.contains(token)
        else:
            view = view.contains(token)
    return view


@dataclass(frozen=True)
class _GraphRequestArgs:
    sourceFile: Any = None


@dataclass(frozen=True)
class _SliceRequestArgs:
    tag: Any = None
    direction: Any = None


@dataclass(frozen=True)
class _QueryRequestArgs:
    query: Any = None


def _filter_graph_by_file(graph: Any, source_file: str) -> dict[str, Any]:
    """Return a to_json_dict-style dict scoped to rungs from *source_file*."""
    import os

    source_norm = os.path.normpath(source_file).lower()

    # Select rung nodes whose source_file matches, tracking old→new index map
    filtered_nodes = []
    old_to_new: dict[int, int] = {}
    for old_idx, node in enumerate(graph.rung_nodes):
        if node.source_file and os.path.normpath(node.source_file).lower() == source_norm:
            old_to_new[old_idx] = len(filtered_nodes)
            filtered_nodes.append(node)

    # Collect tags touched by those rungs
    touched_tags: set[str] = set()
    for node in filtered_nodes:
        touched_tags |= node.condition_reads | node.data_reads | node.writes

    # Build collapse map from block_ranges (only ranges whose members overlap touched_tags)
    collapse: dict[str, str] = {}
    relevant_ranges: dict[str, list[str]] = {}
    for label, members in graph.block_ranges.items():
        overlap = [m for m in members if m in touched_tags]
        if len(overlap) >= 3:
            relevant_ranges[label] = members
            for m in members:
                if m in touched_tags:
                    collapse[m] = label

    collapsed_members = set(collapse.keys())

    # Build edges with collapsing, deduplicating
    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for new_idx, node in enumerate(filtered_nodes):
        rung_id = f"rung:{new_idx}"
        for tag_name in sorted(node.condition_reads):
            if tag_name not in touched_tags:
                continue
            src = collapse.get(tag_name, tag_name)
            key = (src, rung_id, "condition")
            if key not in seen_edges:
                seen_edges.add(key)
                edges.append({"source": src, "target": rung_id, "type": "condition"})
        for tag_name in sorted(node.data_reads):
            if tag_name not in touched_tags:
                continue
            src = collapse.get(tag_name, tag_name)
            key = (src, rung_id, "data")
            if key not in seen_edges:
                seen_edges.add(key)
                edges.append({"source": src, "target": rung_id, "type": "data"})
        for tag_name in sorted(node.writes):
            if tag_name not in touched_tags:
                continue
            tgt = collapse.get(tag_name, tag_name)
            key = (rung_id, tgt, "write")
            if key not in seen_edges:
                seen_edges.add(key)
                edges.append({"source": rung_id, "target": tgt, "type": "write"})

    # Build filtered readers/writers with new indices, collapsed
    readers_of: dict[str, list[int]] = {}
    writers_of: dict[str, list[int]] = {}
    for tag in sorted(touched_tags):
        key = collapse.get(tag, tag)
        r = [old_to_new[i] for i in graph.readers_of.get(tag, frozenset()) if i in old_to_new]
        w = [old_to_new[i] for i in graph.writers_of.get(tag, frozenset()) if i in old_to_new]
        if r:
            merged = readers_of.get(key, [])
            merged.extend(r)
            readers_of[key] = merged
        if w:
            merged = writers_of.get(key, [])
            merged.extend(w)
            writers_of[key] = merged
    readers_of = {k: sorted(set(v)) for k, v in readers_of.items()}
    writers_of = {k: sorted(set(v)) for k, v in writers_of.items()}

    # Build tag roles: non-collapsed + range labels
    tag_roles: dict[str, str] = {}
    for name, role in sorted(graph.tag_roles.items()):
        if name in touched_tags and name not in collapsed_members:
            tag_roles[name] = role.value
    for label in sorted(relevant_ranges):
        tag_roles[label] = "pivot"

    visible_tags = sorted((touched_tags - collapsed_members) | set(relevant_ranges.keys()))

    return {
        "rungNodes": [
            {
                "rungIndex": node.rung_index,
                "scope": node.scope,
                "subroutine": node.subroutine,
                "branchPath": list(node.branch_path),
                "conditionReads": sorted(node.condition_reads),
                "dataReads": sorted(node.data_reads),
                "writes": sorted(node.writes),
                "calls": list(node.calls),
                "sourceFile": node.source_file,
                "sourceLine": node.source_line,
            }
            for node in filtered_nodes
        ],
        "tagRoles": tag_roles,
        "tags": visible_tags,
        "readersOf": readers_of,
        "writersOf": writers_of,
        "graphEdges": edges,
        "blockRanges": {label: members for label, members in sorted(relevant_ranges.items())},
        "sourceFilter": source_file,
    }


def on_pyrung_graph(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    """Return the program graph for visualization, optionally scoped to a file."""
    parsed = adapter._parse_request_args(_GraphRequestArgs, args)

    with adapter._state_lock:
        runner = adapter._require_runner_locked()
    graph = runner.program.dataview()._graph

    if isinstance(parsed.sourceFile, str) and parsed.sourceFile:
        return _filter_graph_by_file(graph, parsed.sourceFile), []

    return graph.to_json_dict(), []


def on_pyrung_slice(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    """Return an upstream or downstream slice subgraph."""
    parsed = adapter._parse_request_args(_SliceRequestArgs, args)

    if not isinstance(parsed.tag, str) or not parsed.tag:
        raise adapter.DAPAdapterError("pyrungSlice.tag must be a non-empty string")
    if parsed.direction not in ("upstream", "downstream"):
        raise adapter.DAPAdapterError("pyrungSlice.direction must be 'upstream' or 'downstream'")

    with adapter._state_lock:
        runner = adapter._require_runner_locked()

    graph = runner.program.dataview()._graph
    tag_name = parsed.tag

    if parsed.direction == "upstream":
        slice_tags = graph.upstream_slice(tag_name)
    else:
        slice_tags = graph.downstream_slice(tag_name)

    # Include the queried tag itself in the result set
    all_tags = slice_tags | {tag_name}

    # Filter graph edges to those within the slice
    all_edges = graph.graph_edges()
    slice_edges = []
    for edge in all_edges:
        src = edge["source"]
        tgt = edge["target"]
        # Rung IDs are "rung:<index>" — include edge if both endpoints
        # resolve to tags in the slice or to rungs connecting slice tags.
        src_tag = src if not src.startswith("rung:") else None
        tgt_tag = tgt if not tgt.startswith("rung:") else None
        if src_tag and src_tag in all_tags and tgt_tag and tgt_tag in all_tags:
            slice_edges.append(edge)
            continue
        if src_tag and src_tag in all_tags and tgt_tag is None:
            # tag→rung: include if rung writes to a tag in the slice
            rung_idx = int(tgt.split(":")[1])
            rung_node = graph.rung_nodes[rung_idx]
            if rung_node.writes & all_tags:
                slice_edges.append(edge)
            continue
        if tgt_tag and tgt_tag in all_tags and src_tag is None:
            # rung→tag: include if rung reads from a tag in the slice
            rung_idx = int(src.split(":")[1])
            rung_node = graph.rung_nodes[rung_idx]
            if (rung_node.condition_reads | rung_node.data_reads) & all_tags:
                slice_edges.append(edge)

    return {"tags": sorted(all_tags), "edges": slice_edges}, []


def on_pyrung_query(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    """Execute a DataView query and return matching tags with roles."""
    parsed = adapter._parse_request_args(_QueryRequestArgs, args)

    if not isinstance(parsed.query, str) or not parsed.query.strip():
        raise adapter.DAPAdapterError("pyrungQuery.query must be a non-empty string")

    with adapter._state_lock:
        runner = adapter._require_runner_locked()

    view = runner.program.dataview()
    result = _parse_query(parsed.query.strip(), view)
    roles = result.roles()

    return {
        "tags": sorted(result.tags),
        "roles": {name: role.value for name, role in sorted(roles.items())},
    }, []
