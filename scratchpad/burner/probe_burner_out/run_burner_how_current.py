from __future__ import annotations

from collections import Counter
import sys
import time
from pathlib import Path


CLICK_PROJECT = Path(r"C:\Users\Sam\AppData\Local\Temp\CLICK (00010A66)\pyrung_project")
sys.path.insert(0, str(CLICK_PROJECT))

from pyrung import PLC  # noqa: E402
from main import logic  # noqa: E402
from tags import y_BurnerLoop  # noqa: E402


def main() -> None:
    print(f"CLICK_PROJECT={CLICK_PROJECT}", flush=True)
    print("RUN=PLC(logic).how(y_BurnerLoop, walk_seconds=120, debug=True)", flush=True)
    t0 = time.monotonic()
    path = PLC(logic).how(y_BurnerLoop, walk_seconds=120, debug=True)
    elapsed = time.monotonic() - t0
    print(f"RESULT elapsed={elapsed:.3f}s reachable={path.reachable}", flush=True)
    print(f"RESULT reason={getattr(path, 'reason', None)!r}", flush=True)
    diagnosis = getattr(path, "diagnosis", None)
    if diagnosis is not None:
        print(f"RESULT diagnosis_reason={getattr(diagnosis, 'reason', None)!r}", flush=True)
        print(f"RESULT failing_goal={getattr(diagnosis, 'failing_goal', None)!r}", flush=True)
        print(f"RESULT failure_kind={getattr(diagnosis, 'failure_kind', None)!r}", flush=True)
        print(f"RESULT diagnosis_notes={getattr(diagnosis, 'notes', ())!r}", flush=True)
    trace = getattr(path, "debug_trace", None)
    if trace is not None:
        events = getattr(trace, "events", ())
        counts = Counter(event.kind for event in events)
        debug_diag = getattr(trace, "diag", None)
        if debug_diag is not None:
            committed_values = getattr(debug_diag, "committed_values", {})
            recovery_snapshots = getattr(debug_diag, "recovery_snapshots", ())
            print(f"DEBUG_DIAG committed_values={len(committed_values)}", flush=True)
            print(f"DEBUG_DIAG recovery_snapshots={len(recovery_snapshots)}", flush=True)
        print(f"DEBUG_EVENTS counts={dict(sorted(counts.items()))}", flush=True)
        regressions = [event for event in events if event.kind == "progress-regression"]
        if regressions:
            first = regressions[0]
            print(
                "DEBUG_EVENTS first_progress_regression="
                f"{first.tag}={first.value!r} detail={first.detail!r}",
                flush=True,
            )
        dead_ends = [event for event in events if event.kind == "dead-end-snapshot"]
        if dead_ends:
            print(f"DEBUG_EVENTS final_dead_end={dead_ends[-1].detail!r}", flush=True)
        out = Path(__file__).with_name("burner_how_current_debug_trace.txt")
        out.write_text(str(trace), encoding="utf-8")
        print(f"DEBUG_TRACE={out}", flush=True)


if __name__ == "__main__":
    main()
