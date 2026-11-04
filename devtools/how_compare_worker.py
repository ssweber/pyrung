"""One side of devtools/how_compare.py: run the program set against one pyrung tree.

Not run by hand.  The driver starts it as

    python -I devtools/how_compare_worker.py --tree <dir with src/> --out <json> [--full]
        [--only SUBSTRING] [--wall S] [--mem-mb N] [--step-cap N]

The tree's src is put first on sys.path and pyrung.__file__ is printed and checked against it,
so a row can never come from the wrong code.  Rows are appended to --out as they finish, and a
worker that is restarted skips the rows already there.  A watchdog thread ends the process (exit
97 memory, 98 time) after writing the row it was on as ABORT, and the driver restarts it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument("--tree", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--full", action="store_true")
ap.add_argument("--only", default="")
ap.add_argument("--wall", type=float, default=20.0, help="how() wall_clock_s per row")
ap.add_argument("--mem-mb", type=int, default=3000, help="process memory limit per worker")
ap.add_argument(
    "--step-cap", type=int, default=30000, help="most scans stepped for the independent check"
)
ap.add_argument("--list", action="store_true")
args = ap.parse_args()

TREE_SRC = os.path.join(os.path.abspath(args.tree), "src")
sys.path.insert(0, TREE_SRC)
sys.path.insert(1, HERE)

import pyrung  # noqa: E402

ORIGIN = os.path.abspath(pyrung.__file__)
print("pyrung.__file__", ORIGIN, flush=True)
if (
    os.path.normcase(os.path.realpath(ORIGIN)).startswith(
        os.path.normcase(os.path.realpath(TREE_SRC))
    )
    is False
):
    print(f"WRONG TREE: wanted {TREE_SRC}", flush=True)
    sys.exit(2)

import how_compare_programs as programs  # noqa: E402

SELECTED = [
    c for c in programs.CASES if (args.full or c.fast) and (not args.only or args.only in c.name)
]
if args.list:
    for c in SELECTED:
        print(c.group, c.name)
    sys.exit(0)


# ---------------------------------------------------------------- bounds


def rss_mb() -> float:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        c = Counters()
        c.cb = ctypes.sizeof(c)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        ]
        if psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
            return c.PagefileUsage / 1048576
        return 0.0
    try:
        with open("/proc/self/statm") as fh:
            return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1048576
    except OSError:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / (1048576 if sys.platform == "darwin" else 1024)


DOC: dict[str, Any] = {"origin": ORIGIN, "rows": []}
LOCK = threading.Lock()
CURRENT: dict[str, Any] = {"name": None, "started": 0.0, "deadline": 0.0, "peak_mb": 0.0}


def save():
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(DOC, fh, indent=0)
    os.replace(tmp, args.out)


def watchdog():
    while True:
        time.sleep(0.4)
        name = CURRENT["name"]
        if name is None:
            continue
        mb = rss_mb()
        CURRENT["peak_mb"] = max(CURRENT["peak_mb"], mb)
        why = None
        if mb > args.mem_mb:
            why, code = f"memory {mb:.0f} MB over {args.mem_mb}", 97
        elif time.perf_counter() > CURRENT["deadline"]:
            why, code = f"time over {CURRENT['deadline'] - CURRENT['started']:.0f} s", 98
        if why:
            with LOCK:
                if CURRENT["name"] == name:
                    DOC["rows"].append(
                        {
                            "case": name,
                            "status": "ABORT",
                            "reason": why,
                            "secs": round(time.perf_counter() - CURRENT["started"], 1),
                        }
                    )
                    save()
            print(f"ABORT {name}: {why}", flush=True)
            os._exit(code)


# ---------------------------------------------------------------- one row


def digest(items) -> str:
    return hashlib.sha1(repr(items).encode()).hexdigest()[:10]


def final_hash(tags) -> str:
    return digest(sorted((k, repr(v)) for k, v in tags.items() if not k.startswith("rtc.")))


def safe_goal(goal, tags):
    try:
        return bool(goal(tags))
    except Exception:  # noqa: BLE001
        return False


def stepped_replay_final(fork):
    """The fork's recording stepped scan by scan from its anchor with no loop and no checkpoint."""
    try:
        replay, log, anchor, lifecycle = fork._build_replay_fork(None)
        for scan in range(anchor + 1, fork.state.scan_id + 1):
            fork._apply_log_entries_for_scan(replay, scan, log, lifecycle)
            replay.step()
        return final_hash(dict(replay.state.tags))
    except Exception as error:  # noqa: BLE001
        return f"n/a:{type(error).__name__}"


def session_step_reach(plc, goal, cap):
    """Step a fork of the session itself, no inputs, and say when the goal first holds."""
    try:
        twin = plc.fork()
        start = twin.state.scan_id
        if safe_goal(goal, dict(twin.state.tags)):
            return start, start
        for _ in range(cap):
            twin.step()
            if safe_goal(goal, dict(twin.state.tags)):
                return start, twin.state.scan_id
        return start, None
    except Exception as error:  # noqa: BLE001
        return None, f"n/a:{type(error).__name__}"


