"""Instrument the cofire/partition machinery on a 3-timer program.

Settles whether proper-subset simultaneity groups (e.g. {A,B} while C is
pending) ever form, or whether the partition only ever produces
all-singletons / the full set.
"""

from collections import Counter as Tally

from pyrung.core import Bool, Program, Rung, Timer, on_delay, out
from pyrung.core.analysis.prove import Intractable, reachable_states
from pyrung.core.analysis.prove import events as events_mod

_part_orig = events_mod._partition_pending_sources
_cofire_orig = events_mod._advance_all_to_cofire

partition_calls = Tally()        # (n_sources, n_groups, max_group_size) -> count
multi_group_seen = []            # groups with >=2 sources, when >2 pending
cofire_calls = Tally()           # n_sources aligned -> count


def _part_spy(sources):
    groups = _part_orig(sources)
    n = len(sources)
    sizes = tuple(sorted(len(g.exact_sources) for g in groups))
    max_size = max(sizes) if sizes else 0
    partition_calls[(n, len(groups), max_size)] += 1
    if n >= 3 and any(2 <= len(g.exact_sources) < n for g in groups):
        multi_group_seen.append((n, sizes))
    return groups


def _cofire_spy(kernel, before_snap, all_sources):
    result = _cofire_orig(kernel, before_snap, all_sources)
    if result is not None:
        cofire_calls[len(result.firing_group.exact_sources)] += 1
    return result


events_mod._partition_pending_sources = _part_spy
events_mod._advance_all_to_cofire = _cofire_spy


def build():
    """Three unconditional self-resetting timers -- always co-pending,
    none can be individually parked."""
    A = Timer.clone("TmrA")
    B = Timer.clone("TmrB")
    C = Timer.clone("TmrC")
    Q = Bool("Q")
    with Program(strict=False) as logic:
        with Rung():
            on_delay(A, 230).reset(A.Done)
        with Rung():
            on_delay(B, 290).reset(B.Done)
        with Rung():
            on_delay(C, 370).reset(C.Done)
        with Rung(A.Done, B.Done):
            out(Q)
    return logic


def main():
    logic = build()
    states = reachable_states(
        logic, project=["Q", "TmrC_Done"], max_states=10_000, depth_budget=20
    )
    if isinstance(states, Intractable):
        print("INTRACTABLE")
        return

    print(f"reachable states: {len(states)}")
    target = frozenset({("Q", True), ("TmrC_Done", False)})
    print(f"target (Q=True, TmrC_Done=False) reachable: {target in states}")
    print()
    print("=== _partition_pending_sources calls ===")
    print("  (n_sources, n_groups, max_group_size) -> count")
    for key in sorted(partition_calls):
        print(f"  {key} -> {partition_calls[key]}")
    print()
    print(f"proper-subset multi-source groups (>=2 pending, group 2<=size<n): {len(multi_group_seen)}")
    for n, sizes in multi_group_seen[:20]:
        print(f"  n_pending={n} group_sizes={sizes}")
    print()
    print("=== _advance_all_to_cofire calls (n_sources aligned -> count) ===")
    for key in sorted(cofire_calls):
        print(f"  {key} -> {cofire_calls[key]}")


if __name__ == "__main__":
    main()
