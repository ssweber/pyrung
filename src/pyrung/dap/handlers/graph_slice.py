"""Program graph and slice request handling for the DAP adapter.

Owns pyrungGraph and pyrungSlice custom requests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

HandlerResult = tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]


@dataclass(frozen=True)
class _SliceRequestArgs:
    tag: Any = None
    direction: Any = None


def on_pyrung_graph(adapter: Any, _args: dict[str, Any]) -> HandlerResult:
    """Return the full program graph for visualization."""
    with adapter._state_lock:
        runner = adapter._require_runner_locked()
    graph = runner.program.dataview()._graph
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
