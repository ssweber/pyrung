"""how() old-versus-new: the same programs on a base ref and on a tree, a table of what differs.

    python -I devtools/mergeq/mergeq.py run --timeout 2000 -- \
        uv run python devtools/how_compare.py --base dev --tree ../state-slots

The outer --timeout must exceed twice the per-side --timeout (default 900 s fast, 5400 s full)
plus a few minutes; with --full give the outer one 11500.

--base   a git ref (its src/ is exported with git archive, cached by sha) or a directory with src/
--tree   a directory with src/ (a worktree, or the merged tree of a train), or a ref
--full   the long set (378 cases instead of the fast set's 122)
--only S run only cases whose name contains S (no match is an error)
--expected FILE
         differences you expect, one per line: `<glob>[:field,...]  # reason`.  The glob is
         case-sensitive (fnmatchcase) on the case name.  No field list covers every field of a
         matching case except `flags` and `run`, which must be named; a row which went from
         REACHED to anything else (an answer, an error, ABORT) is covered only by a line that
         names `status` (and `run` or `flags` too, where those changed).  Fields: status total holds inputs reason plan_hash
         tip final replay_equal stepped_equal goal_at_tip step_reach cause, plus `run` (an ABORT
         or SETUP_ERROR row) and `flags` (a flag kind new to the tree).  A reason is required, a
         glob that matches no case is an error (printed as stale, exit 2), a missing file exit 2.
         A missing row on either side is never explainable.

Each side runs in its own worker process (devtools/how_compare_worker.py) under
`mergeq.py run --timeout`, serially, at below-normal priority, with a memory limit and a
per-row time limit; the program set is devtools/how_compare_programs.py.  The worker prints
pyrung.__file__ and refuses to run if it is not under the tree given, and the report opens with
both paths.

What is reported:
  * rows that differ between the two sides, field by field;
  * how many rows REACHED on each side, and which rows reach on neither side (agreement on
    those is no evidence); which reached plans were not checked against stepping, and why;
  * FLAGS, per row, by kind (not by text, so a shifted scan number is the same defect):
      PLAN_VS_STEP  a plan with no inputs and no holds that ends at a different scan than
                    stepping the session itself reaches the goal (the check that would have
                    caught a plan of 1 scan against stepping's 17)
      TOTAL_VS_TIP  plan.total_scans is not the fork's distance from the session
      GOAL_FALSE_AT_TIP, REPLAY_DIFFERS, STEPPED_DIFFERS, NO_FORK (REACHED without a fork)
      MISSED        stepping reaches by itself and how() said STOPPED or raised (no avoid)
    A flag kind NEW to the tree on a row holds the train.  A kind already on the same base row
    does not hold; it is listed every run under "already on base", with both sides' text.
  * how many REACHED rows on each side leaned on the last resort (one move run with no reason
    pointing to it), and every row where that move changed; reported, never held.

Exit 0 when every selected case has a row on both sides, every difference is explained, and the
tree has no new flag kind; 1 otherwise (including any ABORT or SETUP_ERROR row not named by a
`run` line); 2 on a setup problem (bad ref or --expected, no case selected, wrong tree, stale
expected line, a worker that died).
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import os
import shutil
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
)
# Names an expected line may give.  `run` is an ABORT or SETUP_ERROR row; `flags` a flag new to
# the tree; `row` a missing row (never explainable).
EXPECT_FIELDS = (*FIELDS, "run", "flags")
BAD_STATUS = ("ABORT", "SETUP_ERROR")


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
    if os.path.exists(os.path.join(dest, ".complete")):
        return dest
    archive = subprocess.run(
        ["git", "-C", REPO, "archive", "--format=tar", sha, "src"], capture_output=True
    )
    if archive.returncode:
        die(f"git archive {ref}: {archive.stderr.decode(errors='replace').strip()}")
    shutil.rmtree(dest, ignore_errors=True)  # a half-extracted earlier try
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(dest, filter="data")
    with open(os.path.join(dest, ".complete"), "w") as fh:  # last, so a crash never looks done
        fh.write(sha)
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
        if rc == 3:
            die(f"no case selected (--only {opts.only!r})")
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


def load_expected(path: str | None) -> list[tuple[str, tuple[str, ...] | None, str]]:
    """Lines of `<glob>[:field,...]  # reason`; blank lines and lines starting with # are skipped."""
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError as error:
        die(f"cannot read the expected file {path}: {error.strerror or error}")
    out = []
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        head, hash_, why = line.partition("#")
        why = why.strip()
        if not hash_ or not why:
            die(f"{path}:{number}: a reason after # is required: {line!r}")
        head = head.strip()
        glob, colon, names = head.rpartition(":")
        if not colon:
            glob, fields = head, None
        else:
            fields = tuple(x.strip() for x in names.split(",") if x.strip())
            unknown = [f for f in fields if f not in EXPECT_FIELDS]
            if not fields or unknown:
                die(
                    f"{path}:{number}: field names must be from {', '.join(EXPECT_FIELDS)}"
                    f" (got {names!r})"
                )
        if not glob:
            die(f"{path}:{number}: empty glob")
        out.append((glob.strip(), fields, why))
    return out