def run_row(case):
    row = {"case": case.name, "group": case.group}
    began = time.perf_counter()
    try:
        spec = case.build()
    except Exception as error:  # noqa: BLE001
        row.update(status="SETUP_ERROR", reason=f"{type(error).__name__}: {str(error)[:160]}")
        return row
    plc, targets, goal = spec["plc"], spec["targets"], spec.get("goal")
    avoid = spec.get("avoid")
    kwargs = dict(spec.get("kwargs") or {})
    wall = case.wall or args.wall
    kwargs.setdefault("wall_clock_s", wall)
    if avoid is not None:
        kwargs["avoid"] = avoid
    row["session_scan"] = plc.state.scan_id

    plan = None
    try:
        plan = plc.how(*targets, **kwargs)
    except Exception as error:  # noqa: BLE001
        row.update(status=f"RAISE:{type(error).__name__}", reason=str(error)[:200])
        cause = error.__cause__
        if cause is not None:
            row["cause"] = f"{type(cause).__name__}: {str(cause)[:80]}"
    row["how_secs"] = round(time.perf_counter() - began, 2)

    reached = False
    if plan is not None:
        status = plan.status.name
        inputs = (len(plan.changes) if plan.fork is not None else 0) + sum(
            len(step.inputs) for step in plan.journal
        )
        holds = sum(len(step.rungs) for step in plan.journal)
        row.update(
            status=status,
            total=plan.total_scans,
            holds=holds,
            inputs=inputs,
            reason=(str(plan.reason)[:240] if plan.reason else None),
            plan_hash=digest([str(j) for j in plan.journal]),
        )
        if plan.fork is not None and status == "REACHED":
            reached = True
            fork = plan.fork
            row["tip"] = fork.state.scan_id
            row["final"] = final_hash(dict(fork.state.tags))
            try:
                row["replay_equal"] = dict(plan.replay().state.tags) == dict(fork.state.tags)
            except Exception as error:  # noqa: BLE001
                row["replay_equal"] = f"n/a:{type(error).__name__}"
            row["stepped_final"] = stepped_replay_final(fork)
            row["stepped_equal"] = row["stepped_final"] == row["final"]
            if goal is not None:
                row["goal_at_tip"] = safe_goal(goal, dict(fork.state.tags))

    # Independent of the plan: does stepping the session itself reach the goal, and when?
    if goal is not None:
        cap = args.step_cap
        if reached:
            cap = min(cap, max(2 * row["total"] + 200, 400))
        beyond = reached and row["tip"] - row["session_scan"] > cap
        start, hit = (None, "skipped") if beyond else session_step_reach(plc, goal, cap)
        row["step_cap"] = cap
        row["step_reach"] = None if beyond else hit
        if isinstance(hit, str) and not beyond:
            row["step_note"] = hit
        flags = []
        # A plan with no input and no hold must be what the session does by itself.  When the
        # plan is longer than the stepping cap, there is nothing to compare it with.
        quiet = reached and row.get("inputs") == 0 and row.get("holds") == 0
        if beyond:
            row["step_note"] = (
                f"plan is {row['tip'] - row['session_scan']} scans, beyond the {cap}-scan stepping cap"
            )
            quiet = False
        if reached and quiet and isinstance(hit, int) and hit != row["tip"]:
            flags.append(f"PLAN_VS_STEP plan ends scan {row['tip']}, stepping reaches {hit}")
        elif reached and quiet and hit is None:
            flags.append(
                f"PLAN_VS_STEP plan ends scan {row['tip']}, stepping never reaches in {cap} scans"
            )
        if reached and row.get("goal_at_tip") is False:
            flags.append("GOAL_FALSE_AT_TIP the plan's fork does not satisfy the goal")
        if reached and row.get("replay_equal") is False:
            flags.append("REPLAY_DIFFERS")
        if reached and row.get("stepped_equal") is False:
            flags.append("STEPPED_DIFFERS")
        if (
            plan is not None
            and not reached
            and isinstance(hit, int)
            and row["status"] in ("STOPPED", "CANNOT_REACH")
        ):
            if row.get("inputs", 0) == 0:
                flags.append(f"MISSED stepping reaches at scan {hit}, how() says {row['status']}")
        row["flags"] = flags
    row["secs"] = round(time.perf_counter() - began, 2)
    return row


def main():
    done = set()
    if os.path.exists(args.out):
        try:
            with open(args.out, encoding="utf-8") as fh:
                prior = json.load(fh)
            if prior.get("origin") == ORIGIN:
                DOC["rows"] = prior["rows"]
                done = {r["case"] for r in prior["rows"]}
        except (OSError, ValueError):
            pass
    save()
    threading.Thread(target=watchdog, daemon=True).start()
    todo = [c for c in SELECTED if c.name not in done]
    print(f"{len(todo)} of {len(SELECTED)} cases to run", flush=True)
    for case in todo:
        wall = case.wall or args.wall
        CURRENT.update(
            name=case.name,
            started=time.perf_counter(),
            deadline=time.perf_counter() + 3 * wall + 40,
            peak_mb=0.0,
        )
        row = run_row(case)
        row["peak_mb"] = round(max(CURRENT["peak_mb"], rss_mb()))
        with LOCK:
            CURRENT["name"] = None
            DOC["rows"].append(row)
            save()
        flag = f" FLAG {row['flags']}" if row.get("flags") else ""
        print(
            f"{case.name:<48} {row.get('status')!s:<14} total={row.get('total')} holds={row.get('holds')} "
            f"step={row.get('step_reach')} {row.get('secs')}s{flag}",
            flush=True,
        )
    print("done", len(DOC["rows"]), flush=True)


main()
