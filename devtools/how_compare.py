"""how() old-versus-new: the same programs on a base ref and on a tree, a table of what differs.

    .venv/Scripts/python.exe devtools/how_compare.py --base dev --tree ../state-slots
    python -I devtools/mergeq/mergeq.py run --timeout 1500 -- \\
        .venv/Scripts/python.exe devtools/how_compare.py --base dev --tree ../state-slots

--base   a git ref (its src/ is exported with git archive, cached by sha) or a directory with src/
--tree   a directory with src/ (a worktree, or the merged tree of a train)
--full   the long set (256 more rows; not timed end to end); the default is the fast set
--only S run only cases whose name contains S
--expected FILE
         differences you expect, one per line: `<glob on the case name>  # reason`.  A difference
         under a glob is shown with its reason and does not fail the run.

Each side runs in its own worker process (devtools/how_compare_worker.py) under
`mergeq.py run --timeout`, serially, at below-normal priority, with a memory limit and a
per-row time limit; the program set is devtools/how_compare_programs.py.  The worker prints
pyrung.__file__ and refuses to run if it is not under the tree given, and the table opens with
both paths.

What is reported:
  * rows that differ between the two sides (status, total, holds, inputs, reason, plan hash,
    final state, replay and stepped equality, and when stepping the session itself reaches);
  * how many rows REACHED on each side, and which rows reach on neither side (agreement on
    those is no evidence);
  * FLAGS, whether or not the sides agree: a plan with no inputs and no holds that disagrees with
    stepping the session itself (the check that would have caught a plan of 1 scan against
    stepping's 17), a REACHED plan whose fork does not satisfy the goal, a replay or stepped
    replay that differs from the fork, a STOPPED answer where stepping reaches.

Exit 0 when every difference is expected and the tree has no flag the base lacks; 1 otherwise;
2 on a setup problem (bad ref, wrong tree, a worker that died).
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import NoReturn

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
MERGEQ = os.path.join(HERE, "mergeq", "mergeq.py")
WORKER = os.path.join(HERE, "how_compare_worker.py")

FIELDS = (
    "status",
    "total",
    "holds",
    "inputs",
    "reason",
    "plan_hash",
    "tip",
    "final",
    "replay_equal",
    "stepped_equal",
    "goal_at_tip",
    "step_reach",
    "cause",
    "flags",
)


def die(message: str, code: int = 2) -> NoReturn:
    print(f"how_compare: {message}", file=sys.stderr)
    sys.exit(code)


def export_ref(ref: str, cache: str) -> str:
    sha = subprocess.run(
        ["git", "-C", REPO, "rev-parse", "--verify", ref + "^{commit}"],
        capture_output=True,
        text=True,
    )
    if sha.returncode:
        die(f"{ref!r} is neither a directory nor a git ref: {sha.stderr.strip()}")
    sha = sha.stdout.strip()
    dest = os.path.join(cache, sha)
    if os.path.isdir(os.path.join(dest, "src", "pyrung")):
        return dest
    archive = subprocess.run(
        ["git", "-C", REPO, "archive", "--format=tar", sha, "src"], capture_output=True
    )
    if archive.returncode:
        die(f"git archive {ref}: {archive.stderr.decode(errors='replace').strip()}")
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(dest, filter="data")
    return dest


def resolve(spec: str, cache: str) -> tuple[str, str]:
    if os.path.isdir(spec):
        path = os.path.abspath(spec)
        if not os.path.isdir(os.path.join(path, "src", "pyrung")):
            die(f"{path} has no src/pyrung")
        head = subprocess.run(
            ["git", "-C", path, "rev-parse", "--short", "HEAD"], capture_output=True, text=True
        )
        return path, f"{path} @ {head.stdout.strip() or '?'}"
    path = export_ref(spec, cache)
    return path, f"{spec} @ {os.path.basename(path)[:9]}"


def run_side(label: str, tree: str, out: str, opts) -> int:
    """Run the worker until every row is there; returns the number of restarts."""
    if os.path.exists(out):
        os.remove(out)
    deadline = time.monotonic() + opts.timeout
    restarts = 0
    while True:
        left = int(deadline - time.monotonic())
        if left < 30:
            print(f"[{label}] out of time", flush=True)
            return restarts
        cmd = [
            sys.executable,
            "-I",
            WORKER,
            "--tree",
            tree,
            "--out",
            out,
            "--wall",
            str(opts.wall),
            "--mem-mb",
            str(opts.mem_mb),
        ]
        if opts.full:
            cmd.append("--full")
        if opts.only:
            cmd += ["--only", opts.only]
        if opts.no_mergeq:
            wrapped, timeout = cmd, left
        else:
            wrapped = [
                sys.executable,
                "-I",
                MERGEQ,
                "run",
                "--timeout",
                str(left),
                "--name",
                f"how-compare-{label}",
                "--",
            ] + cmd
            timeout = left + 60
        print(f"[{label}] worker start ({left} s left)", flush=True)
        try:
            rc = subprocess.run(wrapped, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            rc = 124
        if rc == 0:
            return restarts
        if rc in (97, 98):
            restarts += 1
            continue
        if rc == 124:
            print(f"[{label}] time limit reached; rows after the last one are missing", flush=True)
            return restarts
        die(f"worker for {label} exited {rc}")


def load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as error:
        die(f"no results at {path}: {error}")


def short(value, width=70):
    text = repr(value) if not isinstance(value, str) else value
    return text if len(text) <= width else text[: width - 3] + "..."


def really_reached(row: dict) -> bool:
    return (
        row.get("status") == "REACHED"
        and row.get("goal_at_tip") is not False
        and row.get("replay_equal") is not False
        and row.get("stepped_equal") is not False
    )


def load_expected(path: str | None) -> list[tuple[str, str]]:
    if not path:
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            glob, _, why = line.partition("#")
            out.append((glob.strip(), why.strip() or "no reason recorded"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--base", required=True)
    ap.add_argument("--tree", required=True)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--expected")
    ap.add_argument("--out", help="directory for the two result files (default: a temp dir)")
    ap.add_argument(
        "--timeout", type=int, default=0, help="seconds per side (default 600 fast, 3600 full)"
    )
    ap.add_argument(
        "--wall", type=float, default=0, help="how() wall clock per row (default 20 fast, 60 full)"
    )
    ap.add_argument("--mem-mb", type=int, default=3000)
    ap.add_argument(
        "--no-mergeq", action="store_true", help="skip the mergeq wrapper (bounds still apply)"
    )
    opts = ap.parse_args()
    opts.timeout = opts.timeout or (3600 if opts.full else 600)
    opts.wall = opts.wall or (60 if opts.full else 20)

    began = time.monotonic()
    cache = os.path.join(tempfile.gettempdir(), "pyrung-how-compare")
    os.makedirs(cache, exist_ok=True)
    base_tree, base_label = resolve(opts.base, cache)
    new_tree, new_label = resolve(opts.tree, cache)
    outdir = opts.out or tempfile.mkdtemp(prefix="how-compare-")
    os.makedirs(outdir, exist_ok=True)
    base_out, new_out = os.path.join(outdir, "base.json"), os.path.join(outdir, "tree.json")

    run_side("base", base_tree, base_out, opts)
    run_side("tree", new_tree, new_out, opts)
    base, new = load(base_out), load(new_out)
    elapsed = time.monotonic() - began

    for label, doc, tree in (("base", base, base_tree), ("tree", new, new_tree)):
        origin = os.path.normcase(os.path.realpath(doc["origin"]))
        if not origin.startswith(os.path.normcase(os.path.realpath(os.path.join(tree, "src")))):
            die(f"{label} rows came from {doc['origin']}, not {tree}")

    b = {r["case"]: r for r in base["rows"]}
    n = {r["case"]: r for r in new["rows"]}
    names = [r["case"] for r in new["rows"]] + [c for c in b if c not in n]
    expected = load_expected(opts.expected)

    print()
    print(f"base  {base_label}")
    print(f"      pyrung.__file__ = {base['origin']}")
    print(f"tree  {new_label}")
    print(f"      pyrung.__file__ = {new['origin']}")
    print(
        f"programs: {'full' if opts.full else 'fast'} set, {len(names)} rows; elapsed {elapsed:.0f} s"
    )

    # reach
    reach_b = [c for c in names if c in b and b[c].get("status") == "REACHED"]
    reach_n = [c for c in names if c in n and n[c].get("status") == "REACHED"]
    real_b = [c for c in reach_b if really_reached(b[c])]
    real_n = [c for c in reach_n if really_reached(n[c])]
    neither = [c for c in names if c not in reach_b and c not in reach_n]
    print()
    print(f"REACHED: base {len(reach_b)} of {len(b)} rows, tree {len(reach_n)} of {len(n)} rows")
    print(
        f"REACHED and the goal, replay and stepped replay all check out: base {len(real_b)}, tree {len(real_n)}"
    )
    print(f"reach on neither side ({len(neither)}): agreement on these is not evidence")
    for c in neither:
        sb = b.get(c, {}).get("status")
        sn = n.get(c, {}).get("status")
        print(f"    {c}  [{sb} / {sn}]")

    # differences
    diffs = []
    unexplained = 0
    for c in names:
        rb, rn = b.get(c), n.get(c)
        if rb is None or rn is None:
            diffs.append(
                (
                    c,
                    "row",
                    "missing" if rb is None else "present",
                    "missing" if rn is None else "present",
                )
            )
            continue
        for field in FIELDS:
            if rb.get(field) != rn.get(field):
                diffs.append((c, field, rb.get(field), rn.get(field)))
    print()
    print(f"DIFFERENCES: {len({d[0] for d in diffs})} rows of {len(names)}, {len(diffs)} fields")
    if diffs:
        print("| case | field | base | tree | expected |")
        print("|---|---|---|---|---|")
    for c, field, vb, vn in diffs:
        why = next((w for g, w in expected if fnmatch.fnmatch(c, g)), None)
        if why is None:
            unexplained += 1
        print(f"| {c} | {field} | {short(vb)} | {short(vn)} | {why or 'NO'} |")

    # flags
    flagged = [c for c in names if (b.get(c, {}).get("flags") or n.get(c, {}).get("flags"))]
    new_flags = 0
    print()
    print(
        f"FLAGS (a plan against stepping the session, regardless of agreement): {len(flagged)} rows"
    )
    for c in flagged:
        fb = b.get(c, {}).get("flags") or []
        fn = n.get(c, {}).get("flags") or []
        only_new = [f for f in fn if f not in fb]
        new_flags += bool(only_new)
        print(f"  {c}")
        for f in fb:
            print(f"      base: {f}")
        for f in fn:
            print(f"      tree: {f}{'   <-- new' if f in only_new else ''}")

    # aborts, setup errors, timing
    bad = [
        c
        for c in names
        if n.get(c, {}).get("status") in ("ABORT", "SETUP_ERROR")
        or b.get(c, {}).get("status") in ("ABORT", "SETUP_ERROR")
    ]
    if bad:
        print()
        print("ABORT / SETUP_ERROR rows")
        for c in bad:
            print(
                f"  {c}  base: {b.get(c, {}).get('status')} {b.get(c, {}).get('reason')}  | tree: {n.get(c, {}).get('status')} {n.get(c, {}).get('reason')}"
            )
    slow = [
        (c, b[c].get("secs", 0), n[c].get("secs", 0))
        for c in names
        if c in b
        and c in n
        and n[c].get("secs", 0) > 2
        and n[c].get("secs", 0) > 2.5 * max(b[c].get("secs", 0), 0.2)
    ]
    if slow:
        print()
        print("SLOWER on the tree (more than 2.5x and over 2 s)")
        for c, sb, sn in slow:
            print(f"  {c}: base {sb} s, tree {sn} s")
    peak = max([r.get("peak_mb", 0) for r in list(b.values()) + list(n.values())] or [0])
    print()
    print(
        f"peak memory of any row: {peak} MB; seconds, base {sum(r.get('secs', 0) for r in b.values()):.0f}, tree {sum(r.get('secs', 0) for r in n.values()):.0f}"
    )
    print(f"result files: {outdir}")
    if unexplained or new_flags or bad:
        print(
            f"HOLD: {unexplained} unexplained difference field(s), {new_flags} row(s) with a new flag, {len(bad)} abort/setup row(s)"
        )
        return 1
    print("OK: no unexplained difference, no new flag")
    return 0


if __name__ == "__main__":
    sys.exit(main())