def run_kind(row: dict) -> str:
    """What happened to a row that did not produce an answer, without the varying numbers."""
    status = row.get("status")
    if status == "ABORT":
        return f"ABORT:{row.get('reason')}"
    return f"SETUP_ERROR:{str(row.get('reason'))[:80]}"


def is_budget(row: dict) -> bool:
    return row.get("step_reach") == "n/a:step budget"


def flag_kind(text: str) -> str:
    return text.split()[0]


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
        "--timeout", type=int, default=0, help="seconds per side (default 900 fast, 5400 full)"
    )
    ap.add_argument(
        "--wall", type=float, default=0, help="how() wall clock per row (default 20 fast, 60 full)"
    )
    ap.add_argument("--mem-mb", type=int, default=3000)
    ap.add_argument(
        "--no-mergeq", action="store_true", help="skip the mergeq wrapper (bounds still apply)"
    )
    opts = ap.parse_args()
    opts.timeout = opts.timeout or (5400 if opts.full else 900)
    opts.wall = opts.wall or (60 if opts.full else 20)

    expected = load_expected(opts.expected)  # fail before the long run, not after
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
    selected = base.get("selected") or []
    if not selected or sorted(selected) != sorted(new.get("selected") or []):
        die("the two sides did not select the same cases (or none): different program sets?")
    names = list(selected)

    print()
    print(f"base  {base_label}")
    print(f"      pyrung.__file__ = {base['origin']}")
    print(f"tree  {new_label}")
    print(f"      pyrung.__file__ = {new['origin']}")
    print(
        f"programs: {'full' if opts.full else 'fast'} set, {len(names)} cases; elapsed {elapsed:.0f} s"
    )

    # completeness: every selected case has a row on both sides
    missing = [
        (c, side) for c in names for side, rows in (("base", b), ("tree", n)) if c not in rows
    ]
    if missing:
        print()
        print(f"MISSING ROWS ({len(missing)}): the run is incomplete")
        for c, side in missing:
            print(f"  {c}  (no row on {side})")
    names_ok = [c for c in names if c in b and c in n]

    # reach
    reach_b = [c for c in names_ok if b[c].get("status") == "REACHED"]
    reach_n = [c for c in names_ok if n[c].get("status") == "REACHED"]
    real_b = [c for c in reach_b if really_reached(b[c])]
    real_n = [c for c in reach_n if really_reached(n[c])]
    neither = [c for c in names_ok if c not in reach_b and c not in reach_n]
    print()
    print(f"REACHED: base {len(reach_b)} of {len(b)} rows, tree {len(reach_n)} of {len(n)} rows")
    print(
        "REACHED and the goal, replay and stepped replay all check out: "
        f"base {len(real_b)}, tree {len(real_n)}"
    )
    print(f"reach on neither side ({len(neither)}): agreement on these is not evidence")
    for c in neither:
        print(f"    {c}  [{b[c].get('status')} / {n[c].get('status')}]")
    unchecked: dict[str, list[str]] = {}
    for side, rows in (("base", b), ("tree", n)):
        for c in names_ok:
            note = rows[c].get("step_check", "")
            if rows[c].get("status") == "REACHED" and note.startswith("unchecked"):
                unchecked.setdefault(f"{side} {note}", []).append(c)
    print()
    print("REACHED rows whose plan was NOT checked against stepping the session (and why):")
    for key, cases in unchecked.items():
        print(f"    {key}: {len(cases)} rows" + ("" if len(cases) > 6 else f" {cases}"))

    # differences, field by field
    diffs = []
    for c in names_ok:
        rb, rn = b[c], n[c]
        if rb.get("status") in BAD_STATUS or rn.get("status") in BAD_STATUS:
            vb = run_kind(rb) if rb.get("status") in BAD_STATUS else rb.get("status")
            vn = run_kind(rn) if rn.get("status") in BAD_STATUS else rn.get("status")
            if vb != vn:
                diffs.append((c, "run", vb, vn))
            continue
        for field in FIELDS:
            if field == "step_reach" and (is_budget(rb) or is_budget(rn)):
                continue  # one side stopped stepping at its time budget: no evidence either way
            if rb.get(field) != rn.get(field):
                diffs.append((c, field, rb.get(field), rn.get(field)))
    # flags: by kind and row, never by text
    kinds = {
        c: (
            {flag_kind(f) for f in b[c].get("flags", [])},
            {flag_kind(f) for f in n[c].get("flags", [])},
        )
        for c in names_ok
    }
    for c in names_ok:
        for kind in sorted(kinds[c][1] - kinds[c][0]):
            diffs.append((c, "flags", "-", kind))
    # a row that went from REACHED to anything else needs `status` named, not a wildcard
    regress = {
        c for c in names_ok if b[c].get("status") == "REACHED" and n[c].get("status") != "REACHED"
    }
    unexplained = 0
    print()
    print(f"ROWS THAT DIFFER ({len({d[0] for d in diffs})} of {len(names)}):")
    by_row: dict[str, list[tuple[str, object, object]]] = {}
    for c, field, vb, vn in diffs:
        by_row.setdefault(c, []).append((field, vb, vn))
    for c, items in by_row.items():
        shown = {f: (x, y) for f, x, y in items}
        head = []
        for f in ("status", "total", "run"):
            if f in shown:
                head.append(f"{f} {short(shown[f][0], 30)} -> {short(shown[f][1], 30)}")
        rest = [f for f, _, _ in items if f not in ("status", "total", "run")]
        print(f"  {c}: {'; '.join(head)}{'; ' if head and rest else ''}{', '.join(rest)}")
    print()
    print(f"DIFFERENCES: {len({d[0] for d in diffs})} rows of {len(names)}, {len(diffs)} fields")
    if diffs:
        print("| case | field | base | tree | expected |")
        print("|---|---|---|---|---|")
    for c, field, vb, vn in diffs:
        why = None
        for glob, fields, reason in expected:
            if not fnmatch.fnmatchcase(c, glob):
                continue
            if fields is None:
                # a bare line covers neither a regression nor `flags` / `run`
                covers = c not in regress and field not in ("flags", "run")
            elif c in regress:
                covers = "status" in fields and (field in fields or field not in ("flags", "run"))
            else:
                covers = field in fields
            if covers:
                why = reason
                break
        if why is None:
            unexplained += 1
        print(f"| {c} | {field} | {short(vb)} | {short(vn)} | {why or 'NO'} |")
    stale = [e for e in expected if not any(fnmatch.fnmatchcase(c, e[0]) for c in names)]
    if stale:
        print()
        print("STALE expected lines (the glob matches no row):")
        for glob, fields, reason in stale:
            print(f"  {glob}{':' + ','.join(fields) if fields else ''}  # {reason}")

    # flags in full
    print()
    already = [c for c in names_ok if kinds[c][0] & kinds[c][1]]
    new_flag_rows = [c for c in names_ok if kinds[c][1] - kinds[c][0]]
    print(f"FLAGS NEW TO THE TREE: {len(new_flag_rows)} rows (these hold the train)")
    for c in new_flag_rows:
        for f in n[c].get("flags", []):
            if flag_kind(f) in kinds[c][1] - kinds[c][0]:
                print(f"  {c}: {f}")
    print(
        f"FLAGS ALREADY ON BASE: {len(already)} rows (same kind on the same row; shown every run, "
        "do not hold)"
    )
    for c in already:
        for f in b[c].get("flags", []):
            print(f"  {c} base: {f}")
        for f in n[c].get("flags", []):
            print(f"  {c} tree: {f}")
    gone = [c for c in names_ok if kinds[c][0] - kinds[c][1]]
    if gone:
        print(f"FLAGS ON BASE ONLY (gone on the tree): {gone}")
    # the last resort: a REACHED that leaned on one move no reason pointed to.  Reported, never
    # held: a tree whose trace leads further shows here as fewer, one that leads less as more.
    print()
    lean_b = [c for c in reach_b if b[c].get("last_resort")]
    lean_n = [c for c in reach_n if n[c].get("last_resort")]
    print(
        f"REACHED through the last resort: base {len(lean_b)} of {len(reach_b)}, "
        f"tree {len(lean_n)} of {len(reach_n)} (reported, does not hold)"
    )
    for c in names_ok:
        vb, vn = b[c].get("last_resort"), n[c].get("last_resort")
        if vb != vn:
            print(f"    {c}: {vb or '-'} -> {vn or '-'}")
    budget = [c for c in names_ok if is_budget(b[c]) or is_budget(n[c])]
    if budget:
        print(f"stepping stopped by budget (a MISSED here could not be seen): {len(budget)} rows")
        for c in budget:
            sides = [s for s, r in (("base", b[c]), ("tree", n[c])) if is_budget(r)]
            print(f"    {c}  ({', '.join(sides)})")
    goalless = [c for c in names_ok if b[c].get("step_check") == "unchecked: no goal given"]
    if goalless:
        print(f"rows with no goal function, never checked against stepping: {len(goalless)}")
        for c in goalless:
            print(f"    {c}")

    # aborts, setup errors
    bad = [
        c for c in names_ok if n[c].get("status") in BAD_STATUS or b[c].get("status") in BAD_STATUS
    ]
    bad_unexplained = 0
    if bad:
        print()
        print("ABORT / SETUP_ERROR rows (hold unless an expected line names `run` for them)")
        for c in bad:
            ok = any(
                fnmatch.fnmatchcase(c, g)
                and f is not None
                and "run" in f
                and (c not in regress or "status" in f)
                for g, f, _ in expected
            )
            bad_unexplained += not ok
            print(
                f"  {c}  base: {b[c].get('status')} {b[c].get('reason')} {b[c].get('detail', '')}"
                f" | tree: {n[c].get('status')} {n[c].get('reason')} {n[c].get('detail', '')}"
                f"  [{'expected' if ok else 'NOT EXPLAINED'}]"
            )
    slow = [
        (c, b[c].get("secs", 0), n[c].get("secs", 0))
        for c in names_ok
        if n[c].get("secs", 0) > 2 and n[c].get("secs", 0) > 2.5 * max(b[c].get("secs", 0), 0.2)
    ]
    if slow:
        print()
        print("SLOWER on the tree (more than 2.5x and over 2 s)")
        for c, sb, sn in slow:
            print(f"  {c}: base {sb} s, tree {sn} s")
    peak = max([r.get("peak_mb", 0) for r in list(b.values()) + list(n.values())] or [0])
    print()
    print(
        f"peak memory of any row: {peak} MB; seconds, "
        f"base {sum(r.get('secs', 0) for r in b.values()):.0f}, "
        f"tree {sum(r.get('secs', 0) for r in n.values()):.0f}"
    )
    print(f"result files: {outdir}")
    if stale:
        print(f"ERROR: {len(stale)} stale expected line(s); fix the file")
        return 2
    problems = []
    if missing:
        problems.append(f"{len(missing)} missing row(s)")
    if unexplained:
        problems.append(f"{unexplained} unexplained difference field(s)")
    if bad_unexplained:
        problems.append(f"{bad_unexplained} unexplained abort/setup row(s)")
    if problems:
        print("HOLD: " + ", ".join(problems))
        return 1
    print("OK: every case ran on both sides, every difference is explained, no new flag")
    return 0


if __name__ == "__main__":
    sys.exit(main())
