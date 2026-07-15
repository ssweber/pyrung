"""Capture _periods_to_crossing inputs on the first few k=None hits."""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
logic = importlib.import_module("tests.fixtures.tumbler").logic

from pyrung import PLC  # noqa: E402
from pyrung.core.analysis.pilot import cyclefold, pilot_events  # noqa: E402
from pyrung.core.runner import _compile_avoid  # noqa: E402

orig = cyclefold._periods_to_crossing
hits = 0


def probe(cyc, state, fold_ctx, extra_comparisons=None):
    global hits
    k = orig(cyc, state, fold_ctx, extra_comparisons)
    if k is None and hits < 6:
        hits += 1
        from pyrung.core.fold import _resolve_num

        sources = {s.acc_name: s for s in fold_ctx.sources}
        for tag, d in cyc.monotone.items():
            src = sources.get(tag)
            cur = state.tags.get(tag)
            preset = _resolve_num(src.preset, state) if src is not None else None
            cmps = fold_ctx.comparisons.get(tag, ())
            extra = (extra_comparisons or {}).get(tag, ())
            print(
                f"k=None hit{hits}: tag={tag} kind={getattr(src, 'kind', None)} "
                f"d={d} cur={cur} preset={preset} cmps={cmps} extra={extra}",
                flush=True,
            )
    return k


cyclefold._periods_to_crossing = probe

plc = PLC(logic)
plc.step()
tags = plc._known_tags_by_name
avoid_pred = _compile_avoid(tags["Cmd_State_Complete"])
deadline = time.monotonic() + 120
for event in pilot_events(plc, tags["Sts_StateCurrent"] == 17, max_scans=40_000, avoid_pred=avoid_pred):
    if event.kind == "finished" or time.monotonic() > deadline or hits >= 6:
        break
